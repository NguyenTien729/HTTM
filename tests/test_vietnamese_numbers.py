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
