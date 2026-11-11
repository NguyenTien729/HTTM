import asyncio
import io
import zipfile
import httpx
import pandas as pd
import pytest
from app.data_research import repositories as repo, service


def test_repository_ids_are_explicit_and_do_not_accept_arbitrary_urls():
    assert service.validate_dataset_id("mendeley:9krwng5dm7:1") == "mendeley:9krwng5dm7:1"
    for value in ("mendeley:https://localhost:1", "https://localhost/data.csv", "mendeley:../private:1"):
        with pytest.raises(service.DataResearchError):
            service.validate_dataset_id(value)


def test_excel_and_csv_are_parsed_without_executing_content():
    frame = pd.DataFrame({"score": [1, 2, 3], "label": ["A", "B", "A"]})
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    assert repo.parse_table(buffer.getvalue(), "data.xlsx", "0").equals(frame)
    assert repo.parse_table(frame.to_csv(index=False).encode(), "data.csv", "0").equals(frame)


async def test_external_file_download_rejects_private_redirects():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(service.DataResearchError):
            await repo.read_file(client, "mendeley:9krwng5dm7:1", {"id": "a"*36, "size": 10})


async def test_download_preserves_rows_and_source_and_samples_deterministically(monkeypatch):
    file_id = "12345678-1234-1234-1234-123456789abc"
    async def metadata(*args):
        return {"name": "Survey", "doi": "10.17632/9krwng5dm7.1", "description": "Public survey", "licence": {"short_name": "CC BY 4.0"}}, [{"id": file_id, "filename": "data.csv"}]
    async def read(*args):
        return pd.DataFrame({"score": range(150)}).to_csv(index=False).encode()
    monkeypatch.setattr(repo, "metadata", metadata)
    monkeypatch.setattr(repo, "read_file", read)
    frame, meta = await service.download_dataset_dataframe("mendeley:9krwng5dm7:1", file_id, "0", 100)
    assert len(frame) == 100 and meta["rows_in_selected_split"] == 150
    result = service.analyze_dataframe(frame, meta)
    assert "Mendeley Data" in result["methods_draft"]
    assert "Hugging Face" not in result["methods_draft"]
    assert "data.csv" in result["methods_draft"]
    preview = await service.inspect_dataset("mendeley:9krwng5dm7:1")
    assert preview["num_rows_total"] == 150
    assert preview["preview_selection"]["config"] == file_id


async def test_datacite_search_uses_scope_and_only_dataset_records(monkeypatch):
    queries = []
    async def get(client, url, params, **kwargs):
        queries.append(params["query"])
        return {"data": [{"attributes": {"doi": "10.17632/9krwng5dm7.1", "titles": [{"title": "AI university students survey in Vietnam"}],
            "descriptions": [{"description": "AI student productivity survey in Vietnam"}], "rightsList": []}}]}
    monkeypatch.setattr(service, "_get_json", get)
    context = {"project_topic": "AI student productivity in Vietnam", "_search_concept_groups": [["AI".lower()], ["students", "student"], ["productivity"]], "_search_scope_terms": ["vietnam"]}
    async with httpx.AsyncClient() as client:
        found = await repo.search_mendeley(client, context, ["AI students"], 5)
    assert len(found) == 1
    assert found[0]["dataset_id"] == "mendeley:9krwng5dm7:1"
    assert all("resourceTypeGeneral:Dataset" in query for query in queries)
    assert any('"vietnam"' in query for query in queries)


def test_partial_match_is_explicit_and_region_is_not_implied():
    context = {"project_topic": "AI student productivity Vietnam", "_search_concept_groups": [["ai"], ["students"], ["productivity"]], "_search_scope_terms": ["vietnam"]}
    candidate = service._to_candidate({"id": "example/ai-students-survey"}, [])
    assert service._candidate_relevance(candidate, context)
    assert candidate["match_scope"] == "partial_topic_match"
    assert candidate["scope_match"] == "unverified"
    assert candidate["missing_topic_concepts"] == [["productivity"]]
