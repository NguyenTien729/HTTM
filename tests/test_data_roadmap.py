"""World Bank source, data feasibility probe and data-grounded alternative questions.

All network calls are replaced by fixtures shaped like the real World Bank API v2
responses ([metadata, rows]); no request leaves the test process.
"""
import json
import os

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

import pandas as pd
import pytest

from app.autonomous import data, pipeline, uploads
from app.autonomous.models import (AlternativeQuestions, AnalysisDesign, Decision, ResearchBrief)
from app.autonomous.state import decide
from app.data_research import worldbank
from tests.test_autonomous import binding, run

CATALOG = [
    {"id": "NY.GDP.PCAP.CD", "name": "GDP per capita (current US$)", "source": {"id": "2"},
     "sourceNote": "GDP per capita is gross domestic product divided by midyear population.",
     "topics": [{"id": "3", "value": "Economy & Growth"}]},
    {"id": "BX.KLT.DINV.WD.GD.ZS", "name": "Foreign direct investment, net inflows (% of GDP)", "source": {"id": "2"},
     "sourceNote": "Foreign direct investment are the net inflows of investment.", "topics": []},
    {"id": "AG.YLD.CREL.KG", "name": "Cereal yield (kg per hectare)", "source": {"id": "2"},
     "sourceNote": "Cereal yield includes wheat, rice, maize, barley.", "topics": []},
    {"id": "SP.POP.TOTL", "name": "Population, total", "source": {"id": "2"}, "sourceNote": "", "topics": []},
]
COUNTRIES = [
    {"id": "VNM", "iso2Code": "VN", "name": "Viet Nam", "region": {"id": "EAS", "value": "East Asia & Pacific"}},
    {"id": "THA", "iso2Code": "TH", "name": "Thailand", "region": {"id": "EAS", "value": "East Asia & Pacific"}},
    {"id": "IDN", "iso2Code": "ID", "name": "Indonesia", "region": {"id": "EAS", "value": "East Asia & Pacific"}},
    {"id": "EAS", "iso2Code": "Z4", "name": "East Asia & Pacific", "region": {"id": "NA", "value": "Aggregates"}},
]


def series(indicator, start, end, base):
    rows = []
    for offset, iso3 in enumerate(["VNM", "THA", "IDN", "EAS"]):
        for year in range(start, end + 1):
            value = None if (iso3 == "IDN" and year == start) else base * (1 + offset) + (year - start) * 10
            rows.append({"indicator": {"id": indicator, "value": indicator + " name"},
                         "country": {"id": iso3[:2], "value": iso3}, "countryiso3code": iso3,
                         "date": str(year), "value": value})
    return [{"page": 1, "pages": 1, "per_page": 20000, "total": len(rows)}, rows]


@pytest.fixture
def api(monkeypatch):
    calls = []

    async def fake(path, params):
        calls.append((path, dict(params)))
        if path == "sources/2/indicators":
            return [{"page": 1, "pages": 1}, CATALOG]
        if path == "country":
            return [{"page": 1, "pages": 1}, COUNTRIES]
        if path.startswith("country/all/indicator/"):
            indicator = path.rsplit("/", 1)[-1]
            start, end = map(int, params["date"].split(":"))
            return series(indicator, start, end, 1000 if indicator.startswith("NY") else 2)
        raise worldbank.WorldBankError("unexpected path " + path)

    monkeypatch.setattr(worldbank, "_get_json", fake)
    return calls


def country_brief(**changes):
    needs = [("country", "Country", "join_key", "ISO3", ["country code"]),
             ("year", "Year", "join_key", "year", ["year"]),
             ("gdp_pc", "GDP per capita", "outcome", "current US$", ["GDP per capita"]),
             ("fdi", "Foreign direct investment inflows", "exposure", "% of GDP", ["foreign direct investment net inflows"]),
             ("pop", "Population", "control", "people", ["population total"])]
    value = dict(question="FDI liên hệ thế nào với GDP bình quân đầu người ở Đông Nam Á?", gap="Kiểm thử.",
                 evidence_paper_ids=[1], variables=[{"name": "gdp", "role": "dependent", "description": "GDP"}],
                 geography="Vietnam", geography_terms=["Vietnam"], time_scope="2015–2020",
                 data_needs=[{"key": k, "concept": c, "role": r, "unit": u, "resolution": "country-year", "search_terms": t}
                             for k, c, r, u, t in needs], feasibility_notes="Dữ liệu quốc gia × năm.")
    value.update(changes)
    return ResearchBrief(**value).model_dump()


# ---------------------------------------------------------------- World Bank

async def test_indicator_search_ranks_by_name_and_reports_country_scope(api):
    found = await worldbank.search_indicators(["GDP per capita"], "Vietnam")
    assert found[0]["dataset_id"] == "worldbank:NY.GDP.PCAP.CD"
    assert found[0]["scope_match"] == "mentioned_in_metadata" and "Viet Nam" in found[0]["relevance_note"]
    assert found[0]["license"].startswith("CC BY")
    # Weak matches (one word out of three in the note only) are not candidates.
    assert await worldbank.search_indicators(["smartphone screen time"], "Vietnam") == []


async def test_download_drops_aggregates_and_missing_values_within_requested_years(api):
    frame, meta = await worldbank.download_indicator("worldbank:NY.GDP.PCAP.CD", "2015–2020", max_rows=1000)
    assert set(frame["country_iso3"]) == {"VNM", "THA", "IDN"}
    assert frame["year"].between(2015, 2020).all() and len(frame) == 3 * 6 - 1
    assert api[-1][1]["date"] == "2015:2020" and meta["economies"] == 3 and not meta["sampled"]
    trimmed, meta = await worldbank.download_indicator("worldbank:NY.GDP.PCAP.CD", "2015–2020", max_rows=9)
    assert len(trimmed) <= 9 and meta["years_trimmed_for_row_limit"][0] == 2015


async def test_catalog_falls_back_to_full_list_and_backs_off_after_failure(monkeypatch, tmp_path):
    async def full_only(path, params):
        if path == "sources/2/indicators":
            raise worldbank.WorldBankError("down")
        return [{"page": 1}, CATALOG + [{"id": "X.OTHER", "name": "GDP per capita other", "source": {"id": "11"}}]]
    monkeypatch.setattr(worldbank, "_get_json", full_only)
    catalog = await worldbank.indicator_catalog()
    assert {item["id"] for item in catalog} == {item["id"] for item in CATALOG}

    calls = []
    async def broken(path, params):
        calls.append(path)
        raise worldbank.WorldBankError("down")
    monkeypatch.setattr(worldbank, "_memory", {})
    monkeypatch.setattr(worldbank, "CACHE_DIR", tmp_path / "empty-cache")
    monkeypatch.setattr(worldbank, "_get_json", broken)
    with pytest.raises(worldbank.WorldBankError):
        await worldbank.indicator_catalog()
    count = len(calls)
    with pytest.raises(worldbank.WorldBankError, match="tạm bỏ qua"):
        await worldbank.indicator_catalog()
    assert len(calls) == count


@pytest.fixture
def snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "ROOT", tmp_path)


def wb_run(**artifacts):
    value = run()
    value["artifacts"].update(settings={"max_rows": 1000, "max_sources": 4, "collection_seconds": 30,
                                        "paper_type": "original_research"}, research_plan=country_brief(), **artifacts)
    for key in ("design", "design_validation", "analysis", "verification"):
        value["artifacts"].pop(key, None)
    return value


async def test_collection_downloads_world_bank_indicators_and_they_join_into_ols(api, snapshots, monkeypatch):
    async def nothing(context, **kwargs):
        return {"search": {}, "datasets": []}
    monkeypatch.setattr(data, "discover_gap_datasets", nothing)
    collection = await data.collect(wb_run())
    by_id = {s["candidate"]["dataset_id"]: s for s in collection["sources"]}
    assert {"worldbank:NY.GDP.PCAP.CD", "worldbank:BX.KLT.DINV.WD.GD.ZS"} <= set(by_id)
    gdp, fdi = by_id["worldbank:NY.GDP.PCAP.CD"], by_id["worldbank:BX.KLT.DINV.WD.GD.ZS"]
    assert gdp["metadata"]["origin"] == "world_bank"
    spec = AnalysisDesign(adequate=True, rationale="Quốc gia × năm", sources=[
        {"source_key": gdp["source_key"], "bindings": [binding("country", "country_iso3", "ISO3", transform="strip"),
                                                        binding("year", "year", "year", transform="numeric"),
                                                        binding("gdp_pc", "NY.GDP.PCAP.CD", "current US$")]},
        {"source_key": fdi["source_key"], "bindings": [binding("country", "country_iso3", "ISO3", transform="strip"),
                                                        binding("year", "year", "year", transform="numeric"),
                                                        binding("fdi", "BX.KLT.DINV.WD.GD.ZS", "% of GDP")]}],
        joins=[{"right_source": fdi["source_key"], "keys": ["country", "year"], "cardinality": "one_to_one"}],
        method="ols", target="gdp_pc", predictors=["fdi"]).model_dump()
    frame, report = data.prepare_data(1, country_brief(), spec, collection, 1000)
    assert report["joins"][0]["matched_rows"] == len(frame) == 17
    assert report["complete_analysis_rows"] == 17


# ------------------------------------------------------------ feasibility probe

async def test_feasibility_probe_reports_candidates_per_variable(api, monkeypatch):
    async def hub(context, **kwargs):
        if "Population" in context["project_topic"]:
            return {"datasets": [{"dataset_id": "owner/pop", "title": "World population", "source": "Hugging Face"}]}
        return {"datasets": []}
    monkeypatch.setattr(data, "discover_gap_datasets", hub)
    result = await data.probe_feasibility(country_brief(), seconds=20)
    rows = {row["key"]: row for row in result["needs"]}
    assert set(rows) == {"gdp_pc", "fdi", "pop"}  # join keys are not probed
    assert rows["gdp_pc"]["candidates"][0]["dataset_id"] == "worldbank:NY.GDP.PCAP.CD"
    assert "owner/pop" in {c["dataset_id"] for c in rows["pop"]["candidates"]}
    assert result["verdict"] == "likely" and result["missing_required"] == []


async def test_feasibility_flags_missing_required_variables_and_team_data(monkeypatch):
    async def hub(context, **kwargs):
        return {"datasets": []}
    monkeypatch.setattr(data, "discover_gap_datasets", hub)  # World Bank is offline (conftest)
    result = await data.probe_feasibility(country_brief(), seconds=20)
    assert result["verdict"] == "at_risk"
    assert set(result["missing_required"]) == {"GDP per capita", "Foreign direct investment inflows"}
    assert any(e.get("source") == "World Bank" for e in result["errors"])
    assert (await data.probe_feasibility(country_brief(), user_sources=[{"source_key": "s01"}], seconds=20))["verdict"] == "team_data"


async def test_research_plan_uses_team_data_schema_and_stores_feasibility(snapshots, monkeypatch):
    prompts = []
    async def corpus(project_id):
        return [{"id": 1, "title": "Paper"}]
    async def plan(prompt, schema):
        prompts.append(prompt)
        return ResearchBrief(**country_brief())
    async def probe(brief, user_sources, seconds):
        return {"verdict": "team_data", "needs": [], "user_sources": len(user_sources)}
    monkeypatch.setattr(pipeline, "evidence", corpus)
    monkeypatch.setattr(pipeline, "generate_structured", plan)
    monkeypatch.setattr(data, "probe_feasibility", probe)
    value = wb_run()
    frame, log = uploads.parse_and_clean("ma_tinh;nang_suat_lua\n01;5,8\n02;6,1\n".encode(), "lua.csv")
    value = uploads.attach_user_source(value, frame, log, description="Năng suất lúa theo tỉnh của nhóm")
    await pipeline.research_plan(value)
    assert "nang_suat_lua" in prompts[0] and "team_upload" in prompts[0]
    assert value["artifacts"]["data_feasibility"] == {"verdict": "team_data", "needs": [], "user_sources": 1}


# ------------------------------------------------------- alternative questions

def profile_collection():
    columns = lambda *pairs: [{"name": n, "dtype": d} for n, d in pairs]
    return {"sources": [
        {"source_key": "s01", "rows": 30, "columns": columns(("country_iso3", "object"), ("year", "int64"), ("NY.GDP.PCAP.CD", "float64")),
         "sample": [], "metadata": {}, "candidate": {"dataset_id": "worldbank:NY.GDP.PCAP.CD", "title": "GDP"}},
        {"source_key": "s02", "rows": 30, "columns": columns(("country_iso3", "object"), ("year", "int64"), ("fdi", "float64"), ("name", "str")),
         "sample": [], "metadata": {}, "candidate": {"dataset_id": "worldbank:FDI", "title": "FDI"}}]}


def proposal(*items):
    return AlternativeQuestions(alternatives=[{
        "question": q, "rationale": "Có dữ liệu", "scope_change": "Đổi sang cấp quốc gia",
        "outcome": {"source_key": "s01", "column": out, "concept": "GDP"},
        "exposures": [{"source_key": key, "column": col, "concept": "FDI"}], "join_columns": joins}
        for q, out, key, col, joins in items])


def test_only_alternatives_backed_by_real_numeric_joinable_columns_survive():
    valid, rejected = pipeline.validate_alternatives(proposal(
        ("FDI liên hệ với GDP bình quân đầu người?", "NY.GDP.PCAP.CD", "s02", "fdi", ["country_iso3", "year"]),
        ("Cột bịa ra có liên hệ với GDP không?", "NY.GDP.PCAP.CD", "s02", "invented", ["country_iso3"]),
    ), profile_collection())
    more_valid, more_rejected = pipeline.validate_alternatives(proposal(
        ("Tên quốc gia liên hệ với GDP không?", "NY.GDP.PCAP.CD", "s02", "name", ["country_iso3"]),
        ("Ghép hai nguồn mà không khai báo khóa?", "NY.GDP.PCAP.CD", "s02", "fdi", []),
    ), profile_collection())
    assert more_valid == []
    rejected += more_rejected
    assert [v["question"] for v in valid] == ["FDI liên hệ với GDP bình quân đầu người?"]
    assert valid[0]["source_keys"] == ["s01", "s02"]
    reasons = " ".join(r["reason"] for r in rejected)
    assert "không có trong snapshot" in reasons and "không phải biến số" in reasons and "khóa ghép" in reasons


async def test_suggest_alternatives_sends_real_schema_and_filters_output(monkeypatch):
    prompts = []
    async def corpus(project_id):
        return [{"id": 1, "title": "Paper", "key_finding": "x"}]
    async def llm(prompt, schema):
        prompts.append(prompt)
        assert schema is AlternativeQuestions
        return proposal(("FDI liên hệ với GDP bình quân đầu người?", "NY.GDP.PCAP.CD", "s02", "fdi", ["country_iso3", "year"]),
                        ("Câu hỏi sai cột?", "missing", "s02", "fdi", ["country_iso3"]))
    monkeypatch.setattr(pipeline, "evidence", corpus)
    monkeypatch.setattr(pipeline, "generate_structured", llm)
    result = await pipeline.suggest_alternatives(wb_run(), ResearchBrief(**country_brief()), profile_collection(), ["Thiếu biến"])
    assert "NY.GDP.PCAP.CD" in prompts[0] and len(result["alternatives"]) == 1 and len(result["rejected"]) == 1


def test_choosing_an_alternative_keeps_downloaded_sources_only_when_asked():
    value = wb_run(data_collection={"sources": [{"source_key": "s01"}]}, design={"adequate": False}, design_validation={"ok": False})
    value.update(status="awaiting_design_approval", stage="design")
    kept = decide(value, Decision(expected_version=5, action="revise", notes="Dùng câu hỏi thay thế",
                                  restart_from="research_plan", keep_sources=True))
    assert kept["stage"] == "research_plan" and kept["artifacts"]["data_collection"] == {"sources": [{"source_key": "s01"}]}
    assert "design" not in kept["artifacts"]
    dropped = decide(value, Decision(expected_version=5, action="revise", notes="Đổi câu hỏi", restart_from="research_plan"))
    assert "data_collection" not in dropped["artifacts"]


@pytest.mark.parametrize("resolution,searched", [
    ("country-year", True), ("quốc gia × năm", True), ("national, annual", True),
    ("cấp cá nhân, năm 1985", False), ("individual", False), ("tỉnh × năm", False), ("household", False)])
def test_world_bank_is_only_searched_for_country_level_variables(resolution, searched):
    assert data.worldbank_resolution(resolution) is searched


async def test_research_plan_prompt_states_the_analysis_limits(snapshots, monkeypatch):
    prompts = []
    async def corpus(project_id):
        return [{"id": 1, "title": "Paper"}]
    async def plan(prompt, schema):
        prompts.append(prompt)
        return ResearchBrief(**country_brief())
    async def probe(brief, user_sources, seconds):
        return {"verdict": "likely", "needs": []}
    monkeypatch.setattr(pipeline, "evidence", corpus)
    monkeypatch.setattr(pipeline, "generate_structured", plan)
    monkeypatch.setattr(data, "probe_feasibility", probe)
    await pipeline.research_plan(wb_run())
    assert "không đặt giả thuyết phi tuyến" in prompts[0] and "không cần join_key" in prompts[0]
