from copy import deepcopy

import pytest
from fastapi import HTTPException

from app.autonomous.models import Decision
from app.autonomous.state import decide, finish_stage, stages_for
from app.autonomous import literature


def review_run():
    return {"id": 1, "project_id": 1, "version": 3, "revision": 1,
            "status": "awaiting_research_approval", "stage": "research_plan", "events": [],
            "artifacts": {"settings": {"paper_type": "review", "authors": []}, "topic": "Climate",
                          "research_plan": {"question": "What does literature show?"},
                          "review_corpus": {"papers": []}}}


def test_review_approval_skips_dataset_and_empirical_stages():
    run = review_run()
    accepted = decide(run, Decision(expected_version=3, action="approve"))
    assert accepted["stage"] == "manuscript"
    assert "data_collection" not in stages_for(run)
    assert finish_stage(accepted, {"title": "Review"})["stage"] == "verification"
    assert run["stage"] == "research_plan"


def test_review_final_gate_and_revision_keep_frozen_corpus():
    run = review_run()
    run.update(stage="verification", status="awaiting_final_approval")
    run["artifacts"].update(verification={"ok": False}, review_document={"old": True})
    with pytest.raises(HTTPException):
        decide(run, Decision(expected_version=3, action="approve"))
    revised = decide(run, Decision(expected_version=3, action="revise", notes="Clarify limitations"))
    assert revised["stage"] == "manuscript"
    assert revised["artifacts"]["review_corpus"] == run["artifacts"]["review_corpus"]
    assert "review_document" not in revised["artifacts"]
    assert "verification" not in revised["artifacts"]
    with pytest.raises(HTTPException):
        decide(run, Decision(expected_version=3, action="revise", notes="Retry", restart_from="design"))


@pytest.mark.asyncio
async def test_repair_updates_export_snapshot_and_display(monkeypatch):
    run = review_run()
    original = {"title": "Review", "keywords": [], "papers": [],
                "sections": [{"key": "abstract", "title": "Abstract", "paragraphs": [{"text": "old"}]}]}
    run["artifacts"]["review_document"] = original
    async def repair(document):
        revised = deepcopy(document)
        revised["sections"][0]["paragraphs"][0]["text"] = "Repaired evidence-based abstract"
        return revised, {"ok": True, "errors": [], "semantic_decisions": []}
    monkeypatch.setattr(literature, "repair_and_validate", repair)
    result = await literature.verify(run)
    assert result["ok"]
    assert run["artifacts"]["manuscript"]["abstract"] == "Repaired evidence-based abstract"
    assert run["artifacts"]["review_document"]["sections"][0]["paragraphs"][0]["text"] == run["artifacts"]["manuscript"]["abstract"]
