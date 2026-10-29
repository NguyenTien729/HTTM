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
