"""Crossref Works search adapter for journal and conference metadata."""
import html
import os
import re
from datetime import date

import httpx

from app.models import Paper

_URL = "https://api.crossref.org/works"


def _year(item: dict) -> int | None:
    for key in ("published", "published-print", "published-online", "issued", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [])
        if parts and parts[0] and isinstance(parts[0][0], int):
            return parts[0][0]
    return None


def _abstract(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"</?(?:jats:)?[^>]+>", " ", value)
    value = html.unescape(re.sub(r"\s+", " ", value)).strip()
    return value or None


def _parse(item: dict, project_id: int) -> Paper | None:
    title = next((t.strip() for t in item.get("title", []) if t and t.strip()), None)
    if not title:
        return None
    doi = (item.get("DOI") or "").strip().lower() or None
    authors = [" ".join(filter(None, [a.get("given"), a.get("family")]))
               for a in item.get("author", [])]
    links = item.get("link") or []
    pdf_url = next((link.get("URL") for link in links
                    if "pdf" in (link.get("content-type") or "").lower()), None)
    date_parts = next((item.get(key, {}).get("date-parts", [[]])[0]
                       for key in ("published", "published-print", "published-online", "issued")
                       if item.get(key, {}).get("date-parts")), [])
    publication_date = "-".join(str(part).zfill(2) if index else str(part)
                                 for index, part in enumerate(date_parts[:3])) or None
    return Paper(
        project_id=project_id,
        title=title,
        authors=[name for name in authors if name],
        year=_year(item),
        publication_date=publication_date,
        doi=doi,
        abstract=_abstract(item.get("abstract")),
        venue=(item.get("container-title") or [None])[0],
        url=f"https://doi.org/{doi}" if doi else item.get("URL"),
        source="crossref",
        citation_count=item.get("is-referenced-by-count") or 0,
        pdf_url=pdf_url,
    )


async def search_crossref(
    query: str, project_id: int, from_year: int, to_date: date, rows: int = 100
) -> list[Paper]:
    params = {
        "query.bibliographic": query,
        "rows": min(max(rows, 1), 200),
        "filter": f"from-pub-date:{from_year}-01-01,until-pub-date:{to_date.isoformat()}",
        "select": "DOI,title,author,published,published-print,published-online,issued,created,abstract,container-title,URL,link,is-referenced-by-count",
    }
    email = os.environ.get("CROSSREF_POLITE_EMAIL", "").strip()
    headers = {"User-Agent": f"HTTM Academic Research (mailto:{email})" if email else "HTTM Academic Research"}
    async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
        response = await client.get(_URL, params=params)
        response.raise_for_status()
    items = response.json().get("message", {}).get("items", [])
    return [paper for paper in (_parse(item, project_id) for item in items) if paper]
