"""Literature search: fresh papers, region screening, topical LLM screening."""
import os
import json
import logging
from datetime import date
from fastapi import APIRouter, HTTPException
from app.models import ResearchPlan, SearchResponse, Paper
from app.db import get_pool
from app.services.query_builder import build_query_from_plan
from app.services.openalex_client import search_openalex
from app.services.semantic_scholar_client import search_semantic_scholar
from app.services.paper_merge import merge_papers
from app.services.recency_region import region_terms, window_steps, matches_region
from app.services.relevance_filter import screen_papers, extract_topic_concepts

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/search", tags=["literature-search"])


async def _load_plan(project_id: int) -> ResearchPlan:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT research_plan FROM research_projects WHERE id = $1;", project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy research project.")
    value = row["research_plan"]
    return ResearchPlan.model_validate(json.loads(value) if isinstance(value, str) else value)


async def _save_papers(papers: list[Paper]) -> int:
    if not papers:
        return 0
    pool = get_pool()
    saved_count = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for paper in papers:
                row = await conn.fetchrow(
                    """INSERT INTO papers
                        (project_id, title, authors, year, doi, abstract, venue, url, source, citation_count, pdf_url, publication_date)
                    VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    ON CONFLICT (project_id, doi) WHERE doi IS NOT NULL DO UPDATE SET
                        pdf_url = COALESCE(papers.pdf_url, EXCLUDED.pdf_url),
                        abstract = COALESCE(NULLIF(papers.abstract, ''), EXCLUDED.abstract)
                    RETURNING id, (xmax = 0) AS inserted;""",
                    paper.project_id, paper.title, json.dumps(paper.authors), paper.year,
                    paper.doi, paper.abstract, paper.venue, paper.url, paper.source,
                    paper.citation_count, paper.pdf_url, paper.publication_date,
                )
                if row is not None and row["inserted"]:
                    saved_count += 1
    return saved_count


async def _search_once(plan: ResearchPlan, project_id: int, query: str, start_year: int, today: date) -> list[Paper]:
    target = plan.search_strategy.target_paper_count
    pool_size = min(max(target * 4, 60), 200)
    papers = await search_openalex(
        query=query, project_id=project_id, date_range=plan.search_strategy.date_range,
        per_page=pool_size, from_date=f"{start_year}-01-01", to_date=today.isoformat(),
    )
    if os.environ.get("ENABLE_SEMANTIC_SCHOLAR", "1") != "0":
        # S2 uses a plain text query, not OpenAlex's boolean syntax.
        s2_query = plan.topic.strip() or " ".join(plan.keywords[:3])
        extra = await search_semantic_scholar(
            query=s2_query, project_id=project_id,
            date_range=plan.search_strategy.date_range, limit=min(pool_size, 100),
            start_year=start_year, end_year=today.year,
        )
        papers = merge_papers(papers, extra)
    return papers


def _fresh_and_region(papers: list[Paper], plan: ResearchPlan, start_year: int, today: date) -> list[Paper]:
    terms = region_terms(plan)
    return [p for p in papers if
            (p.year is None or start_year <= p.year <= today.year)
            and (not p.publication_date or str(p.publication_date) <= today.isoformat())
            and matches_region(p, terms)]


@router.post("/{project_id}", response_model=SearchResponse)
async def run_search(project_id: int) -> SearchResponse:
    plan = await _load_plan(project_id)
    query = build_query_from_plan(plan)
    target = plan.search_strategy.target_paper_count
    today = date.today()
    try:
        concepts = await extract_topic_concepts(plan)
    except Exception as exc:
        logger.exception("Cannot extract mandatory topic concepts")
        raise HTTPException(status_code=502, detail="Không xác định được các khái niệm bắt buộc của đề tài.") from exc
    logger.info("Topic concepts: %s; scope=%s", concepts.concepts, concepts.scope_rule)
    selected: list[Paper] = []
    # Reuse decisions between expanding windows; no repeated LLM charge for a paper.
    screened: dict[str, tuple[Paper, int]] = {}
    rejected: set[str] = set()
    windows = []

    for start_year in window_steps(plan.search_strategy.date_range, today):
        try:
            raw = await _search_once(plan, project_id, query, start_year, today)
        except Exception as exc:
            logger.exception("Paper provider failed")
            raise HTTPException(status_code=502, detail="Không gọi được OpenAlex API.") from exc
        candidates = _fresh_and_region(raw, plan, start_year, today)
        windows.append({"from_year": start_year, "to_date": today.isoformat(), "retrieved": len(raw), "after_date_region_filter": len(candidates)})
        fresh: list[Paper] = []
        for p in candidates:
            key = p.doi.lower().strip() if p.doi else p.title.casefold().strip()
            if key not in screened and key not in rejected:
                rejected.add(key)  # mark as processed, including rejected results
                fresh.append(p)
        if fresh:
            try:
                scored = await screen_papers(fresh, plan, concepts=concepts)
            except Exception as exc:
                logger.exception("Relevance screening failed; no unscreened papers will be saved")
                raise HTTPException(status_code=502, detail="Lọc độ liên quan thất bại; chưa lưu paper chưa kiểm duyệt.") from exc
            for p, score in scored:
                key = p.doi.lower().strip() if p.doi else p.title.casefold().strip()
                screened[key] = (p, score)
        # Preserve recency as primary ranking, relevance score as tie breaker.
        ranked = sorted(screened.values(), key=lambda item: (
            str(item[0].publication_date or (f"{item[0].year}-00-00" if item[0].year else "")),
            item[1], item[0].citation_count), reverse=True)
        selected = [p for p, _ in ranked[:target]]
        logger.info("Search %s..today: raw=%d region/date_candidates=%d newly_screened=%d accepted_total=%d rejected_total=%d selected=%d", start_year, len(raw), len(candidates), len(fresh), len(screened), len(rejected)-len(screened), len(selected))
        if len(selected) >= target:
            break

    saved = await _save_papers(selected)
    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO literature_search_runs(project_id, audit) VALUES ($1, $2::jsonb)",
            project_id, json.dumps({"query": query, "search_date": today.isoformat(), "windows": windows,
                "sources_requested": ["OpenAlex"] + (["Semantic Scholar"] if os.environ.get("ENABLE_SEMANTIC_SCHOLAR", "1") != "0" else []),
                "selected": len(selected), "newly_saved": saved, "selected_dois": [p.doi for p in selected if p.doi],
                "selected_titles": [p.title for p in selected], "region": plan.region,
                "screening": "LLM topic-concept screening against title/abstract; automated selection, not independent human screening"}),
        )
    return SearchResponse(project_id=project_id, query_used=query, found=len(selected), saved=saved, papers=selected)


@router.get("/{project_id}", response_model=list[Paper])
async def list_papers(project_id: int) -> list[Paper]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM papers WHERE project_id = $1 "
            "ORDER BY publication_date DESC NULLS LAST, year DESC NULLS LAST, citation_count DESC;", project_id)
    return [Paper(
        id=r["id"], project_id=r["project_id"], title=r["title"],
        authors=json.loads(r["authors"]) if isinstance(r["authors"], str) else r["authors"],
        year=r["year"], publication_date=r["publication_date"], doi=r["doi"],
        abstract=r["abstract"], venue=r["venue"], url=r["url"], source=r["source"],
        citation_count=r["citation_count"], pdf_url=r["pdf_url"])
        for r in rows]
