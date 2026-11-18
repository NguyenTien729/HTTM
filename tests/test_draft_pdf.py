"""Draft PDF: available before approval, clearly labelled, built from run artifacts."""
import os

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

import fitz  # pymupdf
import httpx
import pytest
from fastapi import FastAPI

from app.autonomous import router as autonomous_router, store
from tests.test_export_paper import sample_data


def empirical_run(status="awaiting_final_approval"):
    data = sample_data()
    return {"id": 7, "status": status, "version": 3, "artifacts": {
        "settings": {"paper_type": "original_research", "authors": []}, "topic": data["project_topic"],
        "research_plan": {"question": data["rq"]["rq_text"], "hypotheses": data["rq"].get("hypotheses", []),
                          "variables": data["rq"].get("variables", []), "gap": (data.get("gap") or {}).get("description", "")},
        "introduction": {"introduction": data["introduction"]}, "related_work": {"related_work": data["related_work"]},
        "discussion": {"discussion": {k: v for k, v in data["discussion"].items() if k != "analysis_snapshot"}},
        "analysis": data["discussion"].get("analysis_snapshot", {}),
        "verification": {"ok": False, "issues": [{"severity": "error", "section": "Abstract", "claim": "", "reason": "x"}]},
        "manuscript": {"title": "Học vấn và tiền lương", "abstract": "Tóm tắt.", "keywords": ["a", "b", "c"],
                       "authors": [], "references": data["references"]}}}


@pytest.fixture
def client(monkeypatch, tmp_path):
    state = {"run": empirical_run()}
    async def get(run_id):
        return state["run"]
    monkeypatch.setattr(store, "get", get)
    monkeypatch.setattr(autonomous_router, "ROOT", tmp_path)
    app = FastAPI()
    app.include_router(autonomous_router.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"), state


async def test_draft_pdf_is_labelled_and_final_pdf_stays_locked(client):
    http, state = client
    async with http:
        final = await http.get("/autonomous/runs/7/export.pdf")
        assert final.status_code == 409
        draft = await http.get("/autonomous/runs/7/export-draft.pdf")
        assert draft.status_code == 200 and draft.headers["content-type"] == "application/pdf"
    text = " ".join(" ".join(page.get_text() for page in fitz.open(stream=draft.content, filetype="pdf")).split())
    assert "BẢN NHÁP" in text and "1 lỗi chặn" in text


async def test_draft_pdf_needs_a_manuscript(client):
    http, state = client
    state["run"]["artifacts"].pop("manuscript")
    async with http:
        assert (await http.get("/autonomous/runs/7/export-draft.pdf")).status_code == 409
