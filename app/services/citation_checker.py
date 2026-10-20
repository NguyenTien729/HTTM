"""
Reviewer #1 — Citation checker.
Hàm thuần (không DB): kiểm tra mỗi trích dẫn [paper_id] trong draft có trỏ tới paper thật
của project hay không, và liệt kê câu dài không có trích dẫn.
"""

import re

# Khớp [12] và [12, 15] (định dạng Scientific Writer đang dùng: [paper_id]).
_CITE_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_MIN_WORDS_FOR_CLAIM = 8


def extract_cited_ids(text: str) -> list[int]:
    ids: list[int] = []
    for m in _CITE_RE.finditer(text):
        for part in m.group(1).split(","):
            n = int(part.strip())
            if n not in ids:
                ids.append(n)
    return ids


def check_text(text: str, valid_ids: set[int]) -> dict:
    cited = extract_cited_ids(text)
    invalid = [i for i in cited if i not in valid_ids]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    uncited = [
        s for s in sentences
        if len(s.split()) >= _MIN_WORDS_FOR_CLAIM and not _CITE_RE.search(s)
    ]
    return {
        "ok": not invalid,
        "cited_ids": cited,
        "invalid_ids": invalid,            # trỏ tới paper KHÔNG có trong project -> nghi hallucination
        "uncited_sentences": uncited,      # câu dài không có trích dẫn -> cần bổ sung/xác minh
    }
