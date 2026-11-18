"""Multi-query academic literature search with auditable screening."""
import asyncio
import json
import logging
import os
import re
from datetime import date

from fastapi import APIRouter, HTTPException

from app.db import get_pool
from app.models import Paper, ResearchPlan, SearchResponse
from app.services.openalex_client import search_openalex
from app.services.paper_merge import merge_papers
from app.services.crossref_client import search_crossref
from app.services.query_builder import build_queries_from_plan
from app.services.recency_region import matches_region, region_terms
from app.services.relevance_filter import extract_topic_concepts, screen_papers
from app.services.semantic_scholar_client import search_semantic_scholar

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/search", tags=["literature-search"])


async def _load_plan(project_id: int) -> ResearchPlan:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT research_plan FROM research_projects WHERE id = $1;", project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy research project.")
    value = row["research_plan"]
    return ResearchPlan.model_validate(json.loads(value) if isinstance(value, str) else value)


async def _save_papers(papers: list[Paper]) -> int:
    if not papers:
        return 0
    saved_count = 0
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            existing_rows = await conn.fetch("SELECT doi, title FROM papers WHERE project_id=$1", papers[0].project_id)
            existing_keys = {
                "doi:" + row["doi"].lower().strip() if row["doi"]
                else "title:" + re.sub(r"[^a-z0-9]+", "", row["title"].lower())
                for row in existing_rows
            }
            for paper in papers:
                key = _key(paper)
                if not paper.doi and key in existing_keys:
                    continue
                row = await conn.fetchrow(
                    """INSERT INTO papers
                        (project_id, title, authors, year, doi, abstract, venue, url, source, citation_count, pdf_url, publication_date)
                    VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    ON CONFLICT (project_id, doi) WHERE doi IS NOT NULL DO UPDATE SET
                        pdf_url = COALESCE(papers.pdf_url, EXCLUDED.pdf_url),
                        abstract = COALESCE(NULLIF(papers.abstract, ''), EXCLUDED.abstract),
                        authors = CASE WHEN papers.authors = '[]'::jsonb THEN EXCLUDED.authors ELSE papers.authors END,
                        year = COALESCE(papers.year, EXCLUDED.year),
                        publication_date = COALESCE(papers.publication_date, EXCLUDED.publication_date),
                        venue = COALESCE(papers.venue, EXCLUDED.venue),
                        url = COALESCE(papers.url, EXCLUDED.url),
                        source = EXCLUDED.source,
                        citation_count = GREATEST(papers.citation_count, EXCLUDED.citation_count)
                    RETURNING id, (xmax = 0) AS inserted;""",
                    paper.project_id, paper.title, json.dumps(paper.authors), paper.year,
                    paper.doi, paper.abstract, paper.venue, paper.url, paper.source,
                    paper.citation_count, paper.pdf_url, paper.publication_date,
                )
                if row is not None and row["inserted"]:
                    saved_count += 1
                existing_keys.add(key)
    return saved_count


async def _search_once(
    plan: ResearchPlan, project_id: int, queries: list[str], start_year: int, today: date,
    include_crossref: bool = True,
) -> tuple[list[Paper], list[dict]]:
    target = plan.search_strategy.target_paper_count
    pool_size = min(max(target * 2, 60), 200)
    labels = [("OpenAlex", query) for query in queries]
    tasks = [search_openalex(
        query=query, project_id=project_id, per_page=pool_size,
        from_date=f"{start_year}-01-01", to_date=today.isoformat(),
    ) for query in queries]
    s2_queries = ([plan.topic.strip()] + [k.strip() for k in plan.keywords[:2] if k.strip()]
                  if os.environ.get("ENABLE_SEMANTIC_SCHOLAR", "1") != "0" else [])
    labels.extend(("Semantic Scholar", query) for query in s2_queries)
    tasks.extend(search_semantic_scholar(
        query=query, project_id=project_id, limit=min(pool_size, 100),
        start_year=start_year, end_year=today.year,
    ) for query in s2_queries)
    if include_crossref:
        labels.append(("Crossref", plan.topic))
        tasks.append(search_crossref(plan.topic, project_id, start_year, today, rows=min(pool_size, 200)))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    audit: list[dict] = []
    successful: list[list[Paper]] = []
    for (source, query), result in zip(labels, results):
        if isinstance(result, Exception):
            logger.warning("%s failed for query %r: %s", source, query, result)
            audit.append({"source": source, "query": query, "error": type(result).__name__})
        else:
            successful.append(result)
            audit.append({"source": source, "query": query, "retrieved": len(result)})
    if not successful or (not any(successful) and any(isinstance(result, Exception) for result in results)):
        raise RuntimeError("All literature providers failed")
    return merge_papers(*successful), audit


def _in_window(papers: list[Paper], start_year: int, today: date) -> list[Paper]:
    # Do not hard-filter by geography here: abstracts often omit a study site.
    return [paper for paper in papers
            if (paper.year is None or start_year <= paper.year <= today.year)
            and (not paper.publication_date or str(paper.publication_date) <= today.isoformat())]


def _key(paper: Paper) -> str:
    return paper.doi.lower().strip() if paper.doi else re.sub(r"\W+", "", paper.title.casefold())


@router.post("/{project_id}", response_model=SearchResponse)
async def run_search(project_id: int) -> SearchResponse:
    plan = await _load_plan(project_id)
    queries = build_queries_from_plan(plan)
    target = plan.search_strategy.target_paper_count
    today = date.today()
    try:
        concepts = await extract_topic_concepts(plan)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Cannot extract topic concepts")
        raise HTTPException(status_code=502, detail="Không xác định được các khái niệm của đề tài.") from exc

    # Search the recent window first, then include older foundational work if
    # the requested corpus is not yet full. The planner's wider date range also
    # participates when it starts earlier than these defaults.
    starts = [max(1900, today.year - 4), max(1900, today.year - 9), max(1900, today.year - 19), 1900]
    date_match = re.match(r"^\s*(\d{4})\s*-\s*(\d{4})\s*$", plan.search_strategy.date_range or "")
    if date_match:
        starts.append(max(1900, int(date_match.group(1))))
    starts = sorted(set(starts), reverse=True)

    candidates_seen: dict[str, Paper] = {}
    decisions: dict[str, tuple[Paper, int]] = {}
    windows: list[dict] = []
    provider_audit: list[dict] = []
    selected: list[Paper] = []

    for start_year in starts:
        try:
            provider_result = await _search_once(
                plan, project_id, queries, start_year, today, include_crossref=start_year == starts[0])
            if isinstance(provider_result, tuple):
                raw, source_audit = provider_result
            else:  # Backward-compatible provider fixtures/adapters.
                raw, source_audit = provider_result, [{"source": "provider", "retrieved": len(provider_result)}]
        except Exception as exc:
            logger.exception("Paper providers failed")
            if not candidates_seen:
                raise HTTPException(status_code=502, detail="Không gọi được nguồn tài liệu.") from exc
            continue
        candidates = _in_window(raw, start_year, today)
        provider_audit.extend({"from_year": start_year, **entry} for entry in source_audit)
        terms = region_terms(plan)
        windows.append({
            "from_year": start_year, "to_date": today.isoformat(),
            "retrieved": len(raw), "after_date_filter": len(candidates),
            "region_mentions": sum(matches_region(paper, terms) for paper in candidates) if terms else None,
        })
        for paper in candidates:
            key = _key(paper)
            if key in candidates_seen:
                candidates_seen[key] = merge_papers([candidates_seen[key]], [paper])[0]
            else:
                candidates_seen[key] = paper

        fresh = [paper for key, paper in candidates_seen.items() if key not in decisions]
        if fresh:
            try:
                relevant = await screen_papers(fresh, plan, concepts=concepts)
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("Relevance screening failed")
                raise HTTPException(status_code=502, detail="Lọc độ liên quan thất bại; chưa lưu paper chưa kiểm duyệt.") from exc
            accepted = {_key(paper): (paper, score) for paper, score in relevant}
            # Cache negative decisions too, avoiding repeated LLM calls as the
            # time window expands.
            for key, paper in candidates_seen.items():
                decisions.setdefault(key, accepted.get(key, (paper, -1)))

        ranked = sorted((item for item in decisions.values() if item[1] >= 0), key=lambda item: (
            item[1], item[0].citation_count,
            str(item[0].publication_date or (f"{item[0].year}-00-00" if item[0].year else ""))), reverse=True)
        historical_minimum = min(target // 5, 10)
        historical = [item for item in ranked if item[0].year and item[0].year < today.year - 4]
        recent = [item for item in ranked if not (item[0].year and item[0].year < today.year - 4)]
        historical_count = min(historical_minimum, len(historical), target)
        selected_pairs = recent[:target - historical_count] + historical[:historical_count]
        if len(selected_pairs) < target:
            selected_keys = {_key(paper) for paper, _ in selected_pairs}
            selected_pairs.extend(item for item in ranked if _key(item[0]) not in selected_keys)
        selected = [paper for paper, _ in selected_pairs[:target]]
        logger.info("Search from %s: retrieved=%d candidates=%d screened=%d selected=%d",
                    start_year, len(raw), len(candidates), len(fresh), len(selected))
        if len(selected) >= target and len(historical) >= historical_minimum:
            break

    saved = await _save_papers(selected)
    query = " | ".join(queries)
    audit = {
        "queries": queries,
        "provider_queries": provider_audit,
        "search_date": today.isoformat(),
        "windows": windows,
        "sources_requested": ["OpenAlex", "Crossref"] + (["Semantic Scholar"] if os.environ.get("ENABLE_SEMANTIC_SCHOLAR", "1") != "0" else []),
        "selected": len(selected), "requested": target, "newly_saved": saved,
        "selected_dois": [paper.doi for paper in selected if paper.doi],
        "selected_titles": [paper.title for paper in selected], "region": plan.region,
        "screening": "Recall-oriented semantic screen grounded in exact title/abstract excerpts; automated selection, not independent human screening",
    }
    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO literature_search_runs(project_id, audit) VALUES ($1, $2::jsonb)",
            project_id, json.dumps(audit),
        )
    return SearchResponse(project_id=project_id, query_used=query, found=len(selected), saved=saved,
                          papers=selected, search_audit=audit)


@router.get("/{project_id}", response_model=list[Paper])
async def list_papers(project_id: int) -> list[Paper]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM papers WHERE project_id = $1 "
            "ORDER BY publication_date DESC NULLS LAST, year DESC NULLS LAST, citation_count DESC;", project_id)
    return [Paper(
        id=row["id"], project_id=row["project_id"], title=row["title"],
        authors=json.loads(row["authors"]) if isinstance(row["authors"], str) else row["authors"],
        year=row["year"], publication_date=row["publication_date"], doi=row["doi"],
        abstract=row["abstract"], venue=row["venue"], url=row["url"], source=row["source"],
        citation_count=row["citation_count"], pdf_url=row["pdf_url"])
        for row in rows]
