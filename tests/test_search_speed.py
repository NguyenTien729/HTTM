"""Literature screening runs batches concurrently and screens the best candidates first."""
import asyncio
import os

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

from app.models import Paper
from app.services import relevance_filter as rf


class Plan:
    topic = "Returns to schooling and hourly wages"
    region = "United States"
    keywords = ["education", "wage"]


def paper(i, title, abstract=None):
    return Paper(project_id=1, title=title, abstract=abstract, source="openalex", doi=f"10.1/{i}")


async def test_batches_are_screened_concurrently_and_keep_order(monkeypatch):
    running, peak = 0, 0

    async def llm(prompt, schema):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        ids = [int(line.split("=")[1]) for line in prompt.splitlines() if line.startswith("CANDIDATE_ID=")]
        return rf.RelevanceBatch(decisions=[{"candidate_id": i, "concept_evidence": [], "direct_relationship": True,
                                             "relationship_quote": "", "score": 90, "reason": "ok"} for i in ids])

    monkeypatch.setattr(rf, "generate_structured", llm)
    monkeypatch.setattr(rf, "_approve", lambda d, p, c, t: True)
    papers = [paper(i, f"Paper {i}") for i in range(40)]
    concepts = rf.TopicConcepts(concepts=["schooling", "wages"], scope_rule="x")
    result = await rf.screen_papers(papers, Plan(), concepts=concepts, threshold=0, batch_size=10)
    assert [p.title for p, _ in result] == [p.title for p in papers]
    assert peak == 4  # default SEARCH_SCREEN_CONCURRENCY


def test_prescreen_puts_topical_candidates_first_without_dropping_any():
    concepts = rf.TopicConcepts(concepts=["schooling", "wages"], scope_rule="x")
    papers = [paper(1, "Deep learning for images"), paper(2, "Schooling and wages", "Returns to education raise wages."),
              paper(3, "Wages in manufacturing")]
    ranked = rf.prescreen_rank(papers, Plan(), concepts)
    assert ranked[0].title == "Schooling and wages" and ranked[-1].title == "Deep learning for images"
    assert len(ranked) == 3
