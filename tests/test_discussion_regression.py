"""Discussion persistence and metadata-only analysis regression coverage."""
import json
from contextlib import asynccontextmanager

import pytest

from app import db
from app.models import DiscussionExtraction, ConclusionExtraction, HypothesisAssessment, WriteDiscussionRequest
from app.routers import discussion as writer


class SchemaConnection:
    def __init__(self):
        self.statements = []
        self.discussions_created = False
        self.saved = None

    async def execute(self, sql, *args):
        self.statements.append(sql)
        if "CREATE TABLE IF NOT EXISTS discussions" in sql:
            self.discussions_created = True

    async def fetchrow(self, sql, *args):
        assert self.discussions_created, "discussions table is missing"
        assert "INSERT INTO discussions" in sql
        assert len(args) == 13
        self.saved = args
        return {"id": 7}


class Pool:
    def __init__(self):
        self.conn = SchemaConnection()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


async def test_schema_initialization_creates_discussions_and_upgrades_snapshot():
    pool = Pool()
    await db._init_schema(pool)
    await db._init_schema(pool)
    sql = "\n".join(pool.conn.statements)
    assert pool.conn.discussions_created
    for field in ("hypothesis_assessments", "numbers_unverified", "analysis_snapshot", "conclusion_paragraph"):
        assert field in sql
    assert "ALTER TABLE discussions ADD COLUMN IF NOT EXISTS analysis_snapshot" in sql
    assert "DROP TABLE" not in sql


@pytest.mark.parametrize("mode", ["tabular_metadata", "structure_only"])
async def test_writer_saves_discussion_and_analysis_snapshot(monkeypatch, mode):
    pool = Pool()
    await db._init_schema(pool)
    monkeypatch.setattr(writer, "get_pool", lambda: pool)
    async def research_question(_):
        return {"project_id": 1, "rq_text": "RQ?", "gap_type": "dataset", "gap_description": "Gap",
                "hypotheses": [{"label": "H1", "statement": "Example", "independent_variable": "x", "dependent_variable": "y"}]}
    async def themes(_):
        return [{"name": "Theme", "evidence": [{"paper_id": 12, "title": "Paper", "key_finding": "Finding"}]}]
    prompts = []
    async def generate(prompt, schema):
        prompts.append(prompt)
        if schema is ConclusionExtraction:
            return schema(conclusion_paragraph="Chưa đủ bằng chứng.")
        return DiscussionExtraction(key_findings_paragraph="Chưa kiểm định.",
            literature_comparison_paragraph="Literature [12].",
            hypothesis_assessments=[HypothesisAssessment(label="H1", verdict="not_tested", explanation="Chưa đủ dữ liệu.")],
            implications_paragraph="Cần thêm dữ liệu.", limitations_paragraph="Giới hạn dữ liệu.", future_work_paragraph="Phân tích nội dung.")
    monkeypatch.setattr(writer, "_load_research_question", research_question)
    monkeypatch.setattr(writer, "_load_themes_with_evidence", themes)
    monkeypatch.setattr(writer, "generate_structured", generate)
    analysis = {"dataset": {"dataset_id": "owner/images", "analysis_mode": mode, "rows_in_selected_split": 1234},
                "overview": {"rows_analyzed": 0}, "methods_draft": "Metadata only.", "results_draft": "Chưa kiểm định."}
    response = await writer.write_discussion(3, WriteDiscussionRequest(analysis_result=analysis))
    assert response.discussion.id == 7
    assert response.discussion.citations_used == [12]
    snapshot = json.loads(pool.conn.saved[-1])
    assert snapshot["dataset"]["analysis_mode"] == mode
    assert snapshot["methods_draft"] == "Metadata only."
    assert '"analysis_mode": "' + mode + '"' in prompts[0]
    assert "verdict=not_tested" in prompts[0]
