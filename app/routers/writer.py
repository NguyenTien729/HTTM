"""
Module 11 trong kế hoạch: Scientific Writer (phần Introduction).

Luồng:
    Research Question (đã sinh từ gap đã 👤 duyệt)
        ↓ Section planner (gộp trong 1 lần gọi LLM cho toàn bộ Introduction)
    Draft generation — MỖI câu quan trọng PHẢI có citation [paper_id] bám theo
    evidence thật (title + key_finding) đã cung cấp trong prompt
        ↓
    Claim verification (CODE, không phải LLM): trích toàn bộ [paper_id] xuất hiện
    trong text, đối chiếu với danh sách paper_id thật của project — paper_id nào
    không tồn tại bị coi là hallucination, đánh dấu vào citations_invalid
        ↓
    Lưu bảng `introductions`

Đây là bước chuẩn bị trực tiếp cho Citation Manager / Reviewer Agent (module 12) —
citations_invalid chính là input cho reviewer "Citation checker" sau này.
"""

import re
import json
import logging

from fastapi import APIRouter, HTTPException

from app.models import Introduction, IntroductionExtraction, WriteIntroductionResponse
from app.db import get_pool
from app.services.llm import generate_structured

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/write/introduction", tags=["scientific-writer"])

_CITATION_RE = re.compile(r"\[(\d+)\]")


async def _load_research_question(rq_id: int) -> dict:
    """Lấy RQ + gap liên quan + topic của project. Raise 404 nếu không tồn tại."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rq_row = await conn.fetchrow("SELECT * FROM research_questions WHERE id = $1;", rq_id)
        if rq_row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy research question.")

        gap_row = await conn.fetchrow("SELECT * FROM gaps WHERE id = $1;", rq_row["gap_id"])
        project_row = await conn.fetchrow(
            "SELECT topic FROM research_projects WHERE id = $1;", rq_row["project_id"]
        )

    data = dict(rq_row)
    data["gap_description"] = gap_row["description"] if gap_row else ""
    data["gap_type"] = gap_row["gap_type"] if gap_row else ""
    data["project_topic"] = project_row["topic"] if project_row else ""
    return data


async def _load_citable_evidence(project_id: int) -> list[dict]:
    """Lấy các paper ĐÃ được phân tích (có key_finding) — đây là nguồn duy nhất
    LLM được phép cite. Paper chưa phân tích không đưa vào evidence pool."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pa.paper_id, p.title, pa.key_finding
            FROM paper_analyses pa
            JOIN papers p ON p.id = pa.paper_id
            WHERE pa.project_id = $1
            ORDER BY pa.paper_id;
            """,
            project_id,
        )
    return [dict(r) for r in rows]


async def _load_themes_text(project_id: int) -> str:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT name, description FROM themes WHERE project_id = $1;", project_id)
    if not rows:
        return "(chưa có theme nào được tổng hợp)"
    return "\n".join(f"- {r['name']}: {r['description']}" for r in rows)


def _format_evidence_for_prompt(evidence: list[dict]) -> str:
    return "\n".join(
        f"paper_id={e['paper_id']} | title=\"{e['title']}\" | finding=\"{e['key_finding']}\""
        for e in evidence
    )


def _verify_citations(sections: list[str], valid_paper_ids: set[int]) -> tuple[list[int], list[int]]:
    """Trích toàn bộ [paper_id] xuất hiện trong text, phân loại hợp lệ/không hợp lệ.
    Đây là bước KHÔNG dùng LLM — thuần regex + so khớp DB, để không thể bị model
    'tự nhận là đúng'."""
    cited_ids = set()
    for text in sections:
        for match in _CITATION_RE.findall(text):
            cited_ids.add(int(match))

    valid = sorted(cited_ids & valid_paper_ids)
    invalid = sorted(cited_ids - valid_paper_ids)
    return valid, invalid


@router.post("/{research_question_id}", response_model=WriteIntroductionResponse)
async def write_introduction(research_question_id: int) -> WriteIntroductionResponse:
    """Viết Introduction cho 1 research question đã sinh."""
    rq = await _load_research_question(research_question_id)
    project_id = rq["project_id"]

    evidence = await _load_citable_evidence(project_id)
    if not evidence:
        raise HTTPException(
            status_code=400,
            detail="Chưa có paper nào được phân tích trong project này. Gọi POST /analyze/{project_id} trước.",
        )
    themes_text = await _load_themes_text(project_id)

    prompt = (
        f"Đề tài nghiên cứu: {rq['project_topic']}\n"
        f"Research question: {rq['rq_text']}\n"
        f"Research gap liên quan ({rq['gap_type']}): {rq['gap_description']}\n\n"
        f"THEMES đã tổng hợp từ literature:\n{themes_text}\n\n"
        f"EVIDENCE — danh sách paper có thật, PHẢI dùng đúng paper_id dưới đây khi cite, "
        f"KHÔNG được bịa paper_id không có trong danh sách:\n{_format_evidence_for_prompt(evidence)}\n\n"
        "Hãy viết phần Introduction cho bài báo khoa học, gồm các đoạn: bối cảnh chung, "
        "vấn đề nghiên cứu, tổng hợp kiến thức đã có (dựa trên themes + evidence), hạn chế "
        "của literature hiện có, research gap, research question, và đóng góp dự kiến. "
        "MỖI câu có luận điểm dựa trên 1 paper cụ thể PHẢI có citation ngay sau câu theo "
        "định dạng [paper_id] — ví dụ: 'Generative AI có thể cải thiện năng suất viết [12].' "
        "Nếu không có evidence cụ thể cho 1 câu, không thêm citation cho câu đó."
    )

    extracted = await generate_structured(prompt, IntroductionExtraction)

    valid_paper_ids = {e["paper_id"] for e in evidence}
    sections = [
        extracted.background,
        extracted.problem_statement,
        extracted.existing_knowledge,
        extracted.literature_limitation,
        extracted.research_gap_statement,
        extracted.research_question_statement,
        extracted.contribution_statement,
    ]
    citations_used, citations_invalid = _verify_citations(sections, valid_paper_ids)

    if citations_invalid:
        logger.warning(
            "Introduction cho RQ #%d có citation KHÔNG tồn tại trong project: %s",
            research_question_id, citations_invalid,
        )

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO introductions
                (project_id, research_question_id, background, problem_statement,
                 existing_knowledge, literature_limitation, research_gap_statement,
                 research_question_statement, contribution_statement,
                 citations_used, citations_invalid)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb)
            RETURNING *;
            """,
            project_id, research_question_id,
            extracted.background, extracted.problem_statement, extracted.existing_knowledge,
            extracted.literature_limitation, extracted.research_gap_statement,
            extracted.research_question_statement, extracted.contribution_statement,
            json.dumps(citations_used), json.dumps(citations_invalid),
        )

    intro = Introduction(
        id=row["id"], project_id=project_id, research_question_id=research_question_id,
        background=row["background"], problem_statement=row["problem_statement"],
        existing_knowledge=row["existing_knowledge"], literature_limitation=row["literature_limitation"],
        research_gap_statement=row["research_gap_statement"],
        research_question_statement=row["research_question_statement"],
        contribution_statement=row["contribution_statement"],
        citations_used=citations_used, citations_invalid=citations_invalid,
    )
    return WriteIntroductionResponse(
        project_id=project_id, research_question_id=research_question_id, introduction=intro
    )


@router.get("/project/{project_id}", response_model=list[Introduction])
async def list_introductions(project_id: int) -> list[Introduction]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM introductions WHERE project_id = $1 ORDER BY id;", project_id
        )
    return [
        Introduction(
            id=r["id"], project_id=r["project_id"], research_question_id=r["research_question_id"],
            background=r["background"], problem_statement=r["problem_statement"],
            existing_knowledge=r["existing_knowledge"], literature_limitation=r["literature_limitation"],
            research_gap_statement=r["research_gap_statement"],
            research_question_statement=r["research_question_statement"],
            contribution_statement=r["contribution_statement"],
            citations_used=json.loads(r["citations_used"]) if isinstance(r["citations_used"], str) else r["citations_used"],
            citations_invalid=json.loads(r["citations_invalid"]) if isinstance(r["citations_invalid"], str) else r["citations_invalid"],
        )
        for r in rows
    ]