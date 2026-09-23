"""
Module 3 trong kế hoạch: Literature Search.

Luồng:
    Research Plan (đã lưu ở /plan)
        ↓ Query Generator
    OpenAlex API
        ↓ Parse metadata
    Deduplication (theo DOI, trong phạm vi 1 project)
        ↓
    Lưu bảng `papers`

Không để LLM tự nhớ paper — toàn bộ metadata lấy trực tiếp từ OpenAlex.
"""

import json
import logging

from fastapi import APIRouter, HTTPException

from app.models import ResearchPlan, SearchResponse, Paper
from app.db import get_pool
from app.services.query_builder import build_query_from_plan
from app.services.openalex_client import search_openalex

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/search", tags=["literature-search"])


async def _load_plan(project_id: int) -> ResearchPlan:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT research_plan FROM research_projects WHERE id = $1;", project_id
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy research project.")
    return ResearchPlan.model_validate(json.loads(row["research_plan"]))


async def _save_papers(papers: list[Paper]) -> int:
    """Lưu papers vào DB, bỏ qua paper trùng DOI (đã tồn tại trong cùng project).
    Trả về số paper THỰC SỰ được lưu mới."""
    if not papers:
        return 0

    pool = get_pool()
    saved_count = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for paper in papers:
                row = await conn.fetchrow(
                    """
                    INSERT INTO papers
                        (project_id, title, authors, year, doi, abstract, venue, url, source, citation_count)
                    VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (project_id, doi) WHERE doi IS NOT NULL DO NOTHING
                    RETURNING id;
                    """,
                    paper.project_id,
                    paper.title,
                    json.dumps(paper.authors),
                    paper.year,
                    paper.doi,
                    paper.abstract,
                    paper.venue,
                    paper.url,
                    paper.source,
                    paper.citation_count,
                )
                if row is not None:
                    saved_count += 1
    return saved_count


@router.post("/{project_id}", response_model=SearchResponse)
async def run_search(project_id: int) -> SearchResponse:
    """Chạy Literature Search cho 1 research project đã có plan."""
    plan = await _load_plan(project_id)
    query = build_query_from_plan(plan)

    try:
        papers = await search_openalex(
            query=query,
            project_id=project_id,
            date_range=plan.search_strategy.date_range,
            per_page=plan.search_strategy.target_paper_count,
        )
    except Exception:
        raise HTTPException(status_code=502, detail="Không gọi được OpenAlex API.")

    saved = await _save_papers(papers)

    return SearchResponse(
        project_id=project_id,
        query_used=query,
        found=len(papers),
        saved=saved,
        papers=papers,
    )


@router.get("/{project_id}", response_model=list[Paper])
async def list_papers(project_id: int) -> list[Paper]:
    """Lấy toàn bộ paper đã lưu cho 1 project."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM papers WHERE project_id = $1 ORDER BY citation_count DESC;",
            project_id,
        )
    return [
        Paper(
            id=r["id"],
            project_id=r["project_id"],
            title=r["title"],
            authors=json.loads(r["authors"]) if isinstance(r["authors"], str) else r["authors"],
            year=r["year"],
            doi=r["doi"],
            abstract=r["abstract"],
            venue=r["venue"],
            url=r["url"],
            source=r["source"],
            citation_count=r["citation_count"],
        )
        for r in rows
    ]