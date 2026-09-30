"""
Module 11 trong kế hoạch: Scientific Writer (phần Related Work).

Cấu trúc theo đúng outline gốc:
    Theme 1 → Theme 2 → Theme 3 → ... → Contradictions → Gap

Luồng:
    themes (đã có từ POST /synthesize/{project_id}) + evidence của từng theme
        ↓ 1 lần gọi LLM cho toàn bộ Related Work
    theme_paragraphs (1 đoạn/theme) + contradictions_paragraph + gap_paragraph
        ↓
    Claim verification (CODE, không phải LLM) — giống hệt cơ chế ở module Introduction
        ↓
    Lưu bảng `related_works`

Không tách file này vào writer.py để tránh đụng vào router Introduction đã test —
mỗi module Scientific Writer con nằm trong 1 file router riêng, theo đúng quy ước
chia việc của team (mỗi người 1 file, không ai sửa file người khác).
"""

import re
import json
import logging

from fastapi import APIRouter, HTTPException

from app.models import RelatedWork, RelatedWorkExtraction, ThemeParagraph, WriteRelatedWorkResponse
from app.db import get_pool
from app.services.llm import generate_structured

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/write/related-work", tags=["scientific-writer"])

_CITATION_RE = re.compile(r"\[(\d+)\]")


async def _load_research_question(rq_id: int) -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        rq_row = await conn.fetchrow("SELECT * FROM research_questions WHERE id = $1;", rq_id)
        if rq_row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy research question.")
        gap_row = await conn.fetchrow("SELECT * FROM gaps WHERE id = $1;", rq_row["gap_id"])

    data = dict(rq_row)
    data["gap_description"] = gap_row["description"] if gap_row else ""
    data["gap_type"] = gap_row["gap_type"] if gap_row else ""
    return data


async def _load_themes_with_evidence(project_id: int) -> list[dict]:
    """Lấy themes kèm evidence (title + key_finding) của TỪNG paper thuộc theme đó —
    để LLM viết đoạn văn cho theme nào chỉ dựa trên đúng paper của theme đó."""
    pool = get_pool()
    async with pool.acquire() as conn:
        theme_rows = await conn.fetch(
            "SELECT id, name, description, paper_ids FROM themes WHERE project_id = $1 ORDER BY id;",
            project_id,
        )
        evidence_rows = await conn.fetch(
            """
            SELECT pa.paper_id, p.title, pa.key_finding
            FROM paper_analyses pa
            JOIN papers p ON p.id = pa.paper_id
            WHERE pa.project_id = $1;
            """,
            project_id,
        )

    evidence_by_id = {r["paper_id"]: dict(r) for r in evidence_rows}

    themes = []
    for t in theme_rows:
        paper_ids = json.loads(t["paper_ids"]) if isinstance(t["paper_ids"], str) else t["paper_ids"]
        themes.append({
            "name": t["name"],
            "description": t["description"],
            "evidence": [evidence_by_id[pid] for pid in paper_ids if pid in evidence_by_id],
        })
    return themes


def _format_themes_for_prompt(themes: list[dict]) -> str:
    blocks = []
    for t in themes:
        evidence_lines = "\n".join(
            f"  paper_id={e['paper_id']} | title=\"{e['title']}\" | finding=\"{e['key_finding']}\""
            for e in t["evidence"]
        )
        blocks.append(f"THEME: {t['name']}\nMô tả: {t['description']}\nEvidence:\n{evidence_lines}")
    return "\n\n".join(blocks)


def _verify_citations(sections: list[str], valid_paper_ids: set[int]) -> tuple[list[int], list[int]]:
    cited_ids = set()
    for text in sections:
        for match in _CITATION_RE.findall(text):
            cited_ids.add(int(match))
    return sorted(cited_ids & valid_paper_ids), sorted(cited_ids - valid_paper_ids)


@router.post("/{research_question_id}", response_model=WriteRelatedWorkResponse)
async def write_related_work(research_question_id: int) -> WriteRelatedWorkResponse:
    """Viết Related Work (theo theme) cho 1 research question đã sinh."""
    rq = await _load_research_question(research_question_id)
    project_id = rq["project_id"]

    themes = await _load_themes_with_evidence(project_id)
    if not themes:
        raise HTTPException(
            status_code=400,
            detail="Chưa có theme nào. Gọi POST /synthesize/{project_id} trước.",
        )

    all_evidence_ids = {e["paper_id"] for t in themes for e in t["evidence"]}
    if not all_evidence_ids:
        raise HTTPException(
            status_code=400,
            detail="Các theme hiện có chưa gắn với paper nào đã phân tích. Kiểm tra lại bước /synthesize.",
        )

    prompt = (
        f"Research question: {rq['rq_text']}\n"
        f"Research gap sẽ dẫn tới ({rq['gap_type']}): {rq['gap_description']}\n\n"
        f"DANH SÁCH THEME kèm evidence (paper_id có thật) — PHẢI dùng đúng paper_id "
        f"trong evidence của MỖI theme khi viết đoạn cho theme đó, KHÔNG bịa số mới, "
        f"KHÔNG dùng paper_id của theme khác cho theme này:\n\n"
        f"{_format_themes_for_prompt(themes)}\n\n"
        "Hãy viết phần Related Work: mỗi theme 1 đoạn văn tổng hợp/so sánh các paper "
        "trong theme đó (câu có luận điểm cụ thể PHẢI có citation [paper_id] ngay sau câu). "
        "Sau đó viết 1 đoạn Contradictions nêu mâu thuẫn/đối lập giữa các theme nếu có "
        "(nếu các theme không thực sự mâu thuẫn, ghi nhận thẳng điều đó, không bịa mâu thuẫn). "
        "Cuối cùng viết 1 đoạn Gap nối từ các theme/mâu thuẫn trên tới research gap đã cho ở trên."
    )

    extracted = await generate_structured(prompt, RelatedWorkExtraction)

    sections = [tp.paragraph for tp in extracted.theme_paragraphs]
    sections.append(extracted.contradictions_paragraph)
    sections.append(extracted.gap_paragraph)
    citations_used, citations_invalid = _verify_citations(sections, all_evidence_ids)

    if citations_invalid:
        logger.warning(
            "Related Work cho RQ #%d có citation KHÔNG tồn tại trong evidence: %s",
            research_question_id, citations_invalid,
        )

    theme_paragraphs_json = [tp.model_dump() for tp in extracted.theme_paragraphs]

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO related_works
                (project_id, research_question_id, theme_paragraphs,
                 contradictions_paragraph, gap_paragraph, citations_used, citations_invalid)
            VALUES ($1, $2, $3::jsonb, $4, $5, $6::jsonb, $7::jsonb)
            RETURNING *;
            """,
            project_id, research_question_id, json.dumps(theme_paragraphs_json),
            extracted.contradictions_paragraph, extracted.gap_paragraph,
            json.dumps(citations_used), json.dumps(citations_invalid),
        )

    related_work = RelatedWork(
        id=row["id"], project_id=project_id, research_question_id=research_question_id,
        theme_paragraphs=[ThemeParagraph.model_validate(tp) for tp in theme_paragraphs_json],
        contradictions_paragraph=row["contradictions_paragraph"],
        gap_paragraph=row["gap_paragraph"],
        citations_used=citations_used, citations_invalid=citations_invalid,
    )
    return WriteRelatedWorkResponse(
        project_id=project_id, research_question_id=research_question_id, related_work=related_work
    )


@router.get("/project/{project_id}", response_model=list[RelatedWork])
async def list_related_works(project_id: int) -> list[RelatedWork]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM related_works WHERE project_id = $1 ORDER BY id;", project_id
        )
    return [
        RelatedWork(
            id=r["id"], project_id=r["project_id"], research_question_id=r["research_question_id"],
            theme_paragraphs=[
                ThemeParagraph.model_validate(tp)
                for tp in (json.loads(r["theme_paragraphs"]) if isinstance(r["theme_paragraphs"], str) else r["theme_paragraphs"])
            ],
            contradictions_paragraph=r["contradictions_paragraph"],
            gap_paragraph=r["gap_paragraph"],
            citations_used=json.loads(r["citations_used"]) if isinstance(r["citations_used"], str) else r["citations_used"],
            citations_invalid=json.loads(r["citations_invalid"]) if isinstance(r["citations_invalid"], str) else r["citations_invalid"],
        )
        for r in rows
    ]