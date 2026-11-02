"""
Query Generator: Research Plan → search query string.

OpenAlex hỗ trợ boolean operators (AND/OR/NOT) và dấu ngoặc kép cho phrase
trong param `search`. Ta build query dạng:

    (("keyword1" OR "synonym1a") OR ("keyword2" OR "synonym2a")) AND ("Vietnam" OR "Vietnamese")

Phần chủ đề: mỗi keyword + synonym nằm trong 1 nhóm OR, nhóm với nhóm cũng OR.
Phần khu vực (nếu đề tài có chỉ định): AND bắt buộc, để paper phải nhắc tới khu vực đó.
"""

from app.models import ResearchPlan
from app.services.recency_region import region_terms


def build_query_from_plan(plan: ResearchPlan) -> str:
    groups: list[str] = []

    for kw in plan.keywords:
        terms = [f'"{kw}"']
        # Gộp synonym tương ứng với keyword này nếu có khai báo trong plan
        matching = next((s for s in plan.synonyms if s.keyword == kw), None)
        if matching:
            terms.extend(f'"{syn}"' for syn in matching.synonyms)
        groups.append("(" + " OR ".join(terms) + ")")

    if not groups:
        # Fallback an toàn: chưa có keyword nào thì search luôn theo topic
        topic_query = plan.topic
    else:
        topic_query = " OR ".join(groups)

    # Đề tài có chỉ định quốc gia/khu vực => BẮT BUỘC paper nhắc tới khu vực đó (AND),
    # chứ không chỉ đúng chủ đề. Bước lọc sau (matches_region) vẫn kiểm tra lại.
    terms = region_terms(plan)
    if terms:
        region_group = " OR ".join(f'"{t}"' for t in terms)
        return f"({topic_query}) AND ({region_group})"
    return topic_query
