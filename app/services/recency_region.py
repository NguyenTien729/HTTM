"""
Ràng buộc cho bước Literature Search:
  1. MỚI NHẤT: cửa sổ thời gian tính theo ngày hôm nay (không tin date_range do LLM sinh,
     vì LLM không biết hôm nay là ngày nào), rồi xếp bài mới nhất lên đầu.
  2. ĐÚNG KHU VỰC: nếu đề tài chỉ định quốc gia/khu vực, paper phải thực sự nhắc tới khu vực đó
     (title/abstract/venue). Đúng chủ đề nhưng không nhắc khu vực => loại.
"""

import os
import re
from datetime import date
from typing import Optional

from app.models import Paper, ResearchPlan

DEFAULT_RECENT_YEARS = 3
# Nếu sau khi lọc còn thiếu paper, mở rộng cửa sổ dần: 3 -> 5 -> 10 năm.
WIDEN_STEPS = (3, 5, 10)


def recent_years() -> int:
    try:
        return max(1, int(os.environ.get("SEARCH_RECENT_YEARS", DEFAULT_RECENT_YEARS)))
    except ValueError:
        return DEFAULT_RECENT_YEARS


def plan_start_year(date_range: Optional[str]) -> Optional[int]:
    m = re.match(r"^\s*(\d{4})\s*-\s*(\d{4})\s*$", date_range or "")
    return int(m.group(1)) if m else None


def window_steps(date_range: Optional[str], today: Optional[date] = None) -> list[int]:
    """Danh sách start_year thử lần lượt (mới -> cũ). Không bao giờ cũ hơn start của plan
    (nếu plan có) hoặc 10 năm trước."""
    today = today or date.today()
    floor = plan_start_year(date_range) or (today.year - 10)
    base = recent_years()
    spans = sorted({base, *[w for w in WIDEN_STEPS if w > base]})
    starts: list[int] = []
    for span in spans:
        start = max(today.year - span + 1, floor)
        if start not in starts:
            starts.append(start)
    return starts


def region_terms(plan: ResearchPlan) -> list[str]:
    terms = [t.strip() for t in plan.region_terms if t and t.strip()]
    if plan.region and plan.region.strip() and plan.region.strip().lower() not in {t.lower() for t in terms}:
        terms.insert(0, plan.region.strip())
    return terms


def _term_regex(terms: list[str]) -> Optional[re.Pattern]:
    if not terms:
        return None
    alts = "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z]){alts}(?![A-Za-z])", re.IGNORECASE)


def matches_region(paper: Paper, terms: list[str]) -> bool:
    """True nếu paper nhắc tới khu vực yêu cầu. Không có terms => đề tài không giới hạn khu vực."""
    rx = _term_regex(terms)
    if rx is None:
        return True
    text = " ".join(filter(None, [paper.title, paper.abstract, paper.venue]))
    return bool(rx.search(text))


def _sort_key(p: Paper):
    # Có ngày đầy đủ > chỉ có năm > không có gì; trong cùng ngày thì nhiều trích dẫn hơn lên trước.
    date_str = p.publication_date or (f"{p.year}-00-00" if p.year else "")
    return (date_str, p.citation_count)


def filter_and_rank(
    papers: list[Paper], plan: ResearchPlan, start_year: int, limit: int
) -> list[Paper]:
    terms = region_terms(plan)
    kept = [
        p for p in papers
        if (p.year is None or p.year >= start_year) and matches_region(p, terms)
    ]
    kept.sort(key=_sort_key, reverse=True)
    return kept[:limit]
