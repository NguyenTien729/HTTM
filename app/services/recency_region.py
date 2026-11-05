"""
Ràng buộc cho bước Literature Search:
  1. MỚI: chỉ lấy paper trong vòng RECENT_YEARS năm gần đây (mặc định 5), tính theo ngày hôm nay
     (không tin date_range do LLM sinh, vì LLM không biết hôm nay là ngày nào). Đây là ràng buộc
     CỨNG: không tự lùi xa hơn khi thiếu bài; paper không rõ năm xuất bản bị loại.
  2. CÓ CITATION: paper phải đạt ngưỡng citation tối thiểu, và kết quả được xếp theo số citation
     giảm dần. Vì paper mới thì chưa kịp được trích dẫn nhiều, ngưỡng tính theo
     "citation / số năm kể từ khi xuất bản" (citations per year) chứ không phải số citation thô.
     Ngưỡng là ràng buộc MỀM: nếu thiếu bài thì nới dần (xem citation_tiers) nhưng không bao giờ
     nhận paper 0 citation.
  3. ĐÚNG KHU VỰC: nếu đề tài chỉ định quốc gia/khu vực, paper phải thực sự nhắc tới khu vực đó
     (title/abstract/venue). Đúng chủ đề nhưng không nhắc khu vực => loại.

Cấu hình qua biến môi trường:
  SEARCH_RECENT_YEARS               số năm gần đây được phép (mặc định 5; năm hiện tại tính là năm 1)
  SEARCH_MIN_CITATIONS_PER_YEAR     ngưỡng nghiêm nhất (mặc định 3; đặt 0 để tắt lọc citation)
"""

import os
import re
from datetime import date
from typing import Optional

from app.models import Paper, ResearchPlan

DEFAULT_RECENT_YEARS = 5
# Ngưỡng citation/năm nghiêm nhất. Ví dụ với mặc định 3: bài năm nay cần >= 3 citation,
# bài cách đây 5 năm cần >= 15 citation.
DEFAULT_MIN_CITATIONS_PER_YEAR = 3.0
# Các mức nới ngưỡng khi thiếu bài, tính theo tỉ lệ so với ngưỡng nghiêm nhất: 100% -> 50% -> ~17%.
CITATION_TIER_RATIOS = (1.0, 0.5, 1 / 6)


def recent_years() -> int:
    try:
        return max(1, int(os.environ.get("SEARCH_RECENT_YEARS", DEFAULT_RECENT_YEARS)))
    except ValueError:
        return DEFAULT_RECENT_YEARS


def min_citations_per_year() -> float:
    try:
        return max(0.0, float(os.environ.get("SEARCH_MIN_CITATIONS_PER_YEAR", DEFAULT_MIN_CITATIONS_PER_YEAR)))
    except ValueError:
        return DEFAULT_MIN_CITATIONS_PER_YEAR


def citation_tiers() -> list[float]:
    """Các ngưỡng citation/năm thử lần lượt từ nghiêm -> lỏng. [0.0] nếu tắt lọc citation."""
    strict = min_citations_per_year()
    if strict <= 0:
        return [0.0]
    return [strict * r for r in CITATION_TIER_RATIOS]


def min_citation_floor() -> int:
    """Số citation tối thiểu tuyệt đối để đẩy xuống phía nguồn (API) lọc sớm: 1 khi có bật lọc citation."""
    return 1 if min_citations_per_year() > 0 else 0


def plan_start_year(date_range: Optional[str]) -> Optional[int]:
    m = re.match(r"^\s*(\d{4})\s*-\s*(\d{4})\s*$", date_range or "")
    return int(m.group(1)) if m else None


def window_steps(date_range: Optional[str] = None, today: Optional[date] = None) -> list[int]:
    """Danh sách start_year để tìm. Chỉ có MỘT mốc: đầu năm của (RECENT_YEARS - 1) năm trước,
    ví dụ hôm nay 2026 và RECENT_YEARS=5 -> 2022 (tức 2022..2026).

    date_range của plan (do LLM sinh) được cố ý bỏ qua: nó có thể quá hẹp hoặc quá cũ so với
    yêu cầu "trong vòng N năm gần đây". Tham số giữ lại để không đổi chữ ký hàm."""
    today = today or date.today()
    return [today.year - recent_years() + 1]


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


def citations_per_year(p: Paper, today: Optional[date] = None) -> float:
    """Citation trung bình mỗi năm. Năm xuất bản tính là năm thứ 1 (tối thiểu chia cho 1)."""
    today = today or date.today()
    if p.year is None:
        return 0.0
    age = max(today.year - p.year + 1, 1)
    return p.citation_count / age


def _sort_key(p: Paper):
    # Nhiều citation nhất lên đầu; cùng citation thì bài mới hơn lên trước
    # (có ngày đầy đủ > chỉ có năm).
    date_str = p.publication_date or (f"{p.year}-00-00" if p.year else "")
    return (p.citation_count, date_str)


def filter_and_rank_with_tier(
    papers: list[Paper], plan: ResearchPlan, start_year: int, limit: int, today: Optional[date] = None
) -> tuple[list[Paper], float]:
    """Lọc (đủ mới + đúng khu vực + đủ citation) rồi xếp theo citation giảm dần, cắt `limit` bài.

    Trả (danh sách, ngưỡng citation/năm đã dùng). Thử ngưỡng nghiêm nhất trước; chỉ khi chưa đủ
    `limit` bài mới nới sang ngưỡng thấp hơn, và dừng ở ngưỡng thấp nhất (không nhận bài 0 citation
    khi lọc citation đang bật)."""
    today = today or date.today()
    terms = region_terms(plan)
    base = [
        p for p in papers
        if p.year is not None and start_year <= p.year <= today.year and matches_region(p, terms)
    ]

    tiers = citation_tiers()
    chosen: list[Paper] = []
    used = tiers[-1]
    for tier in tiers:
        chosen = [p for p in base if p.citation_count > 0 or tier <= 0]
        chosen = [p for p in chosen if citations_per_year(p, today) >= tier]
        used = tier
        if len(chosen) >= limit:
            break
    chosen.sort(key=_sort_key, reverse=True)
    return chosen[:limit], used


def filter_and_rank(
    papers: list[Paper], plan: ResearchPlan, start_year: int, limit: int, today: Optional[date] = None
) -> list[Paper]:
    return filter_and_rank_with_tier(papers, plan, start_year, limit, today)[0]
