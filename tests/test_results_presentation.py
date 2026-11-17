import copy
from app.services.results_presentation import figure_blocks, results_explanations, numeric_variables
from app.services.manuscript_language import safe_heading, translate_headings
from app.routers.export import _build_paper_pdf, _clean_draft_text
from app.routers.discussion import _compact_analysis
from app.data_research.interpretation import dataset_guide
from tests.test_export_paper import sample_data


def described_data():
    data = sample_data()
    snap = data["discussion"]["analysis_snapshot"]
    snap["variable_guide"] = {"overview_vi": "Khảo sát cảm nhận học tập; không phải đo tác động nhân quả.",
        "variables": [{"name": "score", "label_vi": "Điểm tự báo cáo", "kind": "continuous"},
                      {"name": "hours", "label_vi": "Thời gian học", "kind": "continuous"}],
        "groups": [{"label_vi": "Điểm học tập", "columns": ["score"], "description_vi": "Kết quả tự báo cáo, không phải điểm được xác minh."}]}
    return data


def test_glossary_figures_and_clear_statistics_are_exported(tmp_path):
    import pymupdf
    data = described_data()
    data["related_work"]["theme_paragraphs"][0]["theme_name"] = "AI usage improves learning outcomes"
    output = tmp_path / "readable.pdf"
    _build_paper_pdf(data, output)
    with pymupdf.open(output) as doc:
        text = "\n".join(p.get_text() for p in doc)
        assert "AI usage improves" not in text
        assert "Biến và ý nghĩa" in text
        assert "Trung bình" in text and "Độ lệch chuẩn" in text
        assert "Điểm tự báo cáo" in text
        assert "Hình 1." in text
        assert "không có nghĩa là không có liên hệ" in text
        assert any(p.get_drawings() for p in doc)


def test_compact_snapshot_keeps_all_variables_and_figure_inputs():
    result = {"dataset": {}, "overview": {}, "column_summaries": {f"v{i}": {"mean": i} for i in range(28)},
              "charts": [{"type": "histogram"}], "correlation_pearson": {"a": {"b": .5}}}
    compact = _compact_analysis(result)
    assert len(compact["column_summaries"]) == 28
    assert compact["charts"] == result["charts"]
    assert compact["correlation_pearson"] == result["correlation_pearson"]


def test_coded_categories_are_not_plotted_as_continuous_scores():
    snap = described_data()["discussion"]["analysis_snapshot"]
    snap["variable_guide"]["variables"][0]["kind"] = "categorical"
    assert "score" not in dict(numeric_variables(snap))


def test_legacy_precision_is_formatted_for_readers():
    assert _clean_draft_text("score: mean=3.8634222919937207, SD=0.8287051005, median=4.0") == "score: trung bình 3,86, độ lệch chuẩn 0,83, trung vị 4,00"


async def test_headings_are_translated_in_order_and_not_by_word_substitution(monkeypatch):
    from app.services import manuscript_language
    async def generate(prompt, schema):
        return schema(titles=["Cơ chế trung gian: sự gắn kết và tự điều chỉnh học tập"])
    monkeypatch.setattr(manuscript_language, "generate_structured", generate)
    assert await translate_headings(["Mediating mechanisms: engagement, self-regulation"]) == ["Cơ chế trung gian: sự gắn kết và tự điều chỉnh học tập"]


async def test_variable_descriptions_without_source_support_are_discarded(monkeypatch):
    from app.data_research import interpretation
    async def generate(prompt, schema):
        return schema(overview_vi="Unknown", variables=[{"name": "x", "label_vi": "Invented question", "kind": "continuous",
            "source_quote": "This exact text is not in the dataset description."}])
    monkeypatch.setattr(interpretation, "generate_structured", generate)
    assert await dataset_guide(["x"], {"source_description": "A public survey. "*10}) == {}
