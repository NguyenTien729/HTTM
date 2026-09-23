"""
Query Generator: Research Plan → search query string.

OpenAlex hỗ trợ boolean operators (AND/OR/NOT) và dấu ngoặc kép cho phrase
trong param `search`. Ta build query dạng:

    ("keyword1" OR "synonym1a" OR "synonym1b") OR ("keyword2" OR "synonym2a")

Mỗi keyword và các synonym của nó nằm trong 1 nhóm OR — nhóm với nhóm cũng OR
với nhau (broad search ở bước lấy paper, việc relevance ranking/filter tinh hơn
để module sau xử lý, không cố gắng làm query quá hẹp ngay từ đầu).
"""

from app.models import ResearchPlan


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
        return plan.topic

    return " OR ".join(groups)