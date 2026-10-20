"""
Nguồn thứ 2 cho Literature Search: Semantic Scholar Graph API.
Có abstract + link PDF open-access (openAccessPdf). Key tuỳ chọn: SEMANTIC_SCHOLAR_API_KEY
(không key thì bị giới hạn tốc độ chung — hàm này KHÔNG raise, lỗi thì trả [] để OpenAlex vẫn dùng được).
"""

import os
import logging
from typing import Optional

import httpx

from app.models import Paper

logger = logging.getLogger(__name__)

S2_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_FIELDS = "title,abstract,year,venue,externalIds,citationCount,openAccessPdf,authors,url"


def _parse(raw: dict, project_id: int) -> Optional[Paper]:
    title = raw.get("title")
    if not title:
        return None
    doi = (raw.get("externalIds") or {}).get("DOI")
    return Paper(
        project_id=project_id,
        title=title,
        authors=[a["name"] for a in raw.get("authors") or [] if a.get("name")],
        year=raw.get("year"),
        doi=doi.lower() if doi else None,
        abstract=raw.get("abstract"),
        venue=raw.get("venue") or None,
        url=raw.get("url"),
        source="semantic_scholar",
        citation_count=raw.get("citationCount") or 0,
        pdf_url=(raw.get("openAccessPdf") or {}).get("url"),
    )


async def search_semantic_scholar(
    query: str, project_id: int, date_range: Optional[str] = None, limit: int = 50
) -> list[Paper]:
    params = {"query": query, "limit": min(max(limit, 1), 100), "fields": _FIELDS}
    if date_range and "-" in date_range:
        params["year"] = date_range.replace(" ", "")
    headers = {}
    if os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
        headers["x-api-key"] = os.environ["SEMANTIC_SCHOLAR_API_KEY"]
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
            resp = await client.get(S2_URL, params=params)
        if resp.status_code != 200:
            logger.warning("Semantic Scholar trả HTTP %s — bỏ qua nguồn này.", resp.status_code)
            return []
        data = resp.json().get("data") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lỗi gọi Semantic Scholar: %s — bỏ qua nguồn này.", exc)
        return []
    return [p for p in (_parse(r, project_id) for r in data) if p]
