"""Approval invariants, immutable snapshots, and safe multi-source preparation."""
from copy import deepcopy

import pandas as pd
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.autonomous import data, pipeline
from app.autonomous.models import AnalysisDesign, Decision, ResearchBrief, StartRun
from app.autonomous.state import decide, finish_stage, fingerprint


def brief():
    return ResearchBrief(question="Nhiệt độ liên hệ với năng suất lúa như thế nào?", gap="Chưa đối chiếu trong mẫu này.",
        evidence_paper_ids=[1], variables=[{"name": "yield", "role": "dependent", "description": "Năng suất"}],
        geography="Vietnam", geography_terms=["Vietnam", "Viet Nam"], time_scope="2020–2025",
        data_needs=[{"key": key, "concept": concept, "role": role, "unit": unit,
                     "resolution": "province/year", "search_terms": [concept]}
                    for key, concept, role, unit in [("province", "Province", "join_key", "name"),
                        ("year", "Year", "join_key", "year"), ("yield", "Rice yield", "outcome", "t/ha"),
                        ("temperature", "Temperature", "exposure", "C")]], feasibility_notes="Cần kiểm tra hai nguồn.").model_dump()


def binding(key, column, unit, **kwargs):
    return {"variable": key, "column": column, "source_unit": unit, "target_unit": unit,
            "rationale": "Đối chiếu với bảng mã nguồn", **kwargs}


def design():
    return AnalysisDesign(adequate=True, rationale="Có nguồn theo tỉnh/năm", sources=[
        {"source_key": "s01", "bindings": [binding("province", "province", "name", transform="casefold"),
            binding("year", "year", "year"), binding("yield", "yield", "t/ha")]},
        {"source_key": "s02", "bindings": [binding("province", "province", "name", transform="casefold"),
            binding("year", "year", "year"), binding("temperature", "temp", "C")]}],
        joins=[{"right_source": "s02", "keys": ["province", "year"], "cardinality": "one_to_one"}],
        method="ols", target="yield", predictors=["temperature"]).model_dump()


def run(status="awaiting_research_approval", stage="research_plan"):
    return {"id": 1, "project_id": 1, "status": status, "stage": stage, "version": 5,
        "revision": 1, "events": [], "error": None,
        "artifacts": {"topic": "Climate rice Vietnam", "settings": {"max_rows": 100},
                      "research_plan": brief(), "design": design(), "design_validation": {"ok": True},
                      "analysis": {"must": "be invalidated"}, "verification": {"ok": True}}}


@pytest.mark.parametrize("payload", [{}, {"topic": "Climate rice", "project_id": 2}])
def test_start_requires_exactly_one_topic_or_project(payload):
    with pytest.raises(ValidationError):
        StartRun(**payload)


def test_approve_requires_current_version_and_records_fingerprint():
    original = run()
    with pytest.raises(HTTPException) as exc:
        decide(original, Decision(expected_version=4, action="approve"))
    assert exc.value.status_code == 409
    accepted = decide(original, Decision(expected_version=5, action="approve"))
    assert accepted["status"] == "queued" and accepted["stage"] == "data_collection"
    assert accepted["artifacts"]["approvals"][0]["fingerprint"] == fingerprint(brief())
    assert original["status"] == "awaiting_research_approval"


@pytest.mark.parametrize("status,stage,artifact", [
    ("awaiting_design_approval", "design", "design_validation"),
    ("awaiting_final_approval", "verification", "verification")])
def test_invalid_design_or_failed_verification_cannot_be_approved(status, stage, artifact):
    value = run(status, stage)
    value["artifacts"][artifact]["ok"] = False
    with pytest.raises(HTTPException):
        decide(value, Decision(expected_version=5, action="approve"))


def test_research_revision_invalidates_all_downstream_artifacts():
    value = run("awaiting_final_approval", "verification")
    revised = decide(value, Decision(expected_version=5, action="revise", notes="Đổi cách đo năng suất",
                                     restart_from="research_plan"))
    assert revised["revision"] == 2
    assert revised["stage"] == "research_plan"
    assert "analysis" not in revised["artifacts"] and "design" not in revised["artifacts"]
    assert "research_plan" not in revised["artifacts"]


def test_writing_revision_preserves_approved_analysis():
    revised = decide(run("awaiting_final_approval", "verification"),
                     Decision(expected_version=5, action="revise", notes="Không diễn giải nhân quả"))
    assert revised["stage"] == "introduction"
    assert revised["artifacts"]["analysis"] == {"must": "be invalidated"}
    assert "verification" not in revised["artifacts"]


def test_revision_requires_notes():
    with pytest.raises(HTTPException):
        decide(run(), Decision(expected_version=5, action="revise"))


def test_gates_stop_automatic_progress():
    for stage, gate in (("research_plan", "awaiting_research_approval"),
                        ("design", "awaiting_design_approval"), ("verification", "awaiting_final_approval")):
        value = finish_stage(run("running", stage), {"test": True})
        assert value["stage"] == stage and value["status"] == gate
        assert value["events"][-1]["stage"] == stage


def test_changed_plan_invalidates_approval():
    value = decide(run(), Decision(expected_version=5, action="approve"))
    pipeline.approved(value, "research_plan")
    value["artifacts"]["research_plan"]["question"] = "A different question?"
    with pytest.raises(ValueError, match="phê duyệt"):
        pipeline.approved(value, "research_plan")


@pytest.fixture
def collection(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "ROOT", tmp_path)
    frames = [pd.DataFrame({"province": [" A "]*6, "year": list(range(2020,2026)),
                            "yield": [3., 3.5, 4., 3.8, 5., 4.5]}),
              pd.DataFrame({"province": ["a"]*6, "year": list(range(2020,2026)),
                            "temp": [25., 26., 24., 27., 28., 26.5]})]
    sources = []
    for i, frame in enumerate(frames, 1):
        profile = data.save_snapshot(frame, run(), f"s{i:02d}")
        profile.update(candidate={"dataset_id": f"test/source{i}", "dataset_url": f"https://huggingface.co/datasets/test/source{i}"},
                       metadata={"sampled": False})
        sources.append(profile)
    return {"sources": sources}


def test_merges_real_frames_on_declared_keys_with_provenance(collection):
    frame, report = data.prepare_data(1, brief(), design(), collection, 100)
    assert len(frame) == 6 and frame["temperature"].notna().all()
    assert report["joins"][0]["matched_rows"] == 6
    assert len(report["provenance"]) == 2
    assert report["complete_analysis_rows"] == 6


def test_snapshot_tampering_is_rejected(collection):
    path = data.snapshot_path(1, 1, "s01")
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="thay đổi"):
        data.prepare_data(1, brief(), design(), collection, 100)


def test_many_to_many_join_is_rejected(collection):
    source = collection["sources"][1]
    frame = data.load_snapshot(1, source)
    frame = pd.concat([frame, frame.iloc[[0]]])
    source.update(data.save_snapshot(frame, run(), "s02"))
    with pytest.raises(ValueError):
        data.prepare_data(1, brief(), design(), collection, 100)


def test_all_required_variables_and_unit_agreements_are_enforced(collection):
    spec = design()
    spec["sources"][1]["bindings"][-1]["target_unit"] = "F"
    with pytest.raises(ValueError, match="Đơn vị"):
        data.prepare_data(1, brief(), spec, collection, 100)
    spec = design()
    spec["sources"] = spec["sources"][:1]
    spec["joins"] = []
    with pytest.raises(ValueError, match="Thiếu biến"):
        data.prepare_data(1, brief(), spec, collection, 100)


def test_nonmatching_geographic_keys_do_not_produce_an_analysis(collection):
    source = collection["sources"][1]
    frame = data.load_snapshot(1, source)
    frame["province"] = "different region"
    source.update(data.save_snapshot(frame, run(), "s02"))
    with pytest.raises(ValueError, match="Không có bản ghi khớp"):
        data.prepare_data(1, brief(), design(), collection, 100)


def test_missing_key_or_unreported_unit_conversion_is_rejected(collection):
    spec = design()
    spec["sources"][0]["bindings"][0]["column"] = "invented_column"
    with pytest.raises(ValueError, match="cột không tồn tại"):
        data.prepare_data(1, brief(), spec, collection, 100)


async def test_analysis_cannot_run_before_design_approval(collection):
    value = decide(run(), Decision(expected_version=5, action="approve"))
    value["artifacts"]["data_collection"] = collection
    with pytest.raises(ValueError, match="phê duyệt"):
        await pipeline.analyze(value)


async def test_approved_two_source_analysis_computes_statistics(collection, monkeypatch):
    # analyze() also drafts the paper story; isolate it from PostgreSQL and the LLM.
    from app.autonomous.models import PaperStory
    async def no_papers(project_id):
        return []
    async def story(prompt, schema):
        assert schema is PaperStory
        return PaperStory(thesis="Liên hệ quan sát", research_gap="Kiểm thử")
    monkeypatch.setattr(pipeline, "evidence", no_papers)
    monkeypatch.setattr(pipeline, "generate_structured", story)
    value = decide(run(), Decision(expected_version=5, action="approve"))
    value["stage"], value["status"] = "design", "awaiting_design_approval"
    value = decide(value, Decision(expected_version=5, action="approve"))
    value["artifacts"]["data_collection"] = collection
    result = await pipeline.analyze(value)
    assert result["overview"]["rows_analyzed"] == 6
    assert result["ols_regression"]["status"] == "ok"
    assert result["column_summaries"]["yield"]["mean"] == pytest.approx(3.966667, abs=1e-4)
    assert len(result["dataset"]["source_provenance"]) == 2


def test_data_need_english_queries_survive_llm_expansion_failure():
    from app.data_research.service import _gap_seed_queries
    queries = _gap_seed_queries({"project_topic": "Năng suất lúa Việt Nam", "_data_queries": ["rice yield", "rice production"]})
    assert queries[:2] == ["rice yield", "rice production"]


def test_declared_time_filters_are_applied_before_join_and_audited(collection):
    spec = design()
    for source in spec["sources"]:
        source["filters"] = [{"column": "year", "operator": "between", "values": [2021, 2025],
                              "rationale": "Giới hạn thời kỳ chung"}]
    # The plan covers 2021–2025, so dropping the 2020 rows is a valid scope filter.
    plan = brief()
    plan["time_scope"] = "2021–2025"
    frame, report = data.prepare_data(1, plan, spec, collection, 100)
    assert len(frame) == 5 and frame["year"].min() == 2021
    assert report["provenance"][0]["rows_before_filter"] == 6
    assert report["provenance"][0]["rows"] == 5


def test_geography_filter_cannot_silently_use_other_records(collection):
    spec = design()
    spec["sources"][0]["filters"] = [{"column": "province", "operator": "eq", "values": ["different region"],
                                     "rationale": "Chọn địa bàn"}]
    with pytest.raises(ValueError, match="Không có bản ghi"):
        data.prepare_data(1, brief(), spec, collection, 100)
