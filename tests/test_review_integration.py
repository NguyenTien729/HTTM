"""Run only against the explicitly isolated local review test database."""
import os
import json
from urllib.parse import urlparse
import httpx
import pytest
from app import db
from app.main import app
from app.models import Paper, PaperAnalysisExtraction
from app.services.review_manuscript import WrittenSection, SupportReport
from tests.helpers import sample_plan


@pytest.fixture
async def review_client():
    url = urlparse(os.environ.get("DATABASE_URL", ""))
    if url.username != "httm_review_test" or url.port != 65433:
        pytest.skip("Requires dedicated httm_review_test database on localhost:65433; never modifies a user database")
    await db.connect_db()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await db.close_db()


async def test_review_topic_to_validated_pdf_and_docx_with_real_persistence(review_client, monkeypatch):
    client = review_client
    async def plan_writer(*args):
        plan = sample_plan()
        plan.search_strategy.target_paper_count = 2
        return plan
    monkeypatch.setattr("app.routers.planner.generate_structured", plan_writer)
    response = await client.post("/plan", json={"topic": "AI trong giáo dục", "paper_type": "review"})
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    assert (await client.get(f"/plan/{pid}")).json()["paper_type"] == "review"
    source = "Students reported improved learning engagement when using AI tools, but causal effects were not established."
    async def search_once(plan, project_id, *args):
        return [Paper(project_id=project_id, title=f"Study {i}", abstract=source, source="openalex", year=2026,
                      publication_date="2026-01-01", doi=f"10.test/{pid}-{i}") for i in (1, 2)]
    async def concepts(*args):
        from app.services.relevance_filter import TopicConcepts
        return TopicConcepts(concepts=["AI", "education"], scope_rule="AI in education")
    async def screening(papers, *args, **kwargs):
        return [(p, 90) for p in papers]
    monkeypatch.setattr("app.routers.search._search_once", search_once)
    monkeypatch.setattr("app.routers.search.extract_topic_concepts", concepts)
    monkeypatch.setattr("app.routers.search.screen_papers", screening)
    response = await client.post(f"/search/{pid}")
    assert response.status_code == 200, response.text
    assert response.json()["saved"] == 2
    papers = (await client.get(f"/search/{pid}")).json()
    ids = [p["id"] for p in papers]
    async def analyze(prompt, schema):
        return PaperAnalysisExtraction(method="survey", population="students", context="education",
                                       key_finding="Reported engagement benefits", limitation="No causal identification")
    monkeypatch.setattr("app.routers.analysis.generate_structured", analyze)
    response = await client.post(f"/analyze/{pid}?use_fulltext=false")
    assert response.status_code == 200, response.text
    async def write(prompt, schema):
        if schema is WrittenSection:
            return schema(paragraphs=[{"text": "Các nghiên cứu ghi nhận sự tham gia học tập, nhưng chưa đủ bằng chứng nhân quả " + ''.join(f"[{i}]" for i in ids) + ".",
                "evidence": [{"paper_id": i, "source_id": f"paper:{i}:abstract", "quote": source} for i in ids]}])
        assert schema is SupportReport
        batch = json.loads(prompt[prompt.index('[{'):])
        return schema(decisions=[{"paragraph_id": p["paragraph_id"], "supported": True, "reason": "Matches supplied source"} for p in batch])
    monkeypatch.setattr("app.services.review_manuscript.generate_structured", write)
    response = await client.post(f"/reviews/project/{pid}")
    assert response.status_code == 200, response.text
    draft = response.json()
    rid = draft["id"]
    assert draft["manuscript"]["search_audit"]["selected"] == 2
    assert (await client.post(f"/reviews/{rid}/export/pdf")).status_code == 409
    response = await client.post(f"/reviews/{rid}/validate")
    assert response.status_code == 200 and response.json()["validation"]["ok"], response.text
    assert (await client.get(f"/reviews/{rid}")).json()["status"] == "validated"
    for format, magic in (("pdf", b"%PDF"), ("docx", b"PK")):
        response = await client.post(f"/reviews/{rid}/export/{format}", json={"authors": [{"name": "Tác giả thử nghiệm"}]})
        assert response.status_code == 200, response.text
        assert response.content.startswith(magic)
    edits = {s["key"]: {"paragraphs": s["paragraphs"]} for s in draft["manuscript"]["sections"] if s["key"] != "methodology"}
    response = await client.put(f"/reviews/{rid}", json={"title": "Tiêu đề đã chỉnh sửa", "sections": edits})
    assert response.status_code == 200, response.text
    new_id = response.json()["id"]
    assert new_id != rid
    assert (await client.post(f"/reviews/{new_id}/export/pdf")).status_code == 409
    old = (await client.get(f"/reviews/{rid}")).json()
    assert old["status"] == "validated"
    assert old["manuscript"]["title"] == "AI trong giáo dục"
    assert len((await client.get(f"/reviews/project/{pid}")).json()) == 2
    response = await client.patch(f"/plan/{pid}/paper-type", json={"paper_type": "original_research"})
    assert response.status_code == 200
    assert (await client.get(f"/search/{pid}")).json()  # Source papers remain.
    assert (await client.get(f"/reviews/{rid}")).json()["status"] == "validated"
    assert (await client.post(f"/reviews/project/{pid}")).status_code == 409
    assert (await client.patch(f"/plan/{pid}/paper-type", json={"paper_type": "review"})).status_code == 200


async def test_original_project_cannot_enter_review_writer(review_client, monkeypatch):
    async def plan(*args):
        return sample_plan()
    monkeypatch.setattr("app.routers.planner.generate_structured", plan)
    response = await review_client.post("/plan", json={"topic": "Original research", "paper_type": "original_research"})
    pid = response.json()["id"]
    response = await review_client.post(f"/reviews/project/{pid}")
    assert response.status_code == 409
