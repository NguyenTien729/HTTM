"""DeepSeek sometimes returns null for an absent quote; that must not fail the whole search."""
import os

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

from app.models import Paper
from app.services.relevance_filter import RelevanceBatch, _valid_quote


def test_null_quotes_are_read_as_empty_and_still_count_as_unsupported():
    batch = RelevanceBatch.model_validate({"decisions": [{
        "candidate_id": 1, "direct_relationship": False, "relationship_quote": None, "score": 10, "reason": None,
        "concept_evidence": [{"concept": "education", "quote": None, "supported": False}]}]})
    decision = batch.decisions[0]
    assert decision.concept_evidence[0].quote == "" and decision.relationship_quote == "" and decision.reason == ""
    paper = Paper(project_id=1, title="Returns to schooling", abstract="Education raises wages.", source="openalex")
    assert not _valid_quote(decision.relationship_quote, paper)
