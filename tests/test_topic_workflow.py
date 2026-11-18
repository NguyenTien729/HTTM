import json
from contextlib import asynccontextmanager
import pytest
from app.routers import research_questions as questions
from app.routers import related_work, writer, discussion
from app.data_research import router as datasets
from app.models import ResearchQuestionExtraction


class Connection:
    def __init__(self):
        self.calls = []
    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        assert "FROM gaps" not in query
        if "INSERT INTO research_questions" in query:
            return {"id": 8, "project_id": args[0], "gap_id": None, "rq_text": args[1],
                "feasibility_score": args[2], "feasibility_notes": args[3], "novelty_score": 1,
                "novelty_notes": args[4], "is_quantitative": args[5], "variables": args[6], "hypotheses": "[]"}
        if "FROM research_questions" in query:
            return {"id": 8, "project_id": 3, "gap_id": None, "hypotheses": "[]", "rq_text": "RQ?"}
        return {"topic": "Ảnh hưởng của biến đổi khí hậu lên năng suất lúa Việt Nam", "paper_type": "topic_research"}
    async def fetch(self, *args):
        return []


class Pool:
    def __init__(self):
        self.conn = Connection()
    @asynccontextmanager
    async def acquire(self):
        yield self.conn


async def test_topic_question_is_saved_without_gap_or_new_hypotheses(monkeypatch):
    pool = Pool()
    monkeypatch.setattr(questions, "get_pool", lambda: pool)
    prompts = []
    async def generate(prompt, schema):
        prompts.append(prompt)
        return ResearchQuestionExtraction(candidate_rq="Nhiệt độ và lượng mưa liên hệ với năng suất lúa Việt Nam như thế nào?",
            feasibility_score=3, feasibility_notes="Cần dữ liệu khí hậu và sản lượng", novelty_score=5, novelty_notes="Novel",
            is_quantitative=True, variables=[], hypotheses=[{"label": "H1", "statement": "Do not keep this",
                "independent_variable": "temperature", "dependent_variable": "yield"}])
    monkeypatch.setattr(questions, "generate_structured", generate)
    response = await questions.generate_topic_question(3)
    assert response.gap_id is None
    assert response.research_question.gap_id is None
    assert response.research_question.hypotheses == []
    assert response.research_question.novelty_score == 1
    assert "Ảnh hưởng của biến đổi khí hậu lên năng suất lúa Việt Nam" in prompts[0]


@pytest.mark.parametrize("module", [writer, related_work])
async def test_writers_load_topic_question_without_reading_gaps(monkeypatch, module):
    pool = Pool()
    monkeypatch.setattr(module, "get_pool", lambda: pool)
    rq = await module._load_research_question(8)
    assert rq["research_basis"] == "project_topic"
    assert rq["gap_type"] == "topic"
    assert "Việt Nam" in rq["project_topic"]


async def test_topic_dataset_discovery_ignores_stale_gap(monkeypatch):
    async def project(_):
        return {"project_id": 3, "topic": "Climate and rice yield", "paper_type": "topic_research"}
    async def forbidden(*args):
        raise AssertionError("Branch A must never resolve a gap")
    async def discover(gap, **kwargs):
        assert gap["project_topic"] == "Climate and rice yield"
        assert not gap.get("gap_id")
        return {"datasets": []}
    monkeypatch.setattr(datasets, "resolve_project_topic", project)
    monkeypatch.setattr(datasets, "resolve_selected_gap", forbidden)
    monkeypatch.setattr(datasets, "discover_gap_datasets", discover)
    await datasets.discover(datasets.DiscoverRequest(project_id=3, gap_id=999))


def test_discussion_prompt_uses_topic_instead_of_a_gap():
    prompt = discussion._discussion_prompt({"rq_text": "Climate and rice yield?", "gap_type": "topic",
        "gap_description": "Không dùng gap", "research_basis": "project_topic", "hypotheses": []}, [], "{}")
    assert "Hướng A" in prompt
    assert "hypothesis_assessments rỗng" in prompt
