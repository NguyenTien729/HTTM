"""End-to-end workflow with real isolated PostgreSQL and real statistical code.

Only literature/data providers and LLM responses are replaced with fixtures.
"""
import json

import pandas as pd
import pytest

from app import db
from app.autonomous import data, pipeline, router, store
from app.autonomous.models import ResearchBrief, AnalysisDesign, FrontMatter, ScientificAudit
from app.autonomous.worker import process
from app.models import Paper, PaperAnalysisExtraction, ConclusionExtraction
from tests.helpers import sample_plan
from tests.test_autonomous import brief, design
from tests.test_review_integration import review_client


async def test_autonomous_topic_to_three_approvals_real_data_join_and_pdf(review_client, monkeypatch, tmp_path):
    client = review_client
    monkeypatch.setattr(data, "ROOT", tmp_path)
    monkeypatch.setattr(router, "ROOT", tmp_path)
    async def planning(*args):
        plan = sample_plan()
        plan.search_strategy.target_paper_count = 3
        return plan
    monkeypatch.setattr("app.routers.planner.generate_structured", planning)

    async def search_once(plan, project_id, *args):
        return [Paper(project_id=project_id, title="Climate and rice yield in Vietnam", year=2026,
                      publication_date="2026-01-01", abstract="Rice yield is associated with temperature in Vietnam.",
                      doi=f"10.autonomous/{project_id}", source="openalex")]
    async def concepts(*args):
        from app.services.relevance_filter import TopicConcepts
        return TopicConcepts(concepts=["climate", "rice"], scope_rule="Vietnam")
    async def screening(papers, *args, **kwargs):
        return [(p, 90) for p in papers]
    async def ingest(*args):
        return None
    async def read(prompt, schema):
        return PaperAnalysisExtraction(method="observational", population="rice provinces", context="Vietnam",
            key_finding="Temperature and rice yield show an observed association", limitation="No causal identification")
    monkeypatch.setattr("app.routers.search._search_once", search_once)
    monkeypatch.setattr("app.routers.search.extract_topic_concepts", concepts)
    monkeypatch.setattr("app.routers.search.screen_papers", screening)
    monkeypatch.setattr("app.routers.analysis._ingest_fulltext", ingest)
    monkeypatch.setattr("app.routers.analysis.generate_structured", read)

    response = await client.post("/autonomous/runs", json={"topic": "Climate and rice yield Vietnam", "max_rows": 100,
        "authors": [{"name": "Tác giả kiểm thử", "affiliation": "Viện kiểm thử"}]})
    assert response.status_code == 201, response.text
    run = response.json()
    rid, pid = run["id"], run["project_id"]

    async def tick(expected):
        claimed = await store.claim()
        assert claimed and claimed["id"] == rid and claimed["stage"] == expected
        await process(claimed)
        value = await store.get(rid)
        assert value["status"] != "failed", value.get("error")
        return value

    await tick("literature_search")
    await tick("literature_read")
    async with db.get_pool().acquire() as conn:
        paper_id = await conn.fetchval("SELECT id FROM papers WHERE project_id=$1", pid)

    async def synthesize(prompt, schema):
        return schema(themes=[{"name": "Rice and climate", "description": "Observed association", "paper_ids": [paper_id]}])
    monkeypatch.setattr("app.routers.synthesis.generate_structured", synthesize)
    await tick("literature_synthesis")
    async def scientific(prompt, schema):
        if schema is ResearchBrief:
            result = brief()
            result["evidence_paper_ids"] = [paper_id]
            return schema.model_validate(result)
        if schema is AnalysisDesign:
            return schema.model_validate(design())
        if schema is FrontMatter:
            return schema(title="Khí hậu và năng suất lúa Việt Nam", abstract="Nghiên cứu đối chiếu nhiệt độ và năng suất lúa. "
                "Phân tích cho thấy liên hệ quan sát; chưa có bằng chứng nhân quả.", keywords=["khí hậu", "lúa", "Việt Nam"])
        assert schema is ScientificAudit
        return schema(issues=[], limitations=["Kiểm thử dùng bằng chứng mô phỏng."])
    monkeypatch.setattr(pipeline, "generate_structured", scientific)
    run = await tick("research_plan")
    assert run["status"] == "awaiting_research_approval"
    assert await store.claim() is None
    # A stale browser cannot approve a newer proposal.
    stale = await client.post(f"/autonomous/runs/{rid}/decision", json={"expected_version": 1, "action": "approve"})
    assert stale.status_code == 409
    accepted = await client.post(f"/autonomous/runs/{rid}/decision", json={"expected_version": run["version"], "action": "approve"})
    assert accepted.status_code == 200
    # Verify persistence independently of the current pool/client connection.
    await db.close_db()
    await db.connect_db()
    assert (await store.get(rid))["stage"] == "data_collection"

    searches = []
    async def discover(context, **kwargs):
        searches.append(context["project_topic"])
        outcome = "Rice yield" in context["project_topic"]
        key = "yield" if outcome else "temperature"
        return {"search": {"queries_used": [key]}, "datasets": [{"dataset_id": f"example/{key}", "title": key,
            "dataset_url": f"https://huggingface.co/datasets/example/{key}", "source": "Hugging Face", "license": "CC-BY"}]}
    async def inspect(dataset_id):
        return {"preview_selection": {"config": "default", "split": "train"}}
    async def download(dataset_id, *args):
        frame = {"province": ["A"]*6, "year": list(range(2020, 2026))}
        if dataset_id.endswith("yield"):
            frame["yield"] = [3., 3.5, 4., 3.8, 5., 4.5]
        else:
            frame["temp"] = [25., 26., 24., 27., 28., 26.5]
        return pd.DataFrame(frame), {"sampled": False, "analysis_mode": "tabular"}
    monkeypatch.setattr(data, "discover_gap_datasets", discover)
    monkeypatch.setattr(data, "inspect_dataset", inspect)
    monkeypatch.setattr(data, "download_dataset_dataframe", download)
    run = await tick("data_collection")
    assert len(searches) == 2 and all("Vietnam" in s for s in searches)
    run = await tick("design")
    assert run["status"] == "awaiting_design_approval"
    assert run["artifacts"]["design_validation"]["ok"]
    assert await store.claim() is None
    assert (await client.get(f"/autonomous/runs/{rid}/export.pdf")).status_code == 409
    response = await client.post(f"/autonomous/runs/{rid}/decision", json={"expected_version": run["version"], "action": "approve"})
    assert response.status_code == 200
    run = await tick("analysis")
    assert run["artifacts"]["analysis"]["ols_regression"]["status"] == "ok"
    assert run["artifacts"]["analysis"]["dataset"]["join_report"][0]["matched_rows"] == 6

    async def prose(prompt, schema):
        return schema(**{name: f"Bằng chứng liên quan đến khí hậu và năng suất lúa [{paper_id}]." for name in schema.model_fields})
    monkeypatch.setattr("app.routers.writer.generate_structured", prose)
    monkeypatch.setattr("app.routers.related_work.generate_structured", prose)
    async def discussion(prompt, schema):
        if schema is ConclusionExtraction:
            return schema(conclusion_paragraph="Chỉ ghi nhận liên hệ quan sát, chưa chứng minh nhân quả.")
        return schema(key_findings_paragraph="Phân tích dữ liệu ghi nhận liên hệ quan sát.",
            literature_comparison_paragraph=f"Đối chiếu bằng chứng trước [{paper_id}].", hypothesis_assessments=[],
            implications_paragraph="Cần nghiên cứu bổ sung.", limitations_paragraph="Mẫu nhỏ và chưa đại diện.",
            future_work_paragraph="Mở rộng phạm vi dữ liệu.")
    monkeypatch.setattr("app.routers.discussion.generate_structured", discussion)
    for stage in ("introduction", "related_work", "discussion", "manuscript", "verification"):
        run = await tick(stage)
    assert run["status"] == "awaiting_final_approval"
    assert run["artifacts"]["verification"]["ok"], run["artifacts"]["verification"]
    assert await store.claim() is None
    # Final revision keeps analysis but regenerates writing and invalidates verification.
    response = await client.post(f"/autonomous/runs/{rid}/decision", json={"expected_version": run["version"],
        "action": "revise", "notes": "Nêu rõ chưa có bằng chứng nhân quả"})
    assert response.status_code == 200
    revised = response.json()
    assert "analysis" in revised["artifacts"] and "verification" not in revised["artifacts"]
    for stage in ("introduction", "related_work", "discussion", "manuscript", "verification"):
        run = await tick(stage)
    response = await client.post(f"/autonomous/runs/{rid}/decision", json={"expected_version": run["version"], "action": "approve"})
    assert response.status_code == 200 and response.json()["status"] == "completed"
    pdf = await client.get(f"/autonomous/runs/{rid}/export.pdf")
    assert pdf.status_code == 200, pdf.text[:300] if not pdf.content.startswith(b"%PDF") else ""
    assert pdf.content.startswith(b"%PDF")
    markdown = await client.get(f"/autonomous/runs/{rid}/manuscript.md")
    assert "## Methods" in markdown.text and "Tác giả kiểm thử" in markdown.text
    audit = await client.get(f"/autonomous/runs/{rid}/audit.json")
    assert len(audit.json()["history"]) > 12
    async with db.get_pool().acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM research_questions WHERE project_id=$1", pid) == 1


async def test_cancelled_worker_cannot_overwrite_state_and_expired_leases_need_resume(review_client):
    async with db.get_pool().acquire() as conn:
        pid = await conn.fetchval("INSERT INTO research_projects(topic,research_plan) VALUES('Lease test','{}') RETURNING id")
    run = await store.create(pid, {}, "Lease test")
    claimed = await store.claim()
    assert claimed["id"] == run["id"]
    response = await review_client.post(f"/autonomous/runs/{run['id']}/cancel", json={"expected_version": claimed["version"]})
    assert response.status_code == 200
    claimed["status"] = "queued"
    assert await store.save_worker(claimed) is None
    assert (await store.get(run["id"]))["status"] == "cancelled"
    second = await store.create(pid, {}, "Lease test")
    claimed = await store.claim()
    async with db.get_pool().acquire() as conn:
        await conn.execute("UPDATE autonomous_runs SET lease_until=now()-interval '3 minutes' WHERE id=$1", second["id"])
    assert await store.claim() is None
    interrupted = await store.get(second["id"])
    assert interrupted["status"] == "interrupted"
    response = await review_client.post(f"/autonomous/runs/{second['id']}/resume", json={"expected_version": interrupted["version"]})
    assert response.status_code == 200
    value = response.json()
    await review_client.post(f"/autonomous/runs/{second['id']}/cancel", json={"expected_version": value["version"]})


async def test_lifespan_dispatches_durable_queue_and_serves_new_default_ui(review_client, monkeypatch):
    import asyncio
    from app.main import app
    from app.autonomous import worker

    async with db.get_pool().acquire() as conn:
        pid = await conn.fetchval("INSERT INTO research_projects(topic,research_plan) VALUES('Startup test','{}') RETURNING id")
    saved = await store.create(pid, {}, "Startup test")
    stages = []
    async def execute(value):
        stages.append(value["stage"])
        return {"test_stage": value["stage"]}
    monkeypatch.setattr(worker, "execute_stage", execute)
    await db.close_db()
    async with app.router.lifespan_context(app):
        for _ in range(100):
            value = await store.get(saved["id"])
            if value["status"] == "awaiting_research_approval":
                break
            await asyncio.sleep(.05)
        assert value["status"] == "awaiting_research_approval"
        assert stages == ["literature_search", "literature_read", "literature_synthesis", "research_plan"]
        response = await review_client.get("/")
        assert response.headers["location"] == "/ui/autonomous.html"
        assert (await review_client.get("/ui/autonomous.js")).status_code == 200
        assert (await review_client.get("/openapi.json")).status_code == 200
        await review_client.post(f"/autonomous/runs/{saved['id']}/cancel", json={"expected_version": value["version"]})
