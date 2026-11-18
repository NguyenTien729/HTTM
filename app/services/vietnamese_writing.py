"""Vietnamese terminology for manuscript prose; preserve source identifiers."""
import re

VIETNAMESE_WRITING_RULE = (
    "Viết văn bản bài báo hoàn toàn bằng tiếng Việt học thuật tự nhiên. "
    "Không chèn các từ giao diện/quy trình tiếng Anh: literature = các nghiên cứu trước; "
    "literature review/related work = tổng quan nghiên cứu; research gap = khoảng trống nghiên cứu; "
    "research question = câu hỏi nghiên cứu; dataset = bộ dữ liệu; evidence = bằng chứng; "
    "findings = phát hiện; limitations = hạn chế; future work = hướng nghiên cứu tiếp theo. "
    "Giữ nguyên tên riêng, nhan đề tài liệu gốc, URL, DOI, mã bộ dữ liệu, tên biến, "
    "ký hiệu thống kê và khóa JSON trong schema.\n"
)

_TERMS = {
    "literature review": "tổng quan tài liệu", "related work": "tổng quan nghiên cứu",
    "research gap": "khoảng trống nghiên cứu", "research question": "câu hỏi nghiên cứu",
    "future work": "hướng nghiên cứu tiếp theo", "key findings": "các phát hiện chính",
    "literature": "các nghiên cứu trước", "datasets": "các bộ dữ liệu", "dataset": "bộ dữ liệu",
    "evidence": "bằng chứng", "findings": "các phát hiện", "limitations": "hạn chế",
    "discussion": "thảo luận", "conclusion": "kết luận", "introduction": "mở đầu",
    "abstract": "tóm tắt", "keywords": "từ khóa", "methods": "phương pháp",
    "results": "kết quả", "predictors": "các biến giải thích", "predictor": "biến giải thích",
    "p-value": "giá trị p", "config": "cấu hình", "split": "tập con",
}
_PATTERN = re.compile(r"(?<![\w/-])(?:" + "|".join(re.escape(k) for k in sorted(_TERMS, key=len, reverse=True)) + r")(?![\w/-])", re.I)
_PROTECTED = re.compile(r"(`[^`]*`|https?://\S+|\b[\w.-]+/[\w./-]+|\b10\.\d{4,9}/\S+)")


def vietnamese_prose(text):
    """Translate a controlled glossary, never reference titles or arbitrary English."""
    def replace(match):
        value = _TERMS[match.group().lower()]
        return value[0].upper() + value[1:] if match.group()[0].isupper() else value
    parts = _PROTECTED.split(text or "")
    return "".join(part if i % 2 else _PATTERN.sub(replace, part) for i, part in enumerate(parts))
