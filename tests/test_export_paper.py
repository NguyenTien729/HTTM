"""Test phần dựng bài báo (không cần DB): đánh số citation, xoá citation bịa, dựng PDF."""
import json
from pathlib import Path

from app.routers.export import (
    _CitationNumbering, _build_paper_pdf, _prepare_paper, _strip_citations, _esc,
)


def sample_data() -> dict:
    refs = [
        {"id": 12, "title": "GenAI and writing productivity", "authors": ["A. Noy", "W. Zhang"], "year": 2023,
         "doi": "10.1/abc", "venue": "Science"},
        {"id": 40, "title": "Students & ChatGPT: <a study>", "authors": ["B. Lee"], "year": 2024, "doi": None, "venue": None},
    ]
    snap = {
        "dataset": {"dataset_id": "owner/students-ai", "config": "default", "split": "train",
                    "source_url": "https://huggingface.co/datasets/owner/students-ai",
                    "rows_in_selected_split": 120000, "rows_analyzed": 50000, "sampled": True},
        "overview": {"rows_analyzed": 50000},
        "column_summaries": {
            "score": {"kind": "numeric", "n": 50000, "mean": 71.4321, "std": 9.8, "median": 72.0, "min": 20.0, "max": 100.0},
            "hours": {"kind": "numeric", "n": 49000, "mean": 3.2, "std": 1.4, "median": 3.0, "min": 0.0, "max": 12.0},
            "group": {"kind": "categorical", "top_values": [
                {"value": "AI", "count": 30000, "percent_of_non_missing": 60.0},
                {"value": "no-AI", "count": 20000, "percent_of_non_missing": 40.0}]},
        },
        "top_correlations_pearson": [{"var_1": "hours", "var_2": "score", "pearson_r": 0.4123}],
        "group_test": {"status": "ok", "group_column": "group", "target_column": "score",
                       "group_summary": [{"group": "AI", "n": 30000, "mean": 73.1, "std": 9.1},
                                         {"group": "no-AI", "n": 20000, "mean": 68.9, "std": 10.2}],
                       "tests": [{"name": "Welch independent t-test", "statistic": 48.2, "p_value": 0.0},
                                 {"name": "Mann–Whitney U", "statistic": 3.1e8, "p_value": 0.0004}]},
        "ols_regression": {"status": "ok", "target": "score", "n": 48000, "r_squared": 0.17,
                           "adjusted_r_squared": 0.169, "model_p_value": 0.0,
                           "coefficients": [{"term": "intercept", "coefficient": 60.1, "std_error": 0.3, "t_statistic": 200.3,
                                             "p_value": 0.0, "ci_95_lower": 59.5, "ci_95_upper": 60.7},
                                            {"term": "hours", "coefficient": 3.4, "std_error": 0.1, "t_statistic": 34.0,
                                             "p_value": 0.2, "ci_95_lower": 3.2, "ci_95_upper": 3.6}]},
        "limitations_from_analysis": ["Dataset do người dùng chọn.", "P-value chưa hiệu chỉnh."],
        "methods_draft": "Dữ liệu lấy từ dataset `owner/students-ai` (split `train`). Phân tích gồm thống kê mô tả & tương quan.",
        "results_draft": "Bộ dữ liệu gồm 50,000 dòng.\n\nKhông nên diễn giải p-value như bằng chứng nhân quả; đây là bản nháp.",
    }
    return {
        "project_topic": "AI và năng suất sinh viên", "keywords": ["generative AI", "productivity"],
        "rq": {"rq_text": "AI ảnh hưởng thế nào đến điểm số sinh viên?", "is_quantitative": True,
               "variables": [{"name": "AI usage", "role": "independent", "description": "Có dùng AI"}],
               "hypotheses": [{"label": "H1", "statement": "Dùng AI làm tăng điểm số.",
                               "independent_variable": "AI usage", "dependent_variable": "score"}]},
        "introduction": {"background": "Bối cảnh [40] và bịa [999].", "problem_statement": "Vấn đề [12].",
                         "existing_knowledge": "Đã biết [12][40].", "literature_limitation": "Hạn chế.",
                         "research_gap_statement": "Gap.", "research_question_statement": "RQ.",
                         "contribution_statement": "Đóng góp.", "citations_used": [12, 40]},
        "related_work": {"theme_paragraphs": [{"theme_name": "Năng suất", "paragraph": "Tăng năng suất [12]."}],
                         "contradictions_paragraph": "Không mâu thuẫn.", "gap_paragraph": "Gap cụ thể.",
                         "citations_used": [12]},
        "discussion": {"key_findings_paragraph": "Nhóm AI cao hơn 4.2 điểm (p < 0.001) & r = 0.41. Câu hai.",
                       "literature_comparison_paragraph": "Nhất quán với [40].", "implications_paragraph": "Ý nghĩa.",
                       "limitations_paragraph": "Hạn chế <quan trọng>.", "future_work_paragraph": "Tiếp theo.",
                       "conclusion_paragraph": "Dùng AI gắn với điểm cao hơn [12]. Cần thận trọng.",
                       "hypothesis_assessments": [{"label": "H1", "verdict": "partially_supported",
                                                   "explanation": "Chênh lệch nhóm có ý nghĩa thống kê [12]."}],
                       "citations_used": [12, 40], "numbers_unverified": ["0.87"], "analysis_snapshot": snap},
        "references": refs,
    }


def test_citations_renumbered_by_first_appearance_and_fake_removed():
    num = _CitationNumbering({12, 40})
    assert num.apply("Bối cảnh [40] và bịa [999].") == "Bối cảnh [1] và bịa."
    assert num.apply("Sau đó [12][40].") == "Sau đó [2][1]."
    assert num.removed == [999]


def test_strip_and_escape():
    assert _strip_citations("Câu này [12] .") == "Câu này."
    assert _esc("a < b & c") == "a &lt; b &amp; c"


def test_prepare_paper_notes_and_references():
    paper = _prepare_paper(sample_data())
    assert [n for n, _ in paper["references"]] == [1, 2]
    assert paper["references"][0][1]["id"] == 40            # [40] xuất hiện trước → số 1
    assert any("999" in n for n in paper["notes"])           # ghi nhận citation bị xoá
    assert any("0.87" in n for n in paper["notes"])          # ghi nhận số chưa verify
    assert all(not p.startswith("Không nên diễn giải") for p in paper["results_paras"])
    assert "[999]" not in " ".join(paper["intro"])


def test_build_paper_pdf_creates_file_with_special_chars(tmp_path: Path):
    out = tmp_path / "paper.pdf"
    _build_paper_pdf(sample_data(), out)
    assert out.exists() and out.read_bytes()[:4] == b"%PDF"