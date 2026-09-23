"""
Client gọi OpenAlex (https://openalex.org) — nguồn free, không cần API key.

Không nên để LLM tự "nhớ" paper (dễ hallucinate DOI/title). Toàn bộ metadata
ở module này lấy trực tiếp từ OpenAlex, không qua LLM.
"""

import os
import re
import logging
from typing import Optional

import httpx

from app.models import Paper

logger = logging.getLogger(__name__)

OPENALEX_BASE_URL = "https://api.openalex.org/works"


def _parse_date_range(date_range: str) -> tuple[Optional[str], Optional[str]]:
    """'2018-2026' -> ('2018-01-01', '2026-12-31'). Trả (None, None) nếu không parse được."""
    match = re.match(r"^\s*(\d{4})\s*-\s*(\d{4})\s*$", date_range or "")
    if not match:
        return None, None
    start_year, end_year = match.groups()
    return f"{start_year}-01-01", f"{end_year}-12-31"


def _reconstruct_abstract(inverted_index: Optional[dict]) -> Optional[str]:
    """OpenAlex trả abstract dạng inverted index ({word: [vị trí,...]}) để đỡ vấn đề bản quyền.
    Phải dựng lại thành câu văn bình thường theo đúng vị trí."""
    if not inverted_index:
        return None
    positions: dict[int, str] = {}
    for word, idxs in inverted_index.items():
        for idx in idxs:
            positions[idx] = word
    if not positions:
        return None
    ordered = [positions[i] for i in sorted(positions.keys())]
    return " ".join(ordered)


def _parse_work(raw: dict, project_id: int) -> Paper:
    """Map 1 object 'work' của OpenAlex sang Paper schema nội bộ."""
    authors = [
        a.get("author", {}).get("display_name")
        for a in raw.get("authorships", [])
        if a.get("author", {}).get("display_name")
    ]

    primary_location = raw.get("primary_location") or {}
    source_info = primary_location.get("source") or {}

    doi = raw.get("doi")
    if doi:
        doi = doi.replace("https://doi.org/", "")

    return Paper(
        project_id=project_id,
        title=raw.get("title") or "(không có tiêu đề)",
        authors=authors,
        year=raw.get("publication_year"),
        doi=doi,
        abstract=_reconstruct_abstract(raw.get("abstract_inverted_index")),
        venue=source_info.get("display_name"),
        url=primary_location.get("landing_page_url") or raw.get("id"),
        source="openalex",
        citation_count=raw.get("cited_by_count") or 0,
    )


async def search_openalex(
    query: str,
    project_id: int,
    date_range: Optional[str] = None,
    per_page: int = 50,
) -> list[Paper]:
    """Gọi OpenAlex /works, trả về list Paper đã parse (chưa dedup, chưa lưu DB)."""
    params = {
        "search": query,
        "per-page": min(max(per_page, 1), 200),  # OpenAlex giới hạn tối đa 200/trang
    }

    from_date, to_date = _parse_date_range(date_range or "")
    if from_date and to_date:
        params["filter"] = f"from_publication_date:{from_date},to_publication_date:{to_date}"

    # OpenAlex khuyến nghị thêm mailto để vào "polite pool" (nhanh + ổn định hơn).
    mailto = os.environ.get("OPENALEX_MAILTO")
    if mailto:
        params["mailto"] = mailto

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(OPENALEX_BASE_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Lỗi khi gọi OpenAlex API: %s", exc)
            raise

    data = resp.json()
    results = data.get("results", [])
    return [_parse_work(raw, project_id) for raw in results]