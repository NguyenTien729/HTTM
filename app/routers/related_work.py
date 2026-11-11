"""Related Work writer: bounded LLM requests per theme, then synthesis.

Compatible with the existing test-pr-2 database, models and API endpoints.
"""
import asyncio
import json
import logging
import re
from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import get_pool
from app.models import RelatedWork, ThemeParagraph, WriteRelatedWorkResponse
from app.services.llm import generate_structured
from app.services.vietnamese_writing import VIETNAMESE_WRITING_RULE

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/write/related-work", tags=["scientific-writer"])
_CITATION_RE = re.compile(r"\[(\d+)\]")


class _SingleTheme(BaseModel):
    paragraph: str = Field(description="Một đoạn văn học thuật ngắn, có citation [paper_id].")


class _FinalSections(BaseModel):
    contradictions_paragraph: str
    gap_paragraph: str


async def _load_research_question(rq_id: int) -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        rq_row = await conn.fetchrow("SELECT * FROM research_questions WHERE id = $1;", rq_id)
        if rq_row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy research question.")
        gap_row = await conn.fetchrow("SELECT * FROM gaps WHERE id = $1;", rq_row["gap_id"]) if rq_row["gap_id"] is not None else None
        project_row = await conn.fetchrow("SELECT topic FROM research_projects WHERE id=$1", rq_row["project_id"])
    data = dict(rq_row)
    data["gap_description"] = gap_row["description"] if gap_row else "Không dùng gap; nghiên cứu trực tiếp theo đề tài project."
    data["gap_type"] = gap_row["gap_type"] if gap_row else "topic"
    data["project_topic"] = project_row["topic"] if project_row else ""
    data["research_basis"] = "research_gap" if gap_row else "project_topic"
    return data


async def _load_themes_with_evidence(project_id: int) -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        theme_rows = await conn.fetch(
            "SELECT id, name, description, paper_ids FROM themes WHERE project_id = $1 ORDER BY id;",
            project_id,
        )
        evidence_rows = await conn.fetch(
            """SELECT pa.paper_id, p.title, pa.key_finding
               FROM paper_analyses pa JOIN papers p ON p.id = pa.paper_id
               WHERE pa.project_id = $1;""",
            project_id,
        )
    evidence_by_id = {r["paper_id"]: dict(r) for r in evidence_rows}
    themes = []
    for t in theme_rows:
        ids = json.loads(t["paper_ids"]) if isinstance(t["paper_ids"], str) else t["paper_ids"]
        themes.append({
            "name": t["name"], "description": t["description"],
            "evidence": [evidence_by_id[pid] for pid in (ids or []) if pid in evidence_by_id],
        })
    return themes


def _verify_citations(sections: list[str], valid_paper_ids: set[int]) -> tuple[list[int], list[int]]:
    cited = {int(m) for section in sections for m in _CITATION_RE.findall(section)}
    return sorted(cited & valid_paper_ids), sorted(cited - valid_paper_ids)


def _evidence_text(theme: dict) -> str:
    return "\n".join(
        f"paper_id={e['paper_id']} | title={json.dumps(e['title'], ensure_ascii=False)} | "
        f"finding={json.dumps(e['key_finding'], ensure_ascii=False)}"
        for e in theme["evidence"]
    )


def _check_theme_citations(paragraph: str, theme: dict) -> None:
    allowed = {int(e["paper_id"]) for e in theme["evidence"]}
    cited = {int(x) for x in _CITATION_RE.findall(paragraph)}
    invalid = cited - allowed
    if invalid:
        raise HTTPException(
            status_code=502,
            detail=f"DeepSeek trích dẫn paper ngoài theme '{theme['name']}': {sorted(invalid)}. Hãy chạy lại Related Work.",
        )


async def _write_one_theme(theme: dict, rq: dict) -> ThemeParagraph:
    prompt = (
        VIETNAMESE_WRITING_RULE +
        "Viết CHỈ MỘT đoạn Related Work bằng tiếng Việt (120-200 từ) cho theme dưới đây. "
        "So sánh kết quả các nghiên cứu nếu có cơ sở, không bịa dữ kiện. "
        "Mỗi nhận định cụ thể phải trích dẫn [paper_id] từ evidence của theme này. "
        "Không dùng paper_id khác. Nếu evidence chưa đủ, ghi rõ giới hạn. "
        "Trả về JSON có đúng field paragraph.\n\n"
        f"Research question: {rq['rq_text']}\n"
        f"Theme: {theme['name']}\nDescription: {theme['description']}\n"
        f"Evidence:\n{_evidence_text(theme)}"
    )
    # Validation is repeated only for the individual theme, not the entire document.
    for attempt in range(2):
        result = await generate_structured(prompt, _SingleTheme)
        try:
            _check_theme_citations(result.paragraph, theme)
            return ThemeParagraph(theme_name=theme["name"], paragraph=result.paragraph)
        except HTTPException:
            if attempt == 1:
                raise
            prompt += "\n\nLần trước có citation không thuộc theme. Chỉ dùng ID trong evidence đã cho."
    raise RuntimeError("unreachable")


async def _write_final_sections(themes: list[ThemeParagraph], rq: dict) -> _FinalSections:
    # Only compact summaries of already generated paragraphs are needed here.
    compact = "\n".join(
        f"{p.theme_name}: {p.paragraph[:700]}" for p in themes
    )
    prompt = (
        VIETNAMESE_WRITING_RULE +
        (f"Đề tài project: {rq.get('project_topic', '')}. Đây là hướng A không phân tích gap. "
         "Trường gap_paragraph chỉ kết nối bằng chứng hiện có với mục tiêu/cách đo biến của đề tài; "
         "không phát hiện hoặc tự nhận đã lấp khoảng trống nghiên cứu.\n" if rq.get("research_basis") == "project_topic" else "") +
        "Viết HAI đoạn tiếng Việt cho phần Related Work, mỗi đoạn 100-160 từ. "
        "1) contradictions_paragraph: đối chiếu những kết quả mâu thuẫn có căn cứ; "
        "nếu không có mâu thuẫn rõ thì nói rõ, không tự tạo. "
        "2) gap_paragraph: kết nối tổng quan tới research gap và câu hỏi nghiên cứu. "
        "Chỉ trích dẫn [paper_id] đã xuất hiện trong các đoạn cung cấp. "
        "Không tự tạo dữ liệu hoặc kết quả. Trả về JSON với đúng hai field trên.\n\n"
        f"Research gap ({rq['gap_type']}): {rq['gap_description']}\n"
        f"Research question: {rq['rq_text']}\n"
        f"Theme summaries:\n{compact}"
    )
    return await generate_structured(prompt, _FinalSections)


@router.post("/{research_question_id}", response_model=WriteRelatedWorkResponse)
async def write_related_work(research_question_id: int) -> WriteRelatedWorkResponse:
    rq = await _load_research_question(research_question_id)
    project_id = rq["project_id"]
    themes = await _load_themes_with_evidence(project_id)
    if not themes:
        raise HTTPException(status_code=400, detail="Chưa có theme nào. Gọi POST /synthesize/{project_id} trước.")
    all_ids = {int(e["paper_id"]) for t in themes for e in t["evidence"]}
    if not all_ids:
        raise HTTPException(status_code=400, detail="Các theme chưa gắn với paper đã phân tích.")

    # Sequential requests avoid bursts/rate limiting and keep each JSON small.
    paragraphs: list[ThemeParagraph] = []
    for index, theme in enumerate(themes, start=1):
        logger.info("Related Work RQ #%s: writing theme %s/%s: %s", research_question_id, index, len(themes), theme["name"])
        if not theme["evidence"]:
            paragraphs.append(ThemeParagraph(
                theme_name=theme["name"],
                paragraph="Chưa có bằng chứng paper đã phân tích cho theme này; cần bổ sung tài liệu trước khi tổng hợp.",
            ))
            continue
        paragraphs.append(await _write_one_theme(theme, rq))

    final = await _write_final_sections(paragraphs, rq)
    sections = [p.paragraph for p in paragraphs] + [final.contradictions_paragraph, final.gap_paragraph]
    citations_used, citations_invalid = _verify_citations(sections, all_ids)
    if citations_invalid:
        logger.warning("Related Work RQ #%s: invalid citations %s", research_question_id, citations_invalid)
    payload = [p.model_dump() for p in paragraphs]
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO related_works
               (project_id, research_question_id, theme_paragraphs,
                contradictions_paragraph, gap_paragraph, citations_used, citations_invalid)
               VALUES ($1, $2, $3::jsonb, $4, $5, $6::jsonb, $7::jsonb)
               RETURNING *;""",
            project_id, research_question_id, json.dumps(payload, ensure_ascii=False),
            final.contradictions_paragraph, final.gap_paragraph,
            json.dumps(citations_used), json.dumps(citations_invalid),
        )
    related_work = RelatedWork(
        id=row["id"], project_id=project_id, research_question_id=research_question_id,
        theme_paragraphs=paragraphs,
        contradictions_paragraph=row["contradictions_paragraph"],
        gap_paragraph=row["gap_paragraph"],
        citations_used=citations_used, citations_invalid=citations_invalid,
    )
    return WriteRelatedWorkResponse(
        project_id=project_id, research_question_id=research_question_id, related_work=related_work,
    )


@router.get("/project/{project_id}", response_model=list[RelatedWork])
async def list_related_works(project_id: int) -> list[RelatedWork]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM related_works WHERE project_id = $1 ORDER BY id;", project_id)
    return [RelatedWork(
        id=r["id"], project_id=r["project_id"], research_question_id=r["research_question_id"],
        theme_paragraphs=[ThemeParagraph.model_validate(tp) for tp in (
            json.loads(r["theme_paragraphs"]) if isinstance(r["theme_paragraphs"], str) else r["theme_paragraphs"]
        )],
        contradictions_paragraph=r["contradictions_paragraph"], gap_paragraph=r["gap_paragraph"],
        citations_used=json.loads(r["citations_used"]) if isinstance(r["citations_used"], str) else r["citations_used"],
        citations_invalid=json.loads(r["citations_invalid"]) if isinstance(r["citations_invalid"], str) else r["citations_invalid"],
    ) for r in rows]
