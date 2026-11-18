import pytest
from app.services import review_manuscript as review
from app.autonomous.state import decide
from app.autonomous.models import Decision
from tests.test_autonomous_review import review_run


@pytest.mark.asyncio
async def test_restored_excerpt_does_not_bypass_semantic_rejection(monkeypatch):
    source = "The study reports an association, not a causal effect."
    document = {"evidence_sources": [{"source_id": "s1", "paper_id": 1, "text": source}],
                "sections": [{"key": "synthesis", "paragraphs": [{"text": "A causal effect is established [1].",
                "evidence": [{"source_id": "s1", "paper_id": 1, "quote": "fabricated excerpt"}]}]}]}
    async def reject(current, report):
        assert current["sections"][0]["paragraphs"][0]["evidence"][0]["quote"] == source
        return {"ok": False, "errors": [], "semantic_decisions": [{"paragraph_id": "synthesis:0", "supported": False}]}
    monkeypatch.setattr(review, "semantic_validation", reject)
    updated, report = await review.repair_and_validate(document, max_rounds=0)
    assert not report["ok"]
    assert report["source_excerpt_repairs"][0]["before"] == "fabricated excerpt"
    assert document["sections"][0]["paragraphs"][0]["evidence"][0]["quote"] == "fabricated excerpt"


def test_verification_retry_preserves_manuscript():
    run = review_run()
    run.update(status="awaiting_final_approval", stage="verification")
    run["artifacts"].update(review_document={"title": "Saved"}, manuscript={"title": "Saved"}, verification={"ok": False})
    revised = decide(run, Decision(expected_version=3, action="revise", notes="Repair", restart_from="verification"))
    assert revised["artifacts"]["review_document"] == {"title": "Saved"}
    assert revised["stage"] == "verification"
    assert "verification" not in revised["artifacts"]
