"""Number verification must read Vietnamese decimals (0,3819; 1.671,77) as the analysis values."""
import os

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

from app.routers.discussion import _find_unverified_numbers, _normalize_written_numbers

SOURCE = '{"r": 0.38194, "e": 0.08713, "r2": 0.20202, "adj": 0.19751, "cond": 1671.7712, "n": 534}'


def test_vietnamese_decimals_and_thousands_match_the_analysis():
    text = "Tương quan r=0,3819 và 0,0871; R²=0,202, adjusted R²=0,1975; số điều kiện 1.671,77; n=534 [22]."
    assert _find_unverified_numbers([text], SOURCE) == []


def test_numbers_absent_from_the_analysis_are_still_flagged():
    assert _find_unverified_numbers(["Hệ số 0,9483 và mức tăng 12,5%."], SOURCE) == ["0.9483", "12.5"]


def test_lists_and_sentence_punctuation_are_not_merged_into_decimals():
    assert _normalize_written_numbers("các năm 2010, 2015 và 3, 4 nhóm; 1,5.") == "các năm 2010, 2015 và 3, 4 nhóm; 1.5."


def test_confidence_levels_are_method_constants_not_results():
    text = "hệ số 0,926; khoảng tin cậy 95% HC3 0,751–1,100; 95% CI; confidence interval 99%."
    source = '{"b": 0.92596, "lo": 0.75149, "hi": 1.10043}'
    assert _find_unverified_numbers([text], source) == []
    assert _find_unverified_numbers(["Có 95% người lao động thuộc nhóm này."], source) == ["95"]


def test_significance_thresholds_are_not_results():
    from app.routers.discussion import _find_unverified_numbers
    source = '{"p_value": 1.2e-20, "coefficient": 0.9263}'
    text = "Hệ số 0,926 có ý nghĩa thống kê (p < 0,001; mức ý nghĩa 5%, α = 0.05)."
    assert _find_unverified_numbers([text], source) == []
    # A threshold-looking number that is reported as a result is still checked.
    assert _find_unverified_numbers(["Hệ số bằng 0,001."], source) == ["0.001"]


def test_literature_figures_are_checked_against_cited_evidence():
    from app.routers.discussion import _english_thousands, _find_unverified_numbers
    evidence = _english_thousands('{"quote": "among 34 countries with GDP above $10,000 the correlation was 0.51"}')
    assert _find_unverified_numbers(["Với các nước có GDP trên 10.000 USD, tương quan là 0,51 [39]."], evidence) == []
    assert _find_unverified_numbers(["Tương quan là 0,62 [39]."], evidence) == ["0.62"]
