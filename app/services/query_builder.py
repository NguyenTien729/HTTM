"""Conservative concept-aware query generation for OpenAlex.

Use the original topic as an anchor, with a small set of planner keywords as
optional expansion. Final precision is enforced by the relevance screen.
"""
import re
from app.models import ResearchPlan
from app.services.recency_region import region_terms


def _quote(value: str) -> str:
    return '"' + re.sub(r'["\\\r\n]+', ' ', value).strip() + '"'


def build_query_from_plan(plan: ResearchPlan) -> str:
    # Use two planner concepts together instead of OR-ing every broad keyword.
    # Synonyms stay inside their own OR group. Avoid forcing all planner terms.
    groups = []
    for kw in plan.keywords:
        kw = kw.strip()
        if not kw:
            continue
        matching = next((s for s in plan.synonyms if s.keyword.casefold() == kw.casefold()), None)
        variants = [kw] + (matching.synonyms[:3] if matching else [])
        group = "(" + " OR ".join(_quote(v) for v in dict.fromkeys(variants) if v.strip()) + ")"
        if group not in groups:
            groups.append(group)
        if len(groups) == 2:
            break
    query = " AND ".join(groups) if groups else _quote(plan.topic.strip())
    terms = region_terms(plan)
    if terms:
        region_group = " OR ".join(_quote(t) for t in terms)
        query = f"({query}) AND ({region_group})"
    return query
