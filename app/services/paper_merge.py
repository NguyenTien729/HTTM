"""Gộp paper từ nhiều nguồn, khử trùng theo DOI (hoặc tiêu đề chuẩn hoá nếu không có DOI)."""

import re
from app.models import Paper


def _key(p: Paper) -> str:
    if p.doi:
        return "doi:" + p.doi.lower().strip()
    return "title:" + re.sub(r"[^a-z0-9]+", "", p.title.lower())


def merge_papers(*lists: list[Paper]) -> list[Paper]:
    """Giữ bản xuất hiện trước; bổ sung field còn thiếu (abstract, pdf_url, doi...) từ bản trùng."""
    merged: dict[str, Paper] = {}
    for lst in lists:
        for p in lst:
            k = _key(p)
            if k not in merged:
                merged[k] = p
                continue
            base = merged[k]
            for field in ("abstract", "pdf_url", "doi", "venue", "year"):
                if not getattr(base, field) and getattr(p, field):
                    setattr(base, field, getattr(p, field))
            base.citation_count = max(base.citation_count, p.citation_count)
    return list(merged.values())
