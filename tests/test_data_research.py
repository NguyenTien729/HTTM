import pandas as pd

from app.data_research.service import analyze_dataframe, validate_dataset_id, DataResearchError


def test_analyze_dataframe_returns_real_descriptive_statistics_and_drafts():
    frame = pd.DataFrame({
        "group": ["A", "A", "A", "B", "B", "B"],
        "score": [10.0, 11.0, 12.0, 20.0, 21.0, 22.0],
        "hours": [1.0, 2.0, 3.0, 2.0, 3.0, 4.0],
        "label": ["x", "x", "y", "y", "y", "z"],
    })
    metadata = {
        "dataset_id": "example/demo",
        "config": "default",
        "split": "train",
        "rows_in_selected_split": 6,
        "rows_analyzed": 6,
        "sampled": False,
        "sampling_method": "full split",
        "source_url": "https://huggingface.co/datasets/example/demo",
    }
    result = analyze_dataframe(frame, metadata, target_column="score", group_column="group", predictor_columns=["hours"])

    assert result["overview"]["rows_analyzed"] == 6
    assert result["column_summaries"]["score"]["mean"] == 16.0
    assert result["group_test"]["status"] == "ok"
    assert len(result["group_test"]["tests"]) == 2
    assert result["ols_regression"]["status"] == "ok"
    assert "Methods" not in result["methods_draft"]  # draft is plain prose, not a fake heading
    assert "6 dòng" in result["methods_draft"]
    assert "R²=" in result["results_draft"]


def test_validate_dataset_id_rejects_arbitrary_path():
    try:
        validate_dataset_id("https://example.com/my.csv")
    except DataResearchError as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("URL should not be accepted as dataset_id")


def test_extract_topic_prefers_planner_research_topic_over_generic_title():
    from types import SimpleNamespace
    from app.data_research.planner_integration import _extract_topic

    project = SimpleNamespace(
        id=7,
        title="Research project 7",
        research_topic="Impact of AI tools on university students' productivity",
    )
    topic, source, title = _extract_topic(project)

    assert topic == "Impact of AI tools on university students' productivity"
    assert source == "research_topic"
    assert title == "Research project 7"


def test_discover_uses_the_selected_gap_not_the_project_topic(monkeypatch):
    import asyncio
    from app.data_research import router as router_module

    selected_gap = {
        "gap_id": 9,
        "project_id": 42,
        "gap_type": "dataset",
        "description": "Few public Vietnamese SMS spam datasets include realistic informal messages.",
        "proposed_research_question": "Can informal Vietnamese text messages improve spam detection?",
        "confirmed": True,
    }
    calls = {}

    async def fake_resolve_gap(project_id, gap_id):
        calls["resolve"] = (str(project_id), str(gap_id))
        return selected_gap

    async def fake_discover_gap_datasets(gap, limit, max_search_seconds):
        calls["gap"] = gap
        calls["limit"] = limit
        calls["max_search_seconds"] = max_search_seconds
        return {"input_source": "selected_research_gap", "gap": gap, "count": 0, "datasets": []}

    async def should_not_load_topic(*args, **kwargs):
        raise AssertionError("Dataset discovery must not load or search the broad project topic")

    monkeypatch.setattr(router_module, "resolve_selected_gap", fake_resolve_gap)
    monkeypatch.setattr(router_module, "discover_gap_datasets", fake_discover_gap_datasets)
    monkeypatch.setattr(router_module, "resolve_project_topic", should_not_load_topic)

    request = router_module.DiscoverRequest(
        project_id="42", gap_id="9", limit=5, max_search_seconds=45
    )
    result = asyncio.run(router_module.discover(request))

    assert calls["resolve"] == ("42", "9")
    assert calls["gap"] == selected_gap
    assert calls["limit"] == 5
    assert calls["max_search_seconds"] == 45
    assert result["input_source"] == "selected_research_gap"
    assert result["project_id"] == 42
    assert "topic" not in result


def test_discover_rejects_old_manual_topic_request():
    from pydantic import ValidationError
    from app.data_research import router as router_module

    try:
        router_module.DiscoverRequest(topic="A manually entered research topic")
    except ValidationError:
        pass
    else:
        raise AssertionError("Discover must reject the old manual-topic request")


def test_discover_requires_project_id():
    from pydantic import ValidationError
    from app.data_research import router as router_module

    try:
        router_module.DiscoverRequest()
    except ValidationError:
        pass
    else:
        raise AssertionError("Discover must require project_id")


def test_discover_requires_gap_id():
    from pydantic import ValidationError
    from app.data_research import router as router_module

    try:
        router_module.DiscoverRequest(project_id="42")
    except ValidationError:
        pass
    else:
        raise AssertionError("Discover must require the selected gap_id")


def test_resolve_selected_gap_checks_project_ownership(monkeypatch):
    import asyncio
    import sys
    from types import ModuleType
    from app.data_research.planner_integration import resolve_selected_gap

    calls = {}
    row = {
        "id": 9,
        "project_id": 42,
        "gap_type": "dataset",
        "description": "Few public Vietnamese SMS spam datasets include informal messages.",
        "proposed_research_question": "Can informal messages improve spam detection?",
        "confirmed": True,
    }

    class FakeConnection:
        async def fetchrow(self, query, gap_id, project_id):
            calls["query"] = query
            calls["args"] = (gap_id, project_id)
            return row

    class AcquireContext:
        async def __aenter__(self):
            return FakeConnection()
        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def acquire(self):
            return AcquireContext()

    fake_db = ModuleType("app.db")
    fake_db.get_pool = lambda: FakePool()
    monkeypatch.setitem(sys.modules, "app.db", fake_db)

    result = asyncio.run(resolve_selected_gap("42", "9"))

    assert calls["args"] == (9, 42)
    assert "FROM gaps" in calls["query"]
    assert "WHERE id = $1 AND project_id = $2" in calls["query"]
    assert result["gap_id"] == 9
    assert result["project_id"] == 42
    assert "topic" not in result


def test_discover_gap_datasets_stops_when_enough_relevant_datasets_are_found(monkeypatch):
    import asyncio
    from app.data_research import service

    gap = {
        "gap_id": 9,
        "project_id": 42,
        "gap_type": "dataset",
        "description": "Few public Vietnamese SMS spam datasets include informal messages.",
        "proposed_research_question": "Can informal Vietnamese text messages improve spam detection?",
        "confirmed": True,
    }
    queries_requested = []

    async def fake_expand(selected_gap, excluded_queries, round_number):
        assert selected_gap == gap
        assert "topic" not in selected_gap
        return [
            "Vietnamese SMS spam detection",
            "informal text message classification",
            "mobile junk message corpus",
        ]

    async def fake_get_json(client, url, params, request_timeout=None):
        queries_requested.append(params["search"])
        return [{
            "id": "sample/vietnamese-sms-spam",
            "downloads": 100,
            "likes": 5,
            "tags": ["Vietnamese", "SMS", "spam", "text-classification"],
            "cardData": {
                "description": "Vietnamese SMS spam and ham text messages for classification.",
                "task_categories": ["text-classification"],
            },
        }]

    monkeypatch.setattr(service, "_expand_gap_queries", fake_expand)
    monkeypatch.setattr(service, "_get_json", fake_get_json)

    result = asyncio.run(service.discover_gap_datasets(gap, limit=1, max_search_seconds=5))

    assert result["count"] == 1
    assert result["search"]["stop_reason"] == "enough_results"
    assert result["search"]["time_limit_reached"] is False
    assert result["search"]["searched_query_count"] == 1
    assert len(queries_requested) == 1
    assert result["datasets"][0]["gap_match_score"] > 0
    assert result["input_source"] == "selected_research_gap"


def test_discover_gap_datasets_stops_at_the_time_limit(monkeypatch):
    import asyncio
    from app.data_research import service

    gap = {
        "gap_id": 3,
        "project_id": 12,
        "gap_type": "population",
        "description": "Older adults are underrepresented in digital literacy datasets.",
        "proposed_research_question": "How can digital literacy among older adults be measured?",
    }

    async def fake_expand(selected_gap, excluded_queries, round_number):
        return ["older adults digital literacy"]

    async def slow_get_json(client, url, params, request_timeout=None):
        await asyncio.sleep(2)
        return []

    monkeypatch.setattr(service, "_expand_gap_queries", fake_expand)
    monkeypatch.setattr(service, "_get_json", slow_get_json)

    result = asyncio.run(service.discover_gap_datasets(gap, limit=10, max_search_seconds=1))

    assert result["search"]["stop_reason"] == "time_limit"
    assert result["search"]["time_limit_reached"] is True
    assert result["search"]["elapsed_seconds"] < 1.6


def test_methods_and_results_drafts_include_planner_topic_when_available():
    frame = pd.DataFrame({"score": [1.0, 2.0, 3.0]})
    metadata = {
        "dataset_id": "example/demo",
        "config": "default",
        "split": "train",
        "rows_in_selected_split": 3,
        "rows_analyzed": 3,
        "sampled": False,
        "sampling_method": "full split",
        "source_url": "https://huggingface.co/datasets/example/demo",
        "project_id": "42",
        "research_topic": "AI use and student academic performance",
    }
    result = analyze_dataframe(frame, metadata)

    assert "AI use and student academic performance" in result["methods_draft"]
    assert "AI use and student academic performance" in result["results_draft"]
    assert result["dataset"]["project_id"] == "42"


def test_extract_topic_prefers_nested_planner_output_over_generic_title():
    from types import SimpleNamespace
    from app.data_research.planner_integration import _extract_topic

    project = SimpleNamespace(
        id=9,
        title="My Research Project",
        research_plan={"topic": "Effects of online learning on academic achievement"},
    )
    topic, source, title = _extract_topic(project)

    assert topic == "Effects of online learning on academic achievement"
    assert source == "research_plan.topic"
    assert title == "My Research Project"


def test_analyze_route_attaches_planner_topic_without_manual_topic(monkeypatch):
    import asyncio
    from app.data_research import router as router_module

    async def fake_resolve(project_id):
        assert str(project_id) == "42"
        return {
            "project_id": "42",
            "topic": "AI use and student academic performance",
            "topic_source": "research_topic",
            "project_title": "AI and education",
        }

    async def fake_download_dataset_dataframe(dataset_id, config, split, max_rows):
        return pd.DataFrame({"score": [1.0, 2.0, 3.0]}), {
            "dataset_id": dataset_id,
            "config": config,
            "split": split,
        }

    def fake_analyze_dataframe(frame, dataset_meta, target_column, group_column, predictor_columns):
        return {"dataset": dataset_meta}

    monkeypatch.setattr(router_module, "resolve_project_topic", fake_resolve)
    monkeypatch.setattr(router_module, "download_dataset_dataframe", fake_download_dataset_dataframe)
    monkeypatch.setattr(router_module, "analyze_dataframe", fake_analyze_dataframe)

    request = router_module.AnalyzeRequest(
        dataset_id="example/demo",
        project_id="42",
        config="default",
        split="train",
        confirm_dataset=True,
    )
    result = asyncio.run(router_module.analyze(request))

    assert result["dataset"]["research_topic"] == "AI use and student academic performance"
    assert result["dataset"]["project_id"] == "42"


def test_resolve_project_topic_queries_existing_planner_table(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import sys
    from types import ModuleType
    from app.data_research.planner_integration import resolve_project_topic

    calls = {}
    row = {"id": 42, "topic": "AI use and student academic performance", "research_plan": {}}

    class FakeConnection:
        async def fetchrow(self, query, project_id):
            calls["query"] = query
            calls["project_id"] = project_id
            return row

    class AcquireContext:
        async def __aenter__(self):
            return FakeConnection()
        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def acquire(self):
            return AcquireContext()

    fake_db = ModuleType("app.db")
    fake_db.get_pool = lambda: FakePool()
    monkeypatch.setitem(sys.modules, "app.db", fake_db)
    result = asyncio.run(resolve_project_topic("42"))

    assert calls["project_id"] == 42
    assert "FROM research_projects WHERE id = $1" in calls["query"]
    assert result["project_id"] == "42"
    assert result["topic"] == row["topic"]
    assert result["topic_source"] == "research_projects.topic"
