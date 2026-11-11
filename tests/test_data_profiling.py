import numpy as np
import pandas as pd
import pytest

from app.data_research.service import analyze_dataframe, DataResearchError


def test_roles_exclude_identifiers_and_numeric_labels_from_correlation():
    frame = pd.DataFrame({"user_id": [1, 2, 3, 4], "label": [0, 1, 0, 1],
                          "score": [1., 2., 3., 4.], "hours": [2., 4., 6., 8.]})
    result = analyze_dataframe(frame, {})
    assert result["column_roles"]["label"] == "categorical"
    assert result["correlation_columns"] == ["score", "hours"]
    assert result["correlation_spearman"]["score"]["hours"] == 1
    assert result["correlation_pair_counts"]["score"]["hours"] == 4
    assert any(c["type"] == "heatmap" for c in result["charts"])


def test_quality_normalizes_blanks_infinities_without_mutating_input():
    frame = pd.DataFrame({"score": [1., np.inf, 3.], "text": ["a", "  ", None]})
    result = analyze_dataframe(frame, {}, column_roles={"text": "text"})
    assert result["overview"]["missing_cells_total"] == 3
    quality = {x["column"]: x for x in result["quality_report"]["columns"]}
    assert quality["score"]["infinite_count"] == 1
    assert quality["text"]["blank_count"] == 1
    assert np.isinf(frame.score.iloc[1])


def test_invalid_roles_predictors_and_correlations_are_rejected():
    frame = pd.DataFrame({"score": [1., 2., 3.], "label": [0, 1, 0]})
    for options in ({"column_roles": {"absent": "text"}},
                    {"correlation_columns": ["label"]},
                    {"target_column": "score", "predictor_columns": ["score"]}):
        with pytest.raises(DataResearchError):
            analyze_dataframe(frame, {}, **options)


def test_failed_regression_is_not_claimed_in_methods():
    result = analyze_dataframe(pd.DataFrame({"y": [1., 2., 3.], "x": [1., 1., 1.]}), {},
                               target_column="y", predictor_columns=["x"])
    assert result["ols_regression"]["status"] == "not_run"
    assert "OLS được ước lượng" not in result["methods_draft"]


def test_group_inference_reports_exclusions_adjustments_and_effect():
    frame = pd.DataFrame({"group": ["B"]*3+["A"]*3+["C"],
                          "score": [1., 2., 3., 6., 8., 9., 100.]})
    result = analyze_dataframe(frame, {}, target_column="score", group_column="group")
    test = result["group_test"]
    assert test["rows_excluded"] == 1
    assert test["excluded_groups"] == ["C"]
    assert all(t["p_value_adjusted"] >= t["p_value"] for t in test["tests"])
    assert len(test["effect"]["ci_95"]) == 2


def test_gap_context_and_hc3_are_preserved():
    frame = pd.DataFrame({"x": [1., 2., 3., 4., 5., 6.], "y": [2., 5., 4., 8., 9., 13.]})
    gap = {"gap_id": 9, "description": "Example gap"}
    result = analyze_dataframe(frame, {"research_gap": gap}, target_column="y", predictor_columns=["x"])
    assert result["analysis_plan"]["research_gap"] == gap
    assert result["ols_regression"]["coefficients"][1]["hc3_std_error"] > 0


def test_download_honors_requested_rows_above_old_5000_cap(monkeypatch):
    import asyncio
    import pyarrow as pa
    import pyarrow.parquet as pq
    from types import SimpleNamespace
    from app.data_research import service

    table = pa.table({"score": range(6100)})
    class Parquet:
        metadata = SimpleNamespace(num_rows=6100)
        schema_arrow = table.schema
        def iter_batches(self, batch_size, columns):
            return table.select(columns).to_batches(max_chunksize=batch_size)
    async def splits(*args):
        return [{"config": "default", "split": "train"}]
    async def listing(*args):
        return {"parquet_files": [{"config": "default", "split": "train", "url": "https://huggingface.co/example"}]}
    monkeypatch.setattr(service, "_get_splits", splits)
    monkeypatch.setattr(service, "_get_json", listing)
    monkeypatch.setattr(service, "_RemoteParquetReader", lambda *args: None)
    monkeypatch.setattr(pq, "ParquetFile", lambda *args: Parquet())
    frame, metadata = asyncio.run(service.download_dataset_dataframe("example/demo", "default", "train", 6000))
    assert len(frame) == 6000
    assert metadata["max_rows"] == 6000
    assert metadata["rows_in_selected_split"] == 6100
    assert metadata["sampled"]


def test_analyze_resolves_gap_before_download(monkeypatch):
    import asyncio
    from app.data_research import router
    calls = []
    gap = {"gap_id": 9, "project_id": 42}
    async def resolve(project, selected):
        calls.append("gap")
        return gap
    async def download(**kwargs):
        calls.append("download")
        return pd.DataFrame({"score": [1., 2., 3.]}), {}
    async def topic(project):
        return {"project_id": 42, "topic": "Example", "topic_source": "planner"}
    monkeypatch.setattr(router, "resolve_selected_gap", resolve)
    monkeypatch.setattr(router, "download_dataset_dataframe", download)
    monkeypatch.setattr(router, "resolve_project_topic", topic)
    request = router.AnalyzeRequest(dataset_id="example/demo", project_id=42, gap_id=9,
        config="default", split="train", confirm_dataset=True)
    result = asyncio.run(router.analyze(request))
    assert calls == ["gap", "download"]
    assert result["analysis_plan"]["research_gap"] == gap


def test_parquet_projection_keeps_nested_metadata_without_binary_payloads():
    import pyarrow as pa
    import pyarrow.parquet as pq
    from app.data_research.service import _safe_parquet_columns, _project_record
    table = pa.Table.from_pylist([
        {"image": {"bytes": b"SECRET", "path": "one.png"}, "annotation": {"label": 1}, "image_width": 640},
        {"image": None, "annotation": {"label": 2}, "image_width": 800},
    ])
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    parquet = pq.ParquetFile(pa.BufferReader(sink.getvalue()))
    columns = _safe_parquet_columns(parquet.schema_arrow)
    assert columns == ["image.path", "annotation.label", "image_width"]
    records = [_project_record(r, columns) for batch in parquet.iter_batches(columns=columns) for r in batch.to_pylist()]
    assert records[0] == {"image.path": "one.png", "annotation.label": 1, "image_width": 640}
    assert records[1]["image.path"] is None
    assert "SECRET" not in str(records)
    result = analyze_dataframe(pd.DataFrame(records), {}, column_roles={"annotation.label": "categorical"})
    assert result["overview"]["rows_analyzed"] == 2


def test_binary_only_reports_metadata_without_inventing_statistics():
    result = analyze_dataframe(pd.DataFrame({"_record_id": [1, 9]}),
        {"analysis_mode": "structure_only", "rows_in_selected_split": 100, "rows_analyzed": 2},
        target_column="image", column_roles={"image": "text"})
    assert result["dataset"]["rows_analyzed"] == 0
    assert result["overview"]["rows_in_selected_split"] == 100
    assert result["overview"]["duplicate_rows"] is None
    assert result["column_summaries"] == {}
    assert result["ols_regression"]["status"] == "not_run"


def test_binary_only_download_never_reads_image_batches(monkeypatch):
    import asyncio
    import pyarrow as pa
    import pyarrow.parquet as pq
    from types import SimpleNamespace
    from app.data_research import service
    class Parquet:
        metadata = SimpleNamespace(num_rows=1234)
        schema_arrow = pa.schema([("image", pa.struct([("bytes", pa.binary())]))])
        def iter_batches(self, **kwargs):
            raise AssertionError("Binary payload must not be read")
    async def splits(*args):
        return [{"config": "default", "split": "train"}]
    async def listing(*args):
        return {"parquet_files": [{"config": "default", "split": "train", "url": "https://huggingface.co/example"}]}
    monkeypatch.setattr(service, "_get_splits", splits)
    monkeypatch.setattr(service, "_get_json", listing)
    monkeypatch.setattr(service, "_RemoteParquetReader", lambda *args: None)
    monkeypatch.setattr(pq, "ParquetFile", lambda *args: Parquet())
    frame, metadata = asyncio.run(service.download_dataset_dataframe("example/demo", "default", "train", 100))
    assert frame.empty
    result = analyze_dataframe(frame, metadata)
    assert result["overview"]["rows_in_selected_split"] == 1234
    assert result["overview"]["rows_analyzed"] == 0
