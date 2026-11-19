"""Receiving team data in the empirical branch: parsing, cleaning, provenance and state."""
import io
import os
import unicodedata

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

import httpx
import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException

from app.autonomous import data, pipeline, router as autonomous_router, store, uploads
from app.autonomous.models import AnalysisDesign, Decision
from app.autonomous.state import decide
from tests.test_autonomous import binding, brief, run

VN_CSV = ("﻿Mã tỉnh ; Tỉnh;Năm;Năng suất (tạ/ha);Ghi chú;\n"
          "01;Hà Nội;2021;60,5;;\n"
          "02;Hà Giang ;2021;1.234,5;mưa lớn;\n"
          "03;Lào Cai;2021;N/A;;\n"
          ";;;;;\n"
          "01;Hà Nội;2021;60,5;;\n")


@pytest.fixture(autouse=True)
def snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "ROOT", tmp_path)
    return tmp_path


def user_run(status="awaiting_research_approval", stage="research_plan", **artifacts):
    value = run(status, stage)
    value["artifacts"]["settings"] = {"max_rows": 1000, "max_sources": 3, "collection_seconds": 30,
                                      "paper_type": "original_research"}
    for key in ("design", "design_validation", "analysis", "verification"):
        value["artifacts"].pop(key, None)
    value["artifacts"].update(artifacts)
    return value


def attach(value, text=VN_CSV, name="khao_sat.csv", **options):
    frame, log = uploads.parse_and_clean(text.encode("utf-8"), name, **options)
    return uploads.attach_user_source(value, frame, log, description="Khảo sát năng suất của nhóm, 2021, tạ/ha",
                                      license="Dữ liệu của nhóm")


# ---------------------------------------------------------------- parsing & cleaning

def test_vietnamese_csv_is_cleaned_deterministically_with_a_complete_log():
    frame, log = uploads.parse_and_clean(VN_CSV.encode("utf-8"), "khao_sat.csv")
    assert list(frame.columns) == ["Mã tỉnh", "Tỉnh", "Năm", "Năng suất (tạ/ha)", "Ghi chú"]
    assert log["file"]["delimiter"] == ";" and log["file"]["encoding"] == "utf-8-sig"
    # Decimal comma and dot thousands are read as numbers; codes keep their leading zero.
    assert frame["Năng suất (tạ/ha)"].fillna(-1).tolist() == [60.5, 1234.5, -1, 60.5]
    assert frame["Năm"].dtype == "int64"
    assert frame["Mã tỉnh"].tolist() == ["01", "02", "03", "01"]
    assert log["code_like_columns"] == ["Mã tỉnh"]
    assert frame["Tỉnh"].tolist()[1] == "Hà Giang"
    # The empty row and the unnamed trailing column are removed and counted.
    assert log["dropped_empty_rows"] == 1 and log["dropped_empty_columns"] == ["column_6"]
    # Missing markers are counted only in rows/columns that remain.
    assert {m["column"]: m["markers"] for m in log["missing_markers"]} == {
        "Năng suất (tạ/ha)": {"N/A": 1}, "Ghi chú": {"(ô trống)": 3}}
    assert log["missing_after_cleaning"] == {"Năng suất (tạ/ha)": 1, "Ghi chú": 3}
    # Duplicates are reported but kept unless the user asks.
    assert log["duplicate_rows"] == 1 and not log["duplicates_dropped"] and len(frame) == 4
    summary = uploads.cleaning_summary_vi(log)
    assert "khao_sat.csv" in summary and "dấu phẩy thập phân" in summary and "Không điền giá trị thiếu" in summary


def test_duplicates_are_removed_only_on_request():
    frame, log = uploads.parse_and_clean(VN_CSV.encode("utf-8"), "x.csv", drop_duplicates=True)
    assert len(frame) == 3 and log["duplicates_dropped"]


def test_ambiguous_thousands_stay_text_until_the_user_chooses_a_format():
    raw = "tinh,dan_so\nA,1.234\nB,5.678\n".encode()
    frame, log = uploads.parse_and_clean(raw, "x.csv")
    assert not pd.api.types.is_numeric_dtype(frame["dan_so"])
    assert log["ambiguous_numeric_columns"][0]["column"] == "dan_so"
    frame, log = uploads.parse_and_clean(raw, "x.csv", number_format="comma_decimal")
    assert frame["dan_so"].tolist() == [1234, 5678] and not log["ambiguous_numeric_columns"]
    frame, _ = uploads.parse_and_clean(raw, "x.csv", number_format="dot_decimal")
    assert frame["dan_so"].tolist() == [1.234, 5.678]


def test_mixed_text_column_is_not_partially_converted():
    frame, log = uploads.parse_and_clean("x,y\n1,a\n2,b\nba,c\n".encode(), "x.csv")
    assert frame["x"].tolist() == ["1", "2", "ba"]
    assert all(item["column"] != "x" for item in log["numeric_columns"])


def test_windows_vietnamese_encoding_is_read_and_normalised():
    # Windows-1258 stores tone marks as combining characters (e.g. "Ti" + U+0303 + "nh").
    text = unicodedata.normalize("NFD", "tỉnh,giá trị\nHà Tĩnh,1\n")
    frame, log = uploads.parse_and_clean(text.encode("cp1258"), "x.csv")
    assert log["file"]["encoding"] == "cp1258"
    assert list(frame.columns) == ["tỉnh", "giá trị"]
    assert frame["tỉnh"].iloc[0] == unicodedata.normalize("NFC", "Hà Tĩnh")


def test_excel_sheet_and_header_row_are_respected():
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame([["Bảng 1. Chú thích"]]).to_excel(writer, sheet_name="Ghi chú", index=False, header=False)
        pd.DataFrame({"Tỉnh": ["A", "B"], "GDP": [1.5, 2.25], "Mã": ["01", "02"]}).to_excel(
            writer, sheet_name="Data", index=False, startrow=2)
    frame, log = uploads.parse_and_clean(buffer.getvalue(), "x.xlsx", sheet="Data", header_row=3)
    assert frame["GDP"].tolist() == [1.5, 2.25] and frame["Mã"].tolist() == ["01", "02"]
    assert log["file"]["sheets"] == ["Ghi chú", "Data"]
    with pytest.raises(uploads.UploadError, match="Ghi chú, Data"):
        uploads.parse_and_clean(buffer.getvalue(), "x.xlsx", sheet="Không có")


@pytest.mark.parametrize("raw,name,message", [
    (b"a,b\n1,2\n", "x.exe", "Chỉ hỗ trợ"),
    (b"", "x.csv", "rỗng"),
    (b"a,b\n\n\n", "x.csv", "không còn dữ liệu"),
    (b"a,b\nNA,-\n", "x.csv", "không còn dữ liệu"),
])
def test_bad_files_are_rejected_with_a_readable_reason(raw, name, message):
    with pytest.raises(uploads.UploadError, match=message):
        uploads.parse_and_clean(raw, name)


def test_oversize_file_is_rejected(monkeypatch):
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(uploads.UploadError, match="vượt giới hạn"):
        uploads.read_table(b"a,b\n1,2\n3,4\n", "x.csv")


# ------------------------------------------------------------------------ run state

def test_upload_at_research_gate_is_stored_and_survives_revisions():
    value = attach(user_run())
    assert value["status"] == "awaiting_research_approval" and value["stage"] == "research_plan"
    source = value["artifacts"]["user_sources"][0]
    assert source["source_key"] == "s01" and source["candidate"]["user_provided"]
    assert source["metadata"]["origin"] == "user_upload" and source["metadata"]["raw_sha256"]
    assert data.load_snapshot(1, source)["Năng suất (tạ/ha)"].fillna(-1).tolist() == [60.5, 1234.5, -1, 60.5]
    revised = decide(value, Decision(expected_version=5, action="revise", notes="Đổi câu hỏi",
                                     restart_from="research_plan"))
    assert revised["artifacts"]["user_sources"] == value["artifacts"]["user_sources"]
    approved = decide(value, Decision(expected_version=5, action="approve"))
    assert approved["stage"] == "data_collection" and approved["artifacts"]["user_sources"]


def test_upload_after_collection_requeues_design_and_drops_stale_downstream():
    value = user_run("awaiting_design_approval", "design",
                     data_collection={"sources": []}, design={"x": 1}, design_validation={"ok": False},
                     proposal_draft={"x": 1}, analysis={"x": 1})
    value = attach(value)
    assert value["stage"] == "design" and value["status"] == "queued"
    assert [s["source_key"] for s in value["artifacts"]["data_collection"]["sources"]] == ["s01"]
    for stale in ("design", "design_validation", "proposal_draft", "analysis"):
        assert stale not in value["artifacts"]
    removed = uploads.remove_user_source(dict(value, status="awaiting_design_approval"), "s01")
    assert removed["artifacts"]["user_sources"] == [] and removed["artifacts"]["data_collection"]["sources"] == []


@pytest.mark.parametrize("status,paper_type", [("running", "original_research"), ("completed", "original_research"),
                                               ("awaiting_research_approval", "review")])
def test_upload_is_refused_while_running_after_completion_or_for_reviews(status, paper_type):
    value = user_run(status)
    value["artifacts"]["settings"]["paper_type"] = paper_type
    with pytest.raises(HTTPException) as exc:
        attach(value)
    assert exc.value.status_code == 409


def test_same_file_twice_and_row_limit_are_refused():
    value = attach(user_run())
    with pytest.raises(HTTPException, match="đã được tải lên"):
        attach(value)
    small = user_run()
    small["artifacts"]["settings"]["max_rows"] = 2
    with pytest.raises(HTTPException, match="vượt giới hạn"):
        attach(small)


async def test_collection_can_use_only_team_data(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("public search must not run")
    monkeypatch.setattr(data, "discover_gap_datasets", forbidden)
    value = attach(user_run())
    value["artifacts"]["data_preferences"] = {"public_search": False}
    result = await data.collect(value)
    assert [s["source_key"] for s in result["sources"]] == ["s01"] and result["user_sources_only"]


async def test_collection_keeps_team_data_alongside_public_search(monkeypatch):
    calls = []
    async def no_results(context, **kwargs):
        calls.append(context)
        return {"search": {}, "datasets": []}
    monkeypatch.setattr(data, "discover_gap_datasets", no_results)
    value = attach(user_run())
    # A previous collection that contained the same upload must not duplicate it.
    value["artifacts"]["data_collection"] = {"sources": list(value["artifacts"]["user_sources"])}
    result = await data.collect(value)
    assert calls and [s["source_key"] for s in result["sources"]] == ["s01"]


# ------------------------------------------------------------- analysis & provenance

def team_and_public_collection():
    value = run()
    yields = ("tinh;nam;nang_suat\n" + "".join(f"Tỉnh A;{y};{v}\n" for y, v in
              zip(range(2020, 2026), ["3,0", "3,5", "4,0", "3,8", "5,0", "4,5"])))
    value = attach(user_run(), text=yields, name="nang_suat.csv")
    team = value["artifacts"]["user_sources"][0]
    public = data.save_snapshot(pd.DataFrame({"province": ["tỉnh a"] * 6, "year": list(range(2020, 2026)),
                                              "temp": [25., 26., 24., 27., 28., 26.5]}), value, "s02")
    public.update(candidate={"dataset_id": "test/climate", "dataset_url": "https://huggingface.co/datasets/test/climate"},
                  metadata={"sampled": False})
    return {"sources": [team, public]}


def team_design(transform="numeric"):
    return AnalysisDesign(adequate=True, rationale="Ghép theo tỉnh/năm", sources=[
        {"source_key": "s01", "bindings": [binding("province", "tinh", "name", transform="casefold"),
                                           binding("year", "nam", "year", transform="numeric"),
                                           binding("yield", "nang_suat", "t/ha", transform=transform)]},
        {"source_key": "s02", "bindings": [binding("province", "province", "name", transform="casefold"),
                                           binding("year", "year", "year"), binding("temperature", "temp", "C")]}],
        joins=[{"right_source": "s02", "keys": ["province", "year"], "cardinality": "one_to_one"}],
        method="ols", target="yield", predictors=["temperature"]).model_dump()


def test_team_upload_joins_public_source_and_reaches_ols_with_cleaning_in_methods():
    from app.data_research.service import analyze_dataframe
    collection = team_and_public_collection()
    frame, report = data.prepare_data(1, brief(), team_design(), collection, 100)
    assert report["joins"][0]["matched_rows"] == 6
    team = report["provenance"][0]
    assert team["origin"] == "user_upload" and team["filename"] == "nang_suat.csv" and team["cleaning_summary"]
    result = analyze_dataframe(frame, {"dataset_id": "upload:nang_suat.csv + test/climate", "data_origin": "mixed",
                                       "source": "Dữ liệu do nhóm nghiên cứu cung cấp + Hugging Face"},
                               target_column="yield", predictor_columns=["temperature"],
                               column_roles={"province": "identifier", "year": "identifier",
                                             "yield": "continuous", "temperature": "continuous"},
                               correlation_columns=["yield", "temperature"])
    assert result["ols_regression"]["status"] == "ok" and result["ols_regression"]["n"] == 6
    assert "dataset công khai" not in result["methods_draft"].split(".")[0]
    methods = pipeline.source_methods_text(report["provenance"])
    assert "do nhóm nghiên cứu cung cấp" in methods and "nang_suat.csv" in methods and "SHA-256" in methods
    assert "test/climate" in methods


def test_ambiguous_column_cannot_be_forced_to_numbers_by_the_design():
    value = user_run()
    raw = "tinh;nam;nang_suat\n" + "".join(f"A;{y};1.234\n" for y in range(2020, 2026))
    frame, log = uploads.parse_and_clean(raw.encode(), "x.csv")
    value = uploads.attach_user_source(value, frame, log, description="Dữ liệu thử nghiệm của nhóm")
    collection = {"sources": value["artifacts"]["user_sources"]}
    with pytest.raises(ValueError, match="mơ hồ"):
        data.prepare_data(1, brief(), team_design(), collection, 100)


# ------------------------------------------------------------------------------ API

@pytest.fixture
def api(monkeypatch):
    state = {"run": user_run()}

    async def get(run_id):
        return state["run"]

    async def mutate(run_id, expected_version, change):
        if expected_version != state["run"]["version"]:
            raise HTTPException(409, "stale")
        state["run"] = change(state["run"])
        state["run"]["version"] += 1
        return state["run"]

    monkeypatch.setattr(store, "get", get)
    monkeypatch.setattr(store, "mutate", mutate)
    app = FastAPI()
    app.include_router(autonomous_router.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"), state


async def test_upload_endpoint_cleans_and_attaches_then_removes(api):
    client, state = api
    async with client:
        response = await client.post("/autonomous/runs/1/sources/upload",
            data={"expected_version": "5", "description": "Khảo sát của nhóm năm 2021", "number_format": "auto"},
            files={"file": ("khao_sat.csv", VN_CSV.encode("utf-8"), "text/csv")})
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["artifacts"]["user_sources"][0]["metadata"]["cleaning"]["output_shape"] == {"rows": 4, "columns": 5}
        removed = await client.post("/autonomous/runs/1/sources/s01/remove", json={"expected_version": 6})
        assert removed.status_code == 200 and removed.json()["artifacts"]["user_sources"] == []


async def test_upload_endpoint_reports_bad_files_and_stale_versions(api):
    client, state = api
    async with client:
        bad = await client.post("/autonomous/runs/1/sources/upload",
            data={"expected_version": "5", "description": "Mô tả đủ dài cho dữ liệu"},
            files={"file": ("x.exe", b"a,b\n1,2\n", "application/octet-stream")})
        assert bad.status_code == 422 and "Chỉ hỗ trợ" in bad.json()["detail"]
        stale = await client.post("/autonomous/runs/1/sources/upload",
            data={"expected_version": "4", "description": "Mô tả đủ dài cho dữ liệu"},
            files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
        assert stale.status_code == 409
        short = await client.post("/autonomous/runs/1/sources/upload",
            data={"expected_version": "5", "description": "ngắn"},
            files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
        assert short.status_code == 422


def test_single_upload_needs_no_join_key_and_text_controls_are_dropped_not_blocking():
    from app.autonomous.models import ResearchBrief
    plan = ResearchBrief(
        question="Số năm đi học liên hệ thế nào với lương theo giờ năm 1985?", gap="Kiểm thử.", evidence_paper_ids=[1],
        variables=[{"name": "wage", "role": "dependent", "description": "Lương"}], geography="United States", time_scope="1985",
        data_needs=[{"key": k, "concept": k, "role": r, "unit": u, "resolution": "individual", "search_terms": [k]}
                    for k, r, u in [("person_id", "join_key", "id"), ("wage", "outcome", "USD/hour"),
                                    ("schooling", "exposure", "years"), ("experience", "control", "years"),
                                    ("sex", "control", "group")]], feasibility_notes="Một nguồn cá nhân.").model_dump()
    rows = "".join(f"{w};{e};{x};{'male' if i % 2 else 'female'}\n"
                   for i, (w, e, x) in enumerate([("5,1", 8, 21), ("6,7", 12, 1), ("4", 12, 4), ("7,5", 12, 17),
                                                  ("13,1", 16, 9), ("4,45", 10, 27), ("19,5", 18, 12)]))
    frame, log = uploads.parse_and_clean(("luong;hoc;kn;gioi\n" + rows).encode(), "cps.csv")
    value = uploads.attach_user_source(user_run(), frame, log, description="Mẫu CPS 1985 của nhóm")
    collection = {"sources": value["artifacts"]["user_sources"]}
    spec = AnalysisDesign(adequate=True, rationale="Một nguồn", sources=[{"source_key": "s01", "bindings": [
        binding("wage", "luong", "USD/hour"), binding("schooling", "hoc", "years"),
        binding("experience", "kn", "years"), binding("sex", "gioi", "group")]}],
        method="ols", target="wage", predictors=["schooling", "experience", "sex"]).model_dump()
    with pytest.raises(ValueError, match="phải là cột số"):
        data.prepare_data(1, plan, spec, collection, 100)
    frame, report = data.prepare_data(1, plan, spec, collection, 100, drop_optional_predictors=True)
    assert report["predictors"] == ["schooling", "experience"]
    assert report["dropped_predictors"][0]["variable"] == "sex"
    # A predictor that is not an optional planned variable is never silently dropped.
    with pytest.raises(ValueError, match="không hợp lệ"):
        data.prepare_data(1, plan, dict(spec, predictors=["schooling", "invented"]), collection, 100, True)


async def test_design_stage_freezes_the_reduced_model_and_passes_validation(monkeypatch):
    from app.autonomous.models import ResearchBrief
    from app.autonomous.state import fingerprint
    plan = ResearchBrief(
        question="Số năm đi học liên hệ thế nào với lương theo giờ năm 1985?", gap="Kiểm thử.", evidence_paper_ids=[1],
        variables=[{"name": "wage", "role": "dependent", "description": "Lương"}], geography="United States", time_scope="1985",
        data_needs=[{"key": k, "concept": k, "role": r, "unit": u, "resolution": "individual", "search_terms": [k]}
                    for k, r, u in [("person_id", "join_key", "id"), ("wage", "outcome", "USD/hour"),
                                    ("schooling", "exposure", "years"), ("sex", "control", "group")]],
        feasibility_notes="Một nguồn cá nhân.").model_dump()
    rows = "".join(f"{w};{e};{'male' if i % 2 else 'female'}\n" for i, (w, e) in
                   enumerate([("5,1", 8), ("6,7", 12), ("4", 11), ("7,5", 13), ("13,1", 16), ("4,45", 10), ("19,5", 18)]))
    frame, log = uploads.parse_and_clean(("luong;hoc;gioi\n" + rows).encode(), "cps.csv")
    value = uploads.attach_user_source(user_run(), frame, log, description="Mẫu CPS 1985 của nhóm")
    value["artifacts"].update(research_plan=plan, data_collection={"sources": value["artifacts"]["user_sources"]},
                              approvals=[{"stage": "research_plan", "fingerprint": fingerprint(plan)}])

    async def llm(prompt, schema):
        return AnalysisDesign(adequate=True, rationale="Một nguồn", method="ols", target="wage",
                              predictors=["schooling", "sex"], sources=[{"source_key": "s01", "bindings": [
                                  binding("wage", "luong", "USD/hour"), binding("schooling", "hoc", "years"),
                                  binding("sex", "gioi", "group")]}])
    monkeypatch.setattr(pipeline, "generate_structured", llm)
    result = await pipeline.design(value)
    assert value["artifacts"]["design_validation"]["ok"] is True
    assert result["predictors"] == ["schooling"] and any("sex" in item for item in result["limitations"])


async def test_design_accepts_same_unit_written_differently_but_not_undeclared_conversions(monkeypatch):
    from app.autonomous.models import ResearchBrief
    from app.autonomous.state import fingerprint
    plan = ResearchBrief(
        question="Số năm đi học liên hệ thế nào với lương theo giờ năm 1985?", gap="Kiểm thử.", evidence_paper_ids=[1],
        variables=[{"name": "wage", "role": "dependent", "description": "Lương"}], geography="United States", time_scope="1985",
        data_needs=[{"key": k, "concept": k, "role": r, "unit": u, "resolution": "cá nhân", "search_terms": [k]}
                    for k, r, u in [("person_id", "join_key", "mã"), ("wage", "outcome", "USD/giờ"),
                                    ("schooling", "exposure", "năm"), ("region", "exposure", "phân loại")]],
        feasibility_notes="Một nguồn cá nhân.").model_dump()
    rows = "".join(f"{w};{e};{'south' if i % 2 else 'other'}\n" for i, (w, e) in
                   enumerate([("5,1", 8), ("6,7", 12), ("4", 11), ("7,5", 13), ("13,1", 16), ("4,45", 10), ("19,5", 18)]))
    frame, log = uploads.parse_and_clean(("luong;hoc;vung\n" + rows).encode(), "cps.csv")
    value = uploads.attach_user_source(user_run(), frame, log, description="Mẫu CPS 1985 của nhóm")
    value["artifacts"].update(research_plan=plan, data_collection={"sources": value["artifacts"]["user_sources"]},
                              approvals=[{"stage": "research_plan", "fingerprint": fingerprint(plan)}])
    english = lambda var, col, unit, **kw: {"variable": var, "column": col, "source_unit": unit, "target_unit": unit,
                                            "rationale": "Cột gốc", **kw}
    proposal = {"bindings": [english("wage", "luong", "USD/hour"), english("schooling", "hoc", "years"),
                             english("region", "vung", "category")]}

    async def llm(prompt, schema):
        return AnalysisDesign(adequate=True, rationale="Một nguồn", method="ols", target="wage",
                              predictors=["schooling", "region"], sources=[{"source_key": "s01", **proposal}])
    monkeypatch.setattr(pipeline, "generate_structured", llm)
    result = await pipeline.design(value)
    assert value["artifacts"]["design_validation"]["ok"] is True
    assert result["predictors"] == ["schooling"]
    assert {b["target_unit"] for b in result["sources"][0]["bindings"]} == {"USD/giờ", "năm", "phân loại"}
    # A declared conversion keeps its labels, so a wrong target unit is still refused.
    proposal["bindings"][0] = english("wage", "luong", "cents/hour", scale=0.01)
    proposal["bindings"][0]["target_unit"] = "USD/hour"
    await pipeline.design(value)
    assert value["artifacts"]["design_validation"]["ok"] is False


def test_design_bindings_that_name_columns_are_mapped_to_plan_keys():
    from app.autonomous.models import ResearchBrief
    from app.autonomous.pipeline import repair_binding_variables
    brief = ResearchBrief(
        question="Kinh nghiệm và tuổi liên hệ thế nào với lương theo giờ năm 1985?", gap="Kiểm thử.", evidence_paper_ids=[1],
        variables=[{"name": "wage", "role": "dependent", "description": "Lương"}], geography="United States", time_scope="1985",
        data_needs=[{"key": "hourly_wage", "concept": "Tiền lương theo giờ", "role": "outcome", "unit": "USD/giờ",
                     "resolution": "cấp cá nhân", "search_terms": ["wage"]},
                    {"key": "work_experience", "concept": "Số năm kinh nghiệm làm việc", "role": "exposure", "unit": "năm",
                     "resolution": "cấp cá nhân", "search_terms": ["experience"]},
                    {"key": "age", "concept": "Tuổi của người lao động", "role": "exposure", "unit": "năm",
                     "resolution": "cấp cá nhân", "required": False, "search_terms": ["age"]}],
        feasibility_notes="Một nguồn cá nhân.")
    proposed = AnalysisDesign(adequate=True, rationale="Một nguồn", sources=[{"source_key": "s01", "bindings": [
        binding("Lương giờ (USD)", "Lương giờ (USD)", "USD/giờ"), binding("Kinh nghiệm (năm)", "Kinh nghiệm (năm)", "năm"),
        binding("Tuổi", "Tuổi", "năm")]}], method="ols", target="Lương giờ (USD)",
        predictors=["Kinh nghiệm (năm)", "Tuổi"])
    fixed = repair_binding_variables(proposed, brief)
    assert [b.variable for b in fixed.sources[0].bindings] == ["hourly_wage", "work_experience", "age"]
    assert fixed.target == "hourly_wage" and fixed.predictors == ["work_experience", "age"]
    # A column that matches no planned concept stays unmapped and is still reported as invalid.
    odd = AnalysisDesign(adequate=True, rationale="x", sources=[{"source_key": "s01", "bindings": [
        binding("hourly_wage", "Lương giờ (USD)", "USD/giờ"), binding("union", "union", "group")]}], method="ols")
    assert [b.variable for b in repair_binding_variables(odd, brief).sources[0].bindings] == ["hourly_wage", "union"]


def test_filters_on_row_identifier_columns_are_dropped():
    from app.autonomous.pipeline import drop_row_identifier_filters
    collection = {"sources": [{"source_key": "s01", "rows": 534, "columns": [
        {"name": "rownames", "unique": 534}, {"name": "region", "unique": 2}, {"name": "wage", "unique": 238}]}]}
    proposed = AnalysisDesign(adequate=True, rationale="x", method="ols", sources=[{"source_key": "s01",
        "filters": [{"column": "rownames", "operator": "between", "values": [1, 534], "rationale": "giữ toàn bộ"},
                    {"column": "region", "operator": "in", "values": ["south"], "rationale": "vùng"}],
        "bindings": [binding("wage", "wage", "USD/giờ")]}])
    fixed = drop_row_identifier_filters(proposed, collection)
    assert [f.column for f in fixed.sources[0].filters] == ["region"]
    assert any("rownames" in item for item in fixed.assumptions)


def test_duplicate_binding_is_not_moved_to_the_next_closest_concept():
    from app.autonomous.models import ResearchBrief
    from app.autonomous.pipeline import repair_binding_variables
    brief = ResearchBrief(
        question="GDP bình quân đầu người liên hệ thế nào với tuổi thọ năm 2019?", gap="Kiểm thử.", evidence_paper_ids=[1],
        variables=[{"name": "le", "role": "dependent", "description": "Tuổi thọ"}], geography="Thế giới", time_scope="2019",
        data_needs=[{"key": "life_expectancy", "concept": "Tuổi thọ trung bình", "role": "outcome", "unit": "năm",
                     "resolution": "quốc gia", "search_terms": ["life"]},
                    {"key": "gdp_per_capita", "concept": "GDP bình quân đầu người", "role": "exposure", "unit": "USD/người",
                     "resolution": "quốc gia", "search_terms": ["gdp"]},
                    {"key": "co2_per_capita", "concept": "Phát thải CO2 bình quân đầu người", "role": "control", "unit": "tấn/người",
                     "resolution": "quốc gia", "required": False, "search_terms": ["co2"]}],
        feasibility_notes="Kiểm thử.")
    proposed = AnalysisDesign(adequate=True, rationale="x", method="ols", sources=[{"source_key": "s01", "bindings": [
        binding("life_expectancy", "le", "năm"), binding("gdp_per_capita", "gdppc_ppp", "USD/người"),
        binding("GDP bình quân đầu người (PPP)", "gdppc_ppp", "USD/người")]}])
    fixed = repair_binding_variables(proposed, brief)
    assert [b.variable for b in fixed.sources[0].bindings][2] == "GDP bình quân đầu người (PPP)"
