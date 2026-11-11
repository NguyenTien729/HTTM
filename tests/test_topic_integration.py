"""Topic-driven empirical workflow against an explicitly isolated PostgreSQL."""
import json
import pandas as pd
from app import db
from app.models import ResearchQuestionExtraction, DiscussionExtraction, ConclusionExtraction
from app.routers import export
from tests.test_review_integration import review_client
from tests.helpers import sample_plan, insert_paper


async def test_branch_a_question_data_discussion_and_pdf_without_any_gaps(review_client, monkeypatch, tmp_path):
    client = review_client
    topic = "Tác động của AI lên học tập của sinh viên Việt Nam"
    async def plan(*args):
        result = sample_plan()
        result.topic = topic
        return result
    monkeypatch.setattr("app.routers.planner.generate_structured", plan)
    response = await client.post("/plan", json={"topic": topic, "paper_type": "topic_research"})
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    async with db.get_pool().acquire() as conn:
        paper_id = await insert_paper(conn, pid, title="AI student survey", abstract="Survey of AI usage and learning outcomes among university students.")
        await conn.execute("INSERT INTO paper_analyses(paper_id,project_id,key_finding) VALUES($1,$2,'An observed association')", paper_id, pid)
    async def synthesize(prompt, schema):
        assert topic in prompt
        assert "Hướng A" in prompt
        return schema(themes=[{"name": "Learning", "description": "Evidence on learning", "paper_ids": [paper_id]}])
    monkeypatch.setattr("app.routers.synthesis.generate_structured", synthesize)
    response = await client.post(f"/synthesize/{pid}")
    assert response.status_code == 200, response.text
    async def question(prompt, schema):
        assert topic in prompt
        return ResearchQuestionExtraction(candidate_rq="Việc sử dụng AI liên hệ với điểm học tập của sinh viên Việt Nam như thế nào?",
            feasibility_score=3, feasibility_notes="Cần dữ liệu đo được việc dùng AI và điểm số", novelty_score=4, novelty_notes="Not assessed",
            is_quantitative=True, variables=[], hypotheses=[])
    monkeypatch.setattr("app.routers.research_questions.generate_structured", question)
    response = await client.post(f"/research-questions/project/{pid}")
    assert response.status_code == 200, response.text
    rq = response.json()["research_question"]
    assert rq["gap_id"] is None and rq["hypotheses"] == []
    rid = rq["id"]
    async def prose(prompt, schema):
        return schema(**{name: f"Bằng chứng liên quan đề tài [{paper_id}]." for name in schema.model_fields})
    monkeypatch.setattr("app.routers.writer.generate_structured", prose)
    monkeypatch.setattr("app.routers.related_work.generate_structured", prose)
    for path in (f"/write/introduction/{rid}", f"/write/related-work/{rid}"):
        response = await client.post(path)
        assert response.status_code == 200, response.text
    async def discover(gap, **kwargs):
        assert gap["project_topic"] == topic
        assert not gap.get("gap_id")
        return {"count": 1, "datasets": [{"dataset_id": "example/ai-students"}]}
    monkeypatch.setattr("app.data_research.router.discover_gap_datasets", discover)
    response = await client.post("/data-research/discover", json={"project_id": pid, "gap_id": 98765})
    assert response.status_code == 200, response.text  # A ignores a stale legacy gap.
    async def download(**kwargs):
        return pd.DataFrame({"score": [1., 2., 4., 5., 7., 9.], "hours": [1., 2., 2., 3., 4., 5.]}), {
            "dataset_id": "example/ai-students", "config": "default", "split": "train",
            "rows_in_selected_split": 6, "rows_analyzed": 6, "sampled": False, "analysis_mode": "tabular_metadata"}
    monkeypatch.setattr("app.data_research.router.download_dataset_dataframe", download)
    response = await client.post("/data-research/analyze", json={"project_id": pid, "dataset_id": "example/ai-students",
        "config": "default", "split": "train", "confirm_dataset": True, "target_column": "score", "predictor_columns": ["hours"]})
    assert response.status_code == 200, response.text
    analysis = response.json()
    assert analysis["ols_regression"]["status"] == "ok"
    assert analysis["dataset"]["research_topic"] == topic
    assert "research_gap" not in analysis["dataset"]
    async def discussion(prompt, schema):
        if schema is ConclusionExtraction:
            return schema(conclusion_paragraph="Có mối liên hệ quan sát được; chưa kết luận nhân quả.")
        assert schema is DiscussionExtraction
        assert "Hướng A" in prompt
        return schema(key_findings_paragraph="Phân tích được thực hiện trên 6 dòng.",
            literature_comparison_paragraph=f"Đối chiếu nghiên cứu trước [{paper_id}].", hypothesis_assessments=[],
            implications_paragraph="Cần thêm bằng chứng.", limitations_paragraph="Giới hạn tính đại diện.", future_work_paragraph="Mở rộng mẫu.")
    monkeypatch.setattr("app.routers.discussion.generate_structured", discussion)
    response = await client.post(f"/write/discussion/{rid}", json={"analysis_result": analysis})
    assert response.status_code == 200, response.text
    assert response.json()["discussion"]["hypothesis_assessments"] == []
    data = await export._load_paper_data(rid)
    assert data["gap"] is None
    output = tmp_path / "topic-research.pdf"
    export._build_paper_pdf(data, output)
    assert output.read_bytes().startswith(b"%PDF")
    async with db.get_pool().acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM gaps WHERE project_id=$1", pid) == 0
        assert await conn.fetchval("SELECT gap_id FROM research_questions WHERE id=$1", rid) is None
