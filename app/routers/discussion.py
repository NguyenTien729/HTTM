"""
Module Scientific Writer: Discussion + Conclusion.

Khác Introduction/Related Work (chỉ dựa trên literature), phần này dựa thêm vào KẾT QUẢ
PHÂN TÍCH DỮ LIỆU THẬT do module Data Research sinh ra:

    POST /data-research/analyze  (người dùng đã duyệt dataset, confirm_dataset=true)
        ↓ client gửi nguyên văn JSON kết quả vào body của endpoint này
    RQ + giả thuyết + gap (DB)  +  themes/evidence (DB)  +  kết quả phân tích (body)
        ↓ LLM lần 1: Discussion (6 phần + đánh giá từng giả thuyết)
        ↓ LLM lần 2: Conclusion (ngắn, dựa trên Discussion vừa viết)
        ↓
    Verify bằng CODE (không dùng LLM):
        - citation [paper_id] phải có thật trong evidence của project  → citations_invalid
        - mọi con số trong text phải khớp số liệu trong kết quả phân tích → numbers_unverified
        ↓
    Lưu bảng `discussions`

Vì sao tách 2 lần gọi LLM: output dài dễ bị cắt cụt giữa chừng (lỗi JSON "Unterminated
string" đã gặp ở Related Work), nên mỗi lần chỉ sinh phần vừa đủ.

Module Data Research hiện stateless (không lưu DB) nên kết quả phân tích được truyền
qua body; bản rút gọn được lưu vào `analysis_snapshot` để truy vết.
"""

import re
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from app.models import (
    Discussion,
    DiscussionExtraction,
    ConclusionExtraction,
    HypothesisAssessment,
    WriteDiscussionRequest,
    WriteDiscussionResponse,
)
from app.db import get_pool
from app.services.llm import generate_structured
# Dùng lại helper của Related Work để không lệch logic (chỉ đọc, không sửa file đó).
from app.routers.related_work import (
    _load_research_question,
    _load_themes_with_evidence,
    _verify_citations,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/write/discussion", tags=["scientific-writer"])

_CITATION_RE = re.compile(r"\[\d+\]")
_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w])")
_MAX_NUMBERS_REPORTED = 30


# --------------------------------------------------------------------------- #
# Rút gọn kết quả phân tích để đưa vào prompt (giữ nguyên số liệu, bỏ phần dài)
# --------------------------------------------------------------------------- #

def _top_correlations(corr: dict | None, top_n: int = 10) -> list[dict]:
    if not isinstance(corr, dict):
        return []
    seen, pairs = set(), []
    for a, row in corr.items():
        if not isinstance(row, dict):
            continue
        for b, r in row.items():
            if a == b or (b, a) in seen or not isinstance(r, (int, float)):
                continue
            seen.add((a, b))
            pairs.append({"var_1": a, "var_2": b, "pearson_r": r})
    pairs.sort(key=lambda x: abs(x["pearson_r"]), reverse=True)
    return pairs[:top_n]


def _compact_analysis(result: dict[str, Any]) -> dict[str, Any]:
    dataset = result.get("dataset") or {}
    overview = result.get("overview") or {}
    summaries = result.get("column_summaries") or {}
    return {
        "dataset": {k: dataset.get(k) for k in (
            "dataset_id", "config", "split", "source_url", "rows_in_selected_split",
            "rows_analyzed", "sampled", "sampling_method",
        ) if dataset.get(k) is not None},
        "overview": {
            "rows_analyzed": overview.get("rows_analyzed"),
            "columns_count": overview.get("columns_count"),
            "duplicate_rows": overview.get("duplicate_rows"),
            "missing_cells_total": overview.get("missing_cells_total"),
            "top_missing_columns": (overview.get("missing_by_column") or [])[:5],
        },
        "column_summaries": dict(list(summaries.items())[:15]),
        "top_correlations_pearson": _top_correlations(result.get("correlation_pearson")),
        "group_test": result.get("group_test"),
        "ols_regression": result.get("ols_regression"),
        "limitations_from_analysis": result.get("limitations") or [],
    }


def _validate_analysis_result(result: dict[str, Any]) -> None:
    if not isinstance(result.get("overview"), dict) or not isinstance(result.get("dataset"), dict):
        raise HTTPException(
            status_code=400,
            detail="analysis_result không đúng định dạng. Hãy gửi nguyên văn JSON trả về từ POST /data-research/analyze.",
        )


# --------------------------------------------------------------------------- #
# Verify số liệu (code, không LLM)
# --------------------------------------------------------------------------- #

def _numbers_in(text: str) -> list[str]:
    return _NUMBER_RE.findall(text)


def _find_unverified_numbers(sections: list[str], source_text: str) -> list[str]:
    """Trả về các số xuất hiện trong text viết ra nhưng không có trong dữ liệu nguồn.

    Khớp có dung sai làm tròn: '0.41' khớp nguồn '0.4123'. Bỏ qua số nguyên 1 chữ số
    (đếm 1-9, 'H1', 'hai nhóm'...), năm (1900-2100) và chỉ mục citation [n].
    Đây là cảnh báo cho người rà soát, không phải bằng chứng số đó sai."""
    source_values = [float(n) for n in _numbers_in(source_text)]
    flagged: list[str] = []
    for text in sections:
        cleaned = _CITATION_RE.sub(" ", text)
        for token in _numbers_in(cleaned):
            has_decimal = "." in token
            value = float(token)
            if not has_decimal and (abs(value) < 10 or 1900 <= abs(value) <= 2100):
                continue
            decimals = len(token.split(".")[1]) if has_decimal else 0
            tol = 0.5 * 10 ** (-decimals) + 1e-9
            ok = any(abs(value - s) <= tol or abs(abs(value) - abs(s)) <= tol for s in source_values)
            # Phần trăm viết lại từ tỷ lệ (0.25 → 25%) hoặc ngược lại
            ok = ok or any(abs(value - s * 100) <= tol for s in source_values)
            if not ok and token not in flagged:
                flagged.append(token)
    return flagged[:_MAX_NUMBERS_REPORTED]


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

def _jsonb(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _format_hypotheses(rq: dict) -> str:
    hyps = _jsonb(rq.get("hypotheses")) or []
    if not hyps:
        return "(RQ này không có giả thuyết — để hypothesis_assessments rỗng)"
    return "\n".join(
        f"- {h['label']}: {h['statement']} (biến độc lập: {h['independent_variable']}; "
        f"biến phụ thuộc: {h['dependent_variable']})"
        for h in hyps
    )


def _format_literature(themes: list[dict]) -> str:
    blocks = []
    for t in themes:
        lines = "\n".join(
            f"  paper_id={e['paper_id']} | title=\"{e['title']}\" | finding=\"{e['key_finding']}\""
            for e in t["evidence"]
        )
        blocks.append(f"THEME: {t['name']}\n{lines}")
    return "\n\n".join(blocks)


def _discussion_prompt(rq: dict, themes: list[dict], analysis_json: str) -> str:
    return (
        f"Research question: {rq['rq_text']}\n"
        f"Research gap ({rq['gap_type']}): {rq['gap_description']}\n\n"
        f"GIẢ THUYẾT cần đánh giá:\n{_format_hypotheses(rq)}\n\n"
        f"KẾT QUẢ PHÂN TÍCH DỮ LIỆU (nguồn số liệu DUY NHẤT — mọi con số bạn viết phải lấy "
        f"nguyên từ đây, không tự tính thêm, không làm tròn khác đi, không bịa):\n{analysis_json}\n\n"
        f"LITERATURE (paper_id có thật — chỉ cite các paper_id này, không bịa số mới):\n"
        f"{_format_literature(themes)}\n\n"
        "Hãy viết phần Discussion: diễn giải kết quả chính; so sánh với literature (mỗi luận "
        "điểm kèm citation [paper_id] ngay sau câu); đánh giá từng giả thuyết; ý nghĩa; hạn chế; "
        "hướng tiếp theo.\n"
        "QUY TẮC TRUNG THỰC:\n"
        "1. Đây là phân tích khám phá trên dataset công khai do người dùng chọn, không phải khảo sát "
        "do tác giả thiết kế. Nếu biến trong dataset không khớp biến của giả thuyết, đặt verdict=not_tested "
        "và nói rõ trong hạn chế.\n"
        "2. Tương quan/hồi quy/kiểm định nhóm KHÔNG chứng minh quan hệ nhân quả; không dùng từ 'gây ra', "
        "'dẫn đến' cho quan hệ thống kê.\n"
        "3. p-value chưa hiệu chỉnh đa kiểm định; không nhấn mạnh 'có ý nghĩa' cho kết quả chỉ vừa qua ngưỡng.\n"
        "4. Nếu phép kiểm định/hồi quy có status not_run hoặc vắng mặt, nói thẳng là chưa thực hiện được, "
        "không suy đoán kết quả.\n"
        "5. Nếu kết quả khác literature, nêu sự khác biệt thay vì ép cho nhất quán."
    )


def _conclusion_prompt(rq: dict, discussion: DiscussionExtraction) -> str:
    verdicts = "; ".join(f"{a.label}={a.verdict}" for a in discussion.hypothesis_assessments) or "không có giả thuyết"
    return (
        f"Research question: {rq['rq_text']}\n\n"
        f"Tóm tắt Discussion đã viết:\n"
        f"- Kết quả chính: {discussion.key_findings_paragraph}\n"
        f"- Hạn chế: {discussion.limitations_paragraph}\n"
        f"- Kết luận từng giả thuyết: {verdicts}\n\n"
        "Viết 1 đoạn Conclusion ngắn gọn: trả lời research question dựa trên kết quả trên, nêu đóng góp "
        "chính và một câu về hạn chế quan trọng nhất. KHÔNG thêm con số, citation hay kết luận mạnh hơn "
        "những gì Discussion đã nêu."
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@router.post("/{research_question_id}", response_model=WriteDiscussionResponse)
async def write_discussion(
    research_question_id: int, request: WriteDiscussionRequest
) -> WriteDiscussionResponse:
    """Viết Discussion + Conclusion cho 1 research question, dựa trên kết quả /data-research/analyze."""
    _validate_analysis_result(request.analysis_result)

    rq = await _load_research_question(research_question_id)
    project_id = rq["project_id"]

    themes = await _load_themes_with_evidence(project_id)
    valid_paper_ids = {e["paper_id"] for t in themes for e in t["evidence"]}
    if not valid_paper_ids:
        raise HTTPException(
            status_code=400,
            detail="Chưa có theme/evidence nào để so sánh với literature. Gọi POST /analyze và POST /synthesize trước.",
        )

    compact = _compact_analysis(request.analysis_result)
    analysis_json = json.dumps(compact, ensure_ascii=False, default=str)
    # Snapshot lưu DB = bản rút gọn + Methods/Results do CODE sinh từ lần chạy thật
    # (Data Research không lưu DB, nên nếu không giữ lại ở đây thì /export/paper không có
    # nguồn để dựng phần Methods/Results). Không đưa 2 draft này vào prompt LLM.
    snapshot_json = json.dumps(
        compact | {
            "methods_draft": request.analysis_result.get("methods_draft"),
            "results_draft": request.analysis_result.get("results_draft"),
        },
        ensure_ascii=False, default=str,
    )

    discussion = await generate_structured(
        _discussion_prompt(rq, themes, analysis_json), DiscussionExtraction
    )
    conclusion = await generate_structured(
        _conclusion_prompt(rq, discussion), ConclusionExtraction
    )

    sections = [
        discussion.key_findings_paragraph,
        discussion.literature_comparison_paragraph,
        *[a.explanation for a in discussion.hypothesis_assessments],
        discussion.implications_paragraph,
        discussion.limitations_paragraph,
        discussion.future_work_paragraph,
        conclusion.conclusion_paragraph,
    ]
    citations_used, citations_invalid = _verify_citations(sections, valid_paper_ids)
    # Nguồn số liệu hợp lệ = kết quả phân tích + điểm RQ (không có số nào khác được phép).
    numbers_unverified = _find_unverified_numbers(sections, analysis_json)

    if citations_invalid:
        logger.warning("Discussion RQ #%d có citation không tồn tại: %s", research_question_id, citations_invalid)
    if numbers_unverified:
        logger.warning("Discussion RQ #%d có số không khớp dữ liệu phân tích: %s", research_question_id, numbers_unverified)

    assessments = [HypothesisAssessment(**a.model_dump()) for a in discussion.hypothesis_assessments]

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO discussions
                (project_id, research_question_id, key_findings_paragraph,
                 literature_comparison_paragraph, hypothesis_assessments,
                 implications_paragraph, limitations_paragraph, future_work_paragraph,
                 conclusion_paragraph, citations_used, citations_invalid,
                 numbers_unverified, analysis_snapshot)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9,
                    $10::jsonb, $11::jsonb, $12::jsonb, $13::jsonb)
            RETURNING id;
            """,
            project_id, research_question_id,
            discussion.key_findings_paragraph, discussion.literature_comparison_paragraph,
            json.dumps([a.model_dump() for a in assessments], ensure_ascii=False),
            discussion.implications_paragraph, discussion.limitations_paragraph,
            discussion.future_work_paragraph, conclusion.conclusion_paragraph,
            json.dumps(citations_used), json.dumps(citations_invalid),
            json.dumps(numbers_unverified), snapshot_json,
        )

    saved = Discussion(
        id=row["id"], project_id=project_id, research_question_id=research_question_id,
        key_findings_paragraph=discussion.key_findings_paragraph,
        literature_comparison_paragraph=discussion.literature_comparison_paragraph,
        hypothesis_assessments=assessments,
        implications_paragraph=discussion.implications_paragraph,
        limitations_paragraph=discussion.limitations_paragraph,
        future_work_paragraph=discussion.future_work_paragraph,
        conclusion_paragraph=conclusion.conclusion_paragraph,
        citations_used=citations_used, citations_invalid=citations_invalid,
        numbers_unverified=numbers_unverified,
    )
    return WriteDiscussionResponse(
        project_id=project_id, research_question_id=research_question_id, discussion=saved
    )


@router.get("/project/{project_id}", response_model=list[Discussion])
async def list_discussions(project_id: int) -> list[Discussion]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM discussions WHERE project_id = $1 ORDER BY id;", project_id)
    return [
        Discussion(
            id=r["id"], project_id=r["project_id"], research_question_id=r["research_question_id"],
            key_findings_paragraph=r["key_findings_paragraph"],
            literature_comparison_paragraph=r["literature_comparison_paragraph"],
            hypothesis_assessments=[HypothesisAssessment.model_validate(a) for a in _jsonb(r["hypothesis_assessments"])],
            implications_paragraph=r["implications_paragraph"],
            limitations_paragraph=r["limitations_paragraph"],
            future_work_paragraph=r["future_work_paragraph"],
            conclusion_paragraph=r["conclusion_paragraph"],
            citations_used=_jsonb(r["citations_used"]),
            citations_invalid=_jsonb(r["citations_invalid"]),
            numbers_unverified=_jsonb(r["numbers_unverified"]),
        )
        for r in rows
    ]