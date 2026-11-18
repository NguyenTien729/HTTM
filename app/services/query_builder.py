"""Build a small, auditable set of complementary academic search queries.

Search is recall-oriented: topic, keyword, and synonym queries are issued
separately so a narrow Boolean query cannot hide useful papers. Precision is
handled after retrieval by the evidence-based screen.
"""
import re
from app.models import ResearchPlan


def _quote(value: str) -> str:
    return '"' + re.sub(r'["\\\r\n]+', ' ', value).strip() + '"'


def build_query_from_plan(plan: ResearchPlan) -> str:
    """Backward-compatible broad query builder."""
    return build_queries_from_plan(plan)[0]


def build_queries_from_plan(plan: ResearchPlan, limit: int = 6) -> list[str]:
    """Return complementary queries, from literal topic to concept expansions.

    Do not require geography or every planner keyword in the database query;
    metadata often omits these even when the paper is relevant. The later
    screen sees the user's complete topic and region.
    """
    queries: list[str] = []

    def add(value: str) -> None:
        value = re.sub(r"\s+", " ", value).strip()
        if value and value.casefold() not in {q.casefold() for q in queries}:
            queries.append(value)

    topic = plan.topic.strip()
    add(_quote(topic) if len(topic.split()) <= 5 else topic)

    pairs = []
    clean_keywords = [k.strip() for k in plan.keywords if k and k.strip()]
    for kw in clean_keywords[:4]:
        matching = next((s for s in plan.synonyms if s.keyword.casefold() == kw.casefold()), None)
        variants = list(dict.fromkeys([kw] + (matching.synonyms[:3] if matching else [])))
        add(" OR ".join(_quote(v) for v in variants if v.strip()))
        pairs.append(kw)
        if len(queries) >= limit:
            break

    # Pair the first two central concepts as a fallback, without demanding all
    # of the planner's possibly over-specific keywords at once.
    if len(pairs) >= 2:
        add(f'{_quote(pairs[0])} AND {_quote(pairs[1])}')
    return queries[:limit]
