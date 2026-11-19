"""User-provided tabular data for the empirical branch: receive, clean, snapshot.

The empirical branch must be able to *find or receive* data. ``data.collect``
finds public datasets; this module receives a CSV/TSV/XLSX/Parquet file from the
research team and turns it into a source snapshot that the design, analysis and
verification stages treat exactly like a public source.

Cleaning is deterministic and fully logged so that it can be reported in the
Methods section:

* column names are trimmed, Unicode-normalised (NFC) and de-duplicated;
* text cells are trimmed and NFC-normalised;
* explicit missing-value markers (blank, ``NA``, ``N/A``, ``-``, ``..``,
  Excel errors …) become missing values; the count per column is recorded;
* fully empty rows and columns are removed;
* a text column becomes numeric only when *every* non-missing value parses
  under one number convention. Columns whose values could mean two different
  numbers (``1.234`` = 1.234 or 1234) are left as text and flagged, and code-like
  columns with leading zeros (``01``) are kept as text so join keys survive;
* exact duplicate rows are counted and removed only if the user asks.

Nothing is imputed, no outlier is removed and no unit is converted here: those
are analysis decisions that stay in the approved design.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import PurePath

import pandas as pd

from . import data

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_ROWS = 50_000
MAX_UPLOAD_COLUMNS = 300
MAX_USER_SOURCES = 5
UPLOAD_STATUSES = ("awaiting_research_approval", "awaiting_design_approval", "failed", "interrupted")
SUPPORTED_EXTENSIONS = (".csv", ".tsv", ".txt", ".xlsx", ".parquet")
NUMBER_FORMATS = ("auto", "dot_decimal", "comma_decimal")
USER_SOURCE_LABEL = "Dữ liệu do nhóm nghiên cứu cung cấp"

# Compared after strip() and casefold(). "none"/"không" are deliberately absent:
# they can be genuine survey answers.
MISSING_MARKERS = frozenset({"", "na", "n/a", "n.a.", "nan", "null", "-", "--", "—", "..", "...",
                             "#n/a", "#value!", "#div/0!", "#ref!", "#num!"})

_INTEGER = re.compile(r"^[+-]?\d+$")
_PLAIN = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_COMMA_THOUSANDS = re.compile(r"^[+-]?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?$")
_DOT_THOUSANDS = re.compile(r"^[+-]?[1-9]\d{0,2}(?:\.\d{3})+(?:,\d+)?$")
_DECIMAL_COMMA = re.compile(r"^[+-]?\d+,\d+$")
_SPACE_THOUSANDS = re.compile(r"^[+-]?[1-9]\d{0,2}(?:[   ]\d{3})+(?:[.,]\d+)?$")
_LEADING_ZERO_CODE = re.compile(r"^[+-]?0\d")
_UNNAMED = re.compile(r"^Unnamed: \d+(?:_level_\d+)?$")


class UploadError(ValueError):
    """A user-displayable problem with an uploaded file."""


# --------------------------------------------------------------------------- reading

def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "cp1258"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UploadError("Không đọc được bảng mã của file. Hãy lưu lại dưới dạng 'CSV UTF-8' rồi tải lên.")


def _detect_delimiter(text: str) -> str:
    """Pick the delimiter that splits the first lines into the most consistent columns."""
    lines = [line for line in text.splitlines()[:50] if line.strip()]
    if not lines:
        raise UploadError("File không có dữ liệu.")
    best, best_score = ",", (-1, -1)
    for delimiter in (",", ";", "\t", "|"):
        widths = [len(row) for row in csv.reader(lines, delimiter=delimiter)]
        common, frequency = Counter(widths).most_common(1)[0]
        if common < 2:
            continue
        score = (frequency, common)
        if score > best_score:
            best, best_score = delimiter, score
    return best


def read_table(raw: bytes, filename: str, sheet: str | None = None, header_row: int = 1) -> tuple[pd.DataFrame, dict]:
    """Parse an uploaded file into a DataFrame; return it with reading metadata."""
    name = PurePath(filename or "").name
    extension = PurePath(name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise UploadError("Chỉ hỗ trợ file " + ", ".join(SUPPORTED_EXTENSIONS) + ".")
    if not raw:
        raise UploadError("File rỗng.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise UploadError(f"File vượt giới hạn {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if not 1 <= int(header_row) <= 50:
        raise UploadError("Dòng tiêu đề phải nằm trong khoảng 1–50.")
    info = {"filename": name, "format": extension.lstrip("."), "bytes": len(raw),
            "raw_sha256": hashlib.sha256(raw).hexdigest(), "header_row": int(header_row)}
    try:
        if extension == ".xlsx":
            workbook = pd.ExcelFile(io.BytesIO(raw), engine="openpyxl")
            target = sheet or workbook.sheet_names[0]
            if target not in workbook.sheet_names:
                raise UploadError("Không có trang tính '" + target + "'. Các trang có trong file: "
                                  + ", ".join(workbook.sheet_names) + ".")
            frame = pd.read_excel(workbook, sheet_name=target, header=header_row - 1, dtype=object,
                                  nrows=MAX_UPLOAD_ROWS + 1)
            info.update(sheet=target, sheets=list(workbook.sheet_names))
        elif extension == ".parquet":
            frame = pd.read_parquet(io.BytesIO(raw))
        else:
            text, encoding = _decode(raw)
            delimiter = "\t" if extension == ".tsv" else _detect_delimiter(text)
            frame = pd.read_csv(io.StringIO(text), sep=delimiter, header=header_row - 1, dtype=str,
                                keep_default_na=False, na_filter=False, skip_blank_lines=True,
                                nrows=MAX_UPLOAD_ROWS + 1)
            info.update(encoding=encoding, delimiter={"\t": "tab"}.get(delimiter, delimiter))
    except UploadError:
        raise
    except Exception as exc:  # pandas/openpyxl/pyarrow raise many parser types
        raise UploadError("Không đọc được bảng trong file: " + type(exc).__name__
                          + ". Kiểm tra định dạng, trang tính và dòng tiêu đề.") from exc
    if len(frame) > MAX_UPLOAD_ROWS:
        raise UploadError(f"File có hơn {MAX_UPLOAD_ROWS:,} dòng. Hãy lọc bớt hoặc chia nhỏ trước khi tải lên.")
    if len(frame.columns) > MAX_UPLOAD_COLUMNS:
        raise UploadError(f"File có {len(frame.columns)} cột, vượt giới hạn {MAX_UPLOAD_COLUMNS} cột.")
    return frame, info


# -------------------------------------------------------------------------- cleaning

def _parse_dot_decimal(value: str) -> float | None:
    if _PLAIN.fullmatch(value):
        return float(value)
    if _COMMA_THOUSANDS.fullmatch(value):
        return float(value.replace(",", ""))
    return None


def _parse_comma_decimal(value: str) -> float | None:
    if _INTEGER.fullmatch(value):
        return float(value)
    if _DOT_THOUSANDS.fullmatch(value) or _DECIMAL_COMMA.fullmatch(value):
        return float(value.replace(".", "").replace(",", "."))
    return None


_PARSERS = {"dot_decimal": _parse_dot_decimal, "comma_decimal": _parse_comma_decimal}
_CONVENTION_LABELS = {"number": "giá trị số trong Excel/Parquet", "integer": "số nguyên",
                      "dot_decimal": "dấu chấm thập phân (1,234.5)",
                      "comma_decimal": "dấu phẩy thập phân (1.234,5)"}


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _numeric_column(values: pd.Series, number_format: str):
    """Return (converted, convention, note). converted is None when kept as text."""
    present = values.dropna()
    if present.empty:
        return None, None, None
    texts = [str(v) for v in present if not _is_number(v)]
    if not texts:
        return pd.to_numeric(values, errors="coerce"), "number", None
    if any(_LEADING_ZERO_CODE.fullmatch(v) for v in texts):
        return None, None, "code"
    texts = [re.sub(r"[   ]", "", v) if _SPACE_THOUSANDS.fullmatch(v) else v for v in texts]
    candidates = [number_format] if number_format != "auto" else ["dot_decimal", "comma_decimal"]
    parsed = {}
    for convention in candidates:
        results = [_PARSERS[convention](v) for v in texts]
        if all(r is not None for r in results):
            parsed[convention] = results
    if not parsed:
        return None, None, None
    if len(parsed) == 2:
        if parsed["dot_decimal"] != parsed["comma_decimal"]:
            examples = [v for v, a, b in zip(texts, parsed["dot_decimal"], parsed["comma_decimal"]) if a != b][:5]
            return None, None, {"examples": examples}
        convention = "integer" if all(_INTEGER.fullmatch(v) for v in texts) else "dot_decimal"
        values_used = parsed["dot_decimal"]
    else:
        convention = next(iter(parsed))
        values_used = parsed[convention]
    lookup = dict(zip(texts, values_used))

    def convert(value):
        if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NA:
            return float("nan")
        if _is_number(value):
            return float(value)
        text = str(value)
        text = re.sub(r"[   ]", "", text) if _SPACE_THOUSANDS.fullmatch(text) else text
        return lookup[text]

    converted = values.map(convert).astype(float)
    return converted, convention, None


def clean_frame(frame: pd.DataFrame, *, number_format: str = "auto", drop_duplicates: bool = False) -> tuple[pd.DataFrame, dict]:
    """Apply the documented deterministic cleaning and return (frame, log)."""
    if number_format not in NUMBER_FORMATS:
        raise UploadError("Định dạng số không hợp lệ.")
    frame = frame.copy()
    log = {"schema_version": "httm/cleaning-v1",
           "input_shape": {"rows": int(len(frame)), "columns": int(len(frame.columns))},
           "number_format": number_format}

    names, renamed = [], []
    for index, column in enumerate(frame.columns, 1):
        name = re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(column))).strip()
        if not name or _UNNAMED.fullmatch(name):
            name = f"column_{index}"
        base, suffix = name, 2
        while name in names:
            name, suffix = f"{base}_{suffix}", suffix + 1
        names.append(name)
        if name != str(column):
            renamed.append({"from": str(column), "to": name})
    frame.columns = names
    log["renamed_columns"] = renamed

    # Tidy text and record which cells were missing markers, then report the
    # markers only for rows/columns that survive empty-row/column removal.
    labels = pd.DataFrame(None, index=frame.index, columns=frame.columns, dtype=object)
    normalized = pd.DataFrame(False, index=frame.index, columns=frame.columns)
    for column in frame.columns:
        series = frame[column]
        if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
            continue
        series = series.astype(object)
        is_text = series.map(lambda v: isinstance(v, str))
        tidy = series.map(lambda v: unicodedata.normalize("NFC", v).strip() if isinstance(v, str) else v)
        normalized[column] = is_text & (tidy != series)
        marker = tidy.map(lambda v: isinstance(v, str) and v.casefold() in MISSING_MARKERS)
        labels[column] = tidy.where(marker).map(lambda v: (v or "(ô trống)") if isinstance(v, str) else None)
        frame[column] = tidy.where(~marker, None)

    empty_rows = frame.isna().all(axis=1)
    frame, labels, normalized = (x.loc[~empty_rows].reset_index(drop=True) for x in (frame, labels, normalized))
    empty_columns = [c for c in frame.columns if frame[c].isna().all()]
    frame, labels, normalized = (x.drop(columns=empty_columns) for x in (frame, labels, normalized))
    log["dropped_empty_rows"] = int(empty_rows.sum())
    log["dropped_empty_columns"] = empty_columns
    log["text_normalized_cells"] = int(normalized.to_numpy().sum())
    markers = []
    for column in labels.columns:
        found = Counter(v for v in labels[column] if isinstance(v, str))
        if found:
            markers.append({"column": column, "count": int(sum(found.values())), "markers": dict(found)})
    log["missing_markers"] = markers
    if frame.empty or not len(frame.columns):
        raise UploadError("Sau khi bỏ dòng/cột trống, file không còn dữ liệu.")

    numeric, codes, ambiguous = [], [], []
    for column in frame.columns:
        series = frame[column]
        if pd.api.types.is_bool_dtype(series):
            continue
        if pd.api.types.is_numeric_dtype(series):
            numeric.append({"column": column, "convention": "number"})
            continue
        converted, convention, note = _numeric_column(series, number_format)
        if converted is not None:
            if converted.notna().all() and (converted % 1 == 0).all() and converted.abs().max() < 2**53:
                converted = converted.astype("int64")
            frame[column] = converted
            numeric.append({"column": column, "convention": convention})
        elif note == "code":
            codes.append(column)
        elif isinstance(note, dict):
            ambiguous.append({"column": column, "examples": note["examples"],
                              "reason": "Giá trị có thể hiểu theo hai cách (ví dụ 1.234 = 1,234 hoặc 1234). "
                                        "Chọn định dạng số khi tải lên để chuyển cột này sang số."})
    log["numeric_columns"] = numeric
    log["code_like_columns"] = codes
    log["ambiguous_numeric_columns"] = ambiguous

    try:
        duplicated = frame.duplicated()
        log["duplicate_rows"] = int(duplicated.sum())
    except TypeError:
        duplicated, log["duplicate_rows"] = None, None
    log["duplicates_dropped"] = bool(drop_duplicates and duplicated is not None and duplicated.any())
    if log["duplicates_dropped"]:
        frame = frame.loc[~duplicated].reset_index(drop=True)

    log["output_shape"] = {"rows": int(len(frame)), "columns": int(len(frame.columns))}
    log["missing_after_cleaning"] = {c: int(n) for c, n in frame.isna().sum().items() if n}
    log["not_performed"] = ["Không điền giá trị thiếu (imputation).", "Không loại giá trị ngoại lai.",
                            "Không đổi đơn vị đo; đổi đơn vị chỉ thực hiện trong thiết kế đã duyệt."]
    return frame, log


def parse_and_clean(raw: bytes, filename: str, *, sheet: str | None = None, header_row: int = 1,
                    number_format: str = "auto", drop_duplicates: bool = False) -> tuple[pd.DataFrame, dict]:
    frame, info = read_table(raw, filename, sheet=sheet or None, header_row=header_row)
    frame, log = clean_frame(frame, number_format=number_format, drop_duplicates=drop_duplicates)
    log["file"] = info
    return frame, log


def cleaning_summary_vi(log: dict) -> str:
    """One Methods-ready paragraph describing exactly what was done to the file."""
    info = log.get("file", {})
    read = f"Tệp `{info.get('filename', 'không rõ')}` (SHA-256 {str(info.get('raw_sha256', ''))[:12]}…"
    if info.get("encoding"):
        read += f", mã hóa {info['encoding']}, phân cách '{info.get('delimiter')}'"
    if info.get("sheet"):
        read += f", trang tính '{info['sheet']}'"
    read += f") gồm {log['input_shape']['rows']:,} dòng và {log['input_shape']['columns']} cột ban đầu."
    steps = []
    if log.get("renamed_columns"):
        steps.append(f"chuẩn hóa {len(log['renamed_columns'])} tên cột")
    missing = sum(item["count"] for item in log.get("missing_markers", []))
    if missing:
        steps.append(f"chuyển {missing:,} ô trống hoặc ký hiệu thiếu (như N/A, '-') thành giá trị thiếu")
    if log.get("dropped_empty_rows") or log.get("dropped_empty_columns"):
        steps.append(f"bỏ {log.get('dropped_empty_rows', 0)} dòng và {len(log.get('dropped_empty_columns', []))} cột hoàn toàn trống")
    converted = [item for item in log.get("numeric_columns", []) if item["convention"] != "number"]
    if converted:
        conventions = sorted({_CONVENTION_LABELS[item["convention"]] for item in converted})
        steps.append(f"chuyển {len(converted)} cột văn bản sang dạng số theo {', '.join(conventions)}")
    if log.get("code_like_columns"):
        steps.append("giữ dạng mã cho cột có số 0 đứng đầu: " + ", ".join(log["code_like_columns"]))
    if log.get("duplicate_rows"):
        steps.append(f"phát hiện {log['duplicate_rows']} dòng trùng lặp hoàn toàn"
                     + (" và đã loại theo yêu cầu" if log.get("duplicates_dropped") else ", vẫn giữ nguyên"))
    text = read + " "
    text += ("Làm sạch tất định gồm: " + "; ".join(steps) + ". ") if steps else "Không cần bước làm sạch nào. "
    text += (f"Sau làm sạch còn {log['output_shape']['rows']:,} dòng và {log['output_shape']['columns']} cột. "
             "Không điền giá trị thiếu, không loại ngoại lai và không đổi đơn vị ở bước này.")
    if log.get("ambiguous_numeric_columns"):
        text += (" Các cột có định dạng số mơ hồ được giữ dạng văn bản: "
                 + ", ".join(item["column"] for item in log["ambiguous_numeric_columns"]) + ".")
    return text


# ----------------------------------------------------------------------- run state

def _source_keys(artifacts: dict) -> set[str]:
    keys = {s.get("source_key") for s in artifacts.get("user_sources", [])}
    keys |= {s.get("source_key") for s in (artifacts.get("data_collection") or {}).get("sources", [])}
    return {k for k in keys if k}


def next_source_key(artifacts: dict) -> str:
    used = [int(k[1:]) for k in _source_keys(artifacts) if re.fullmatch(r"s\d{2}", k)]
    number = max(used, default=0) + 1
    if number > 99:
        raise UploadError("Đã hết mã nguồn khả dụng cho tiến trình này.")
    return f"s{number:02d}"


def _check_can_change(run: dict) -> None:
    from fastapi import HTTPException
    from .state import is_review
    if is_review(run):
        raise HTTPException(409, "Bài tổng quan không dùng dữ liệu thực nghiệm.")
    if run["status"] not in UPLOAD_STATUSES:
        raise HTTPException(409, "Chỉ tải hoặc gỡ dữ liệu khi tiến trình đang chờ duyệt câu hỏi, chờ duyệt thiết kế, "
                                 "bị lỗi hoặc bị gián đoạn.")


def _redo_design(run: dict) -> None:
    """New or removed data invalidates the design and everything after it."""
    keep = {"settings", "topic", "literature_search", "literature_read", "literature_synthesis", "approvals",
            "research_plan", "data_feasibility", "data_collection", "user_sources", "data_preferences",
            "data_search_guidance", "feedback"}
    run["artifacts"] = {k: v for k, v in run["artifacts"].items() if k in keep}
    run.update(stage="design", status="queued", error=None)


def attach_user_source(run: dict, frame: pd.DataFrame, log: dict, *, description: str,
                       license: str | None = None, only_user_data: bool | None = None) -> dict:
    """Snapshot an uploaded, cleaned table and attach it to the run (pure apart from the file write)."""
    from fastapi import HTTPException
    from .state import event
    run = deepcopy(run)
    _check_can_change(run)
    artifacts = run["artifacts"]
    users = artifacts.setdefault("user_sources", [])
    if len(users) >= MAX_USER_SOURCES:
        raise HTTPException(409, f"Mỗi tiến trình nhận tối đa {MAX_USER_SOURCES} file dữ liệu.")
    max_rows = int(artifacts.get("settings", {}).get("max_rows", MAX_UPLOAD_ROWS))
    if len(frame) > max_rows:
        raise HTTPException(422, f"File có {len(frame):,} dòng sau làm sạch, vượt giới hạn {max_rows:,} dòng của tiến trình. "
                                 "Hãy lọc theo địa bàn/thời gian trước, hoặc tạo tiến trình với giới hạn dòng lớn hơn.")
    if any(u.get("metadata", {}).get("raw_sha256") == log["file"]["raw_sha256"] for u in users):
        raise HTTPException(409, "File này đã được tải lên trong tiến trình.")
    key = next_source_key(artifacts)
    profile = data.save_snapshot(frame, run, key)
    filename = log["file"]["filename"][:150]
    profile.update(
        candidate={"dataset_id": "upload:" + filename, "title": filename, "dataset_url": "",
                   "source": USER_SOURCE_LABEL, "license": (license or "").strip() or None,
                   "description": description.strip()[:2500], "requirements": [],
                   "scope_match": "user_declared", "scope_verification_required": True,
                   "relevance_note": "Dữ liệu do người dùng tải lên; phạm vi, cách thu thập, đơn vị và giấy phép "
                                     "do người dùng khai báo và cần đối chiếu với giá trị thực tế.",
                   "user_provided": True},
        metadata={"origin": "user_upload", "sampled": False, "filename": filename,
                  "raw_sha256": log["file"]["raw_sha256"], "cleaning": log,
                  "cleaning_summary": cleaning_summary_vi(log),
                  "uploaded_at": datetime.now(timezone.utc).isoformat()},
        config="upload", split="upload")
    users.append(profile)
    if only_user_data is not None:
        artifacts["data_preferences"] = {"public_search": not only_user_data}
    message = f"Người dùng tải lên dữ liệu {filename} ({profile['rows']:,} dòng, {profile['total_columns']} cột) → {key}."
    if artifacts.get("data_collection") is not None:
        collection = artifacts["data_collection"]
        collection["sources"] = [s for s in collection.get("sources", []) if s.get("source_key") != key] + [profile]
        _redo_design(run)
        message += " Đề xuất lại thiết kế với dữ liệu mới."
    event(run, message)
    return run


def remove_user_source(run: dict, source_key: str) -> dict:
    from fastapi import HTTPException
    from .state import event
    run = deepcopy(run)
    _check_can_change(run)
    artifacts = run["artifacts"]
    users = artifacts.get("user_sources", [])
    if not any(s.get("source_key") == source_key for s in users):
        raise HTTPException(404, "Không có nguồn do người dùng tải lên với mã này.")
    artifacts["user_sources"] = [s for s in users if s.get("source_key") != source_key]
    message = "Người dùng gỡ nguồn dữ liệu " + source_key + "."
    if artifacts.get("data_collection") is not None:
        collection = artifacts["data_collection"]
        collection["sources"] = [s for s in collection.get("sources", []) if s.get("source_key") != source_key]
        _redo_design(run)
        message += " Đề xuất lại thiết kế."
    event(run, message)
    return run


def is_user_source(source: dict) -> bool:
    return bool(source.get("candidate", {}).get("user_provided"))
