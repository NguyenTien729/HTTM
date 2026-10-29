"""Dataset discovery and analysis services.

Public datasets are discovered through Hugging Face Hub and its Dataset Viewer.
The client never supplies a download URL: only URLs returned by Hugging Face are
accepted, which keeps this service from becoming an arbitrary URL fetcher.
"""

from __future__ import annotations

import io
import math
import random
import re
import unicodedata
from collections import Counter
from typing import Any
from urllib.parse import urlparse

import httpx
import numpy as np
import pandas as pd
from scipy import stats

HUB_API = "https://huggingface.co/api/datasets"
VIEWER_API = "https://datasets-server.huggingface.co"
USER_AGENT = "HTTM-DataResearch/1.0 (academic research prototype)"
REQUEST_TIMEOUT = 25.0
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 32 * 1024 * 1024
MAX_PARQUET_FILES = 40
MAX_ANALYSIS_ROWS = 100_000
MAX_PREVIEW_ROWS = 20

_DATASET_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class DataResearchError(Exception):
    """An expected, user-displayable error from the Data Research workflow."""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def validate_dataset_id(dataset_id: str) -> str:
    value = (dataset_id or "").strip()
    if not _DATASET_ID_RE.fullmatch(value):
        raise DataResearchError(
            "dataset_id không hợp lệ. Hãy dùng ID do API discover trả về, ví dụ owner/dataset-name.",
            422,
        )
    return value


def _normalized_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _topic_terms(topic: str) -> list[str]:
    stop_words = {
        "the", "and", "for", "with", "from", "into", "about", "data", "dataset",
        "study", "research", "of", "to", "in", "on", "a", "an", "va", "cua",
        "cho", "trong", "nhung", "mot", "cac", "ve", "tai", "anh", "huong",
    }
    return [t for t in _normalized_text(topic).split() if len(t) > 1 and t not in stop_words]


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _json_safe(value: Any) -> Any:
    """Convert NumPy/Pandas values into JSON-compatible values."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return _safe_float(value)
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (str, int)):
        return value
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (ValueError, TypeError):
            pass
    return str(value)


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any] | list[Any]:
    try:
        response = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise DataResearchError("Nguồn dữ liệu phản hồi quá chậm. Hãy thử lại sau.", 504) from exc
    except httpx.HTTPError as exc:
        raise DataResearchError(f"Không kết nối được tới nguồn dữ liệu: {exc.__class__.__name__}.", 502) from exc

    if response.status_code == 404:
        raise DataResearchError("Không tìm thấy dataset hoặc dataset chưa được Dataset Viewer hỗ trợ.", 404)
    if response.status_code == 429:
        raise DataResearchError("Nguồn dữ liệu đang giới hạn số yêu cầu. Hãy đợi một lúc rồi thử lại.", 429)
    if response.status_code >= 400:
        message = "Nguồn dữ liệu từ chối yêu cầu. Dataset có thể riêng tư, không tồn tại hoặc chưa được xử lý."
        raise DataResearchError(message, 502)
    try:
        return response.json()
    except ValueError as exc:
        raise DataResearchError("Nguồn dữ liệu trả về JSON không hợp lệ.", 502) from exc


def _to_candidate(item: dict[str, Any], terms: list[str]) -> dict[str, Any]:
    card = item.get("cardData") or {}
    dataset_id = str(item.get("id") or "")
    description = card.get("short_description") or card.get("description") or ""
    description = re.sub(r"\s+", " ", str(description)).strip()
    search_text = _normalized_text(" ".join([
        dataset_id,
        description,
        " ".join(item.get("tags") or []),
        " ".join(card.get("task_categories") or []),
        " ".join(card.get("task_ids") or []),
    ]))
    matched = [term for term in terms if term in search_text]
    score = round(len(matched) / max(1, len(terms)), 3)
    return {
        "dataset_id": dataset_id,
        "title": dataset_id.split("/", 1)[-1].replace("-", " ").replace("_", " "),
        "description": description[:1200],
        "downloads": int(item.get("downloads") or 0),
        "likes": int(item.get("likes") or 0),
        "last_modified": item.get("lastModified"),
        "license": card.get("license"),
        "task_categories": (card.get("task_categories") or [])[:10],
        "tags": (item.get("tags") or [])[:15],
        "topic_match_score": score,
        "matched_terms": matched,
        "dataset_url": f"https://huggingface.co/datasets/{dataset_id}",
        "source": "Hugging Face Hub",
    }


async def discover_datasets(topic: str, limit: int = 10) -> dict[str, Any]:
    topic = (topic or "").strip()
    if len(topic) < 3:
        raise DataResearchError("Hãy nhập chủ đề nghiên cứu cụ thể (ít nhất 3 ký tự).", 422)
    limit = max(1, min(int(limit), 20))
    terms = _topic_terms(topic)
    variants = [topic]
    if len(terms) >= 2:
        shorter = " ".join(terms[:5])
        if _normalized_text(shorter) != _normalized_text(topic):
            variants.append(shorter)

    headers = {"User-Agent": USER_AGENT}
    found: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers) as client:
        for query in variants:
            result = await _get_json(client, HUB_API, {"search": query, "limit": min(50, limit * 3), "sort": "downloads", "direction": "-1"})
            if not isinstance(result, list):
                continue
            for item in result:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                dataset_id = str(item["id"])
                if not _DATASET_ID_RE.fullmatch(dataset_id):
                    continue
                candidate = _to_candidate(item, terms)
                if dataset_id not in found or candidate["topic_match_score"] > found[dataset_id]["topic_match_score"]:
                    found[dataset_id] = candidate

    candidates = sorted(
        found.values(),
        key=lambda item: (item["topic_match_score"], item["downloads"], item["likes"]),
        reverse=True,
    )[:limit]
    return {
        "topic": topic,
        "source": "Hugging Face Hub API",
        "count": len(candidates),
        "datasets": candidates,
        "review_note": "Đây là danh sách gợi ý tự động, không phải xác nhận dataset phù hợp. Hãy kiểm tra mô tả, giấy phép, biến và phạm vi mẫu trước khi chọn.",
    }


async def _get_splits(client: httpx.AsyncClient, dataset_id: str) -> list[dict[str, Any]]:
    result = await _get_json(client, f"{VIEWER_API}/splits", {"dataset": dataset_id})
    splits = result.get("splits", []) if isinstance(result, dict) else []
    normalized: list[dict[str, Any]] = []
    for item in splits:
        if not isinstance(item, dict) or not item.get("config") or not item.get("split"):
            continue
        normalized.append({
            "config": str(item["config"]),
            "split": str(item["split"]),
            "num_rows": int(item.get("num_rows") or 0),
            "num_bytes": item.get("num_bytes"),
        })
    if not normalized:
        raise DataResearchError(
            "Dataset này không có split khả dụng trên Hugging Face Dataset Viewer. Hãy chọn dataset công khai khác.",
            422,
        )
    return normalized


async def inspect_dataset(dataset_id: str, config: str | None = None, split: str | None = None) -> dict[str, Any]:
    dataset_id = validate_dataset_id(dataset_id)
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers) as client:
        available_splits = await _get_splits(client, dataset_id)
        selected = next((x for x in available_splits if (not config or x["config"] == config) and (not split or x["split"] == split)), None)
        if selected is None:
            raise DataResearchError("config/split bạn chọn không nằm trong danh sách split của dataset.", 422)
        result = await _get_json(client, f"{VIEWER_API}/rows", {
            "dataset": dataset_id,
            "config": selected["config"],
            "split": selected["split"],
            "offset": 0,
            "length": MAX_PREVIEW_ROWS,
        })

    rows = result.get("rows", []) if isinstance(result, dict) else []
    features = result.get("features", []) if isinstance(result, dict) else []
    preview_rows = []
    for wrapped in rows:
        row = wrapped.get("row", {}) if isinstance(wrapped, dict) else {}
        preview_rows.append({str(k): _json_safe(v) for k, v in row.items()})
    total = result.get("num_rows_total", selected["num_rows"]) if isinstance(result, dict) else selected["num_rows"]
    return {
        "dataset_id": dataset_id,
        "dataset_url": f"https://huggingface.co/datasets/{dataset_id}",
        "source": "Hugging Face Dataset Viewer",
        "available_splits": available_splits,
        "preview_selection": {"config": selected["config"], "split": selected["split"]},
        "num_rows_total": int(total or 0),
        "columns": [{"name": f.get("name"), "type": f.get("type")} for f in features],
        "preview_rows": preview_rows,
        "requires_user_review": True,
        "review_checklist": [
            "Dataset có đúng chủ đề và đối tượng nghiên cứu không?",
            "Các cột có đo đúng biến cần phân tích không?",
            "Split train/test hay nhóm dữ liệu nào phù hợp với câu hỏi nghiên cứu?",
            "Giấy phép có cho phép mục đích sử dụng này không?",
            "Dữ liệu có thông tin cá nhân hoặc giới hạn đạo đức cần xử lý không?",
        ],
    }


async def _download_checked(client: httpx.AsyncClient, url: str, remaining_bytes: int) -> bytes:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = (
        host == "huggingface.co"
        or host.endswith(".huggingface.co")
        or host == "hf.co"
        or host.endswith(".hf.co")
        or host == "xethub.hf.co"
        or host.endswith(".xethub.hf.co")
    )
    if parsed.scheme != "https" or not allowed:
        raise DataResearchError("Nguồn parquet trả về URL không được phép; đã chặn tải xuống để bảo vệ máy chủ.", 502)

    try:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise DataResearchError("Không tải được file Parquet từ nguồn dataset.", 502)
            declared = response.headers.get("content-length")
            if declared and int(declared) > min(MAX_SINGLE_FILE_BYTES, remaining_bytes):
                raise DataResearchError(
                    "File dataset vượt giới hạn tải an toàn (32 MB/file hoặc 100 MB tổng). Hãy chọn dataset nhỏ hơn.", 413
                )
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > min(MAX_SINGLE_FILE_BYTES, remaining_bytes):
                    raise DataResearchError(
                        "File dataset vượt giới hạn tải an toàn (32 MB/file hoặc 100 MB tổng). Hãy chọn dataset nhỏ hơn.", 413
                    )
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.TimeoutException as exc:
        raise DataResearchError("Tải dataset bị timeout; hãy thử lại hoặc chọn dataset nhỏ hơn.", 504) from exc
    except httpx.HTTPError as exc:
        raise DataResearchError("Không tải được file dataset. Hãy thử lại hoặc chọn split khác.", 502) from exc


async def download_dataset_dataframe(
    dataset_id: str, config: str, split: str, max_rows: int = 50_000
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Download public Parquet shards and build a reproducible reservoir sample.

    The full selected split is streamed in batches, while only `max_rows` rows are
    retained in memory. This avoids silently analyzing the first N rows (which can
    be ordered by label, time, or class).
    """
    dataset_id = validate_dataset_id(dataset_id)
    if max_rows < 100 or max_rows > MAX_ANALYSIS_ROWS:
        raise DataResearchError(f"max_rows phải nằm trong khoảng 100–{MAX_ANALYSIS_ROWS}.", 422)

    headers = {"User-Agent": USER_AGENT}
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise DataResearchError("Thiếu pyarrow. Hãy chạy pip install -r requirements.txt rồi thử lại.", 500) from exc

    rng = random.Random(42)
    reservoir: list[dict[str, Any]] = []
    seen_rows = 0
    total_downloaded = 0
    parquet_files_used = 0
    total_rows_reported = None

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers) as client:
        splits = await _get_splits(client, dataset_id)
        if not any(item["config"] == config and item["split"] == split for item in splits):
            raise DataResearchError("config/split không hợp lệ với dataset này. Hãy gọi inspect để xem lựa chọn khả dụng.", 422)
        parquet_result = await _get_json(client, f"{VIEWER_API}/parquet", {"dataset": dataset_id})
        parquet_files = parquet_result.get("parquet_files", []) if isinstance(parquet_result, dict) else []
        selected_files = [
            item for item in parquet_files
            if isinstance(item, dict) and item.get("config") == config and item.get("split") == split and item.get("url")
        ]
        if not selected_files:
            raise DataResearchError(
                "Dataset Viewer không cung cấp file Parquet cho split này. Hãy chọn dataset/split khác có Parquet công khai.",
                422,
            )
        if len(selected_files) > MAX_PARQUET_FILES:
            raise DataResearchError(
                f"Split có quá nhiều file ({len(selected_files)}). Module hiện giới hạn {MAX_PARQUET_FILES} file và 100 MB tải xuống.",
                413,
            )

        for file_info in selected_files:
            remaining = MAX_DOWNLOAD_BYTES - total_downloaded
            if remaining <= 0:
                raise DataResearchError("Dataset vượt giới hạn tổng tải 100 MB. Hãy chọn dataset nhỏ hơn.", 413)
            payload = await _download_checked(client, str(file_info["url"]), remaining)
            total_downloaded += len(payload)
            parquet_files_used += 1
            try:
                parquet_file = pq.ParquetFile(io.BytesIO(payload))
                file_rows = int(parquet_file.metadata.num_rows)
                total_rows_reported = (total_rows_reported or 0) + file_rows
                for batch in parquet_file.iter_batches(batch_size=5000):
                    for record in batch.to_pylist():
                        seen_rows += 1
                        if len(reservoir) < max_rows:
                            reservoir.append(record)
                        else:
                            index = rng.randrange(seen_rows)
                            if index < max_rows:
                                reservoir[index] = record
            except Exception as exc:
                if isinstance(exc, DataResearchError):
                    raise
                raise DataResearchError(
                    f"Không đọc được file Parquet ({file_info.get('filename', 'unknown')}). Dataset có thể có định dạng cột chưa hỗ trợ.",
                    422,
                ) from exc

    if not reservoir:
        raise DataResearchError("Dataset không có dòng dữ liệu để phân tích.", 422)
    frame = pd.DataFrame.from_records(reservoir)
    # A reproducible random sample is used when the split is larger than max_rows.
    sampled = seen_rows > max_rows
    return frame, {
        "dataset_id": dataset_id,
        "config": config,
        "split": split,
        "source_url": f"https://huggingface.co/datasets/{dataset_id}",
        "rows_in_selected_split": int(total_rows_reported if total_rows_reported is not None else seen_rows),
        "rows_analyzed": int(len(frame)),
        "columns_count": int(len(frame.columns)),
        "parquet_files_used": parquet_files_used,
        "downloaded_bytes": total_downloaded,
        "sampled": sampled,
        "sampling_method": "Reservoir sampling, random seed=42" if sampled else "Toàn bộ các dòng trong split đã chọn",
        "max_rows": max_rows,
    }


def _numeric_columns(frame: pd.DataFrame) -> list[str]:
    return [str(c) for c in frame.columns if pd.api.types.is_numeric_dtype(frame[c]) and not pd.api.types.is_bool_dtype(frame[c])]


def _categorical_columns(frame: pd.DataFrame) -> list[str]:
    found = []
    for col in frame.columns:
        series = frame[col]
        if pd.api.types.is_bool_dtype(series) or pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series) or isinstance(series.dtype, pd.CategoricalDtype):
            found.append(str(col))
    return found


def _numeric_summary(series: pd.Series) -> dict[str, Any]:
    values = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if values.empty:
        return {"n": 0, "missing": int(series.isna().sum()), "unique": int(series.nunique(dropna=True))}
    return {
        "n": int(values.shape[0]),
        "missing": int(series.isna().sum() + (pd.to_numeric(series, errors="coerce").isna() & series.notna()).sum()),
        "unique": int(values.nunique()),
        "mean": _safe_float(values.mean()),
        "median": _safe_float(values.median()),
        "std": _safe_float(values.std(ddof=1)) if values.shape[0] > 1 else None,
        "variance": _safe_float(values.var(ddof=1)) if values.shape[0] > 1 else None,
        "min": _safe_float(values.min()),
        "q1": _safe_float(values.quantile(0.25)),
        "q3": _safe_float(values.quantile(0.75)),
        "max": _safe_float(values.max()),
        "skewness": _safe_float(values.skew()) if values.shape[0] > 2 else None,
    }


def _categorical_summary(series: pd.Series, top_n: int = 10) -> dict[str, Any]:
    values = series.dropna().astype(str)
    counts = values.value_counts().head(top_n)
    denominator = int(values.shape[0])
    return {
        "n_non_missing": denominator,
        "missing": int(series.isna().sum()),
        "unique": int(values.nunique()),
        "top_values": [
            {"value": str(value)[:250], "count": int(count), "percent_of_non_missing": round(100 * int(count) / denominator, 3) if denominator else None}
            for value, count in counts.items()
        ],
    }


def _regression_summary(frame: pd.DataFrame, target: str, predictors: list[str]) -> dict[str, Any]:
    numeric_predictors = [p for p in predictors if p in frame.columns and pd.api.types.is_numeric_dtype(frame[p])]
    if len(numeric_predictors) != len(predictors):
        bad = [p for p in predictors if p not in frame.columns or p not in numeric_predictors]
        return {"status": "not_run", "reason": f"Các predictor sau không hợp lệ hoặc không phải số: {bad}"}
    columns = [target] + numeric_predictors
    work = frame[columns].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    n = len(work)
    k = len(numeric_predictors)
    if n <= k + 2:
        return {"status": "not_run", "reason": f"Cần tối thiểu {k + 3} dòng đầy đủ để ước lượng hồi quy với {k} predictor; hiện có {n}."}
    y = work[target].to_numpy(dtype=float)
    x = work[numeric_predictors].to_numpy(dtype=float)
    design = np.column_stack([np.ones(n), x])
    if np.linalg.matrix_rank(design) < design.shape[1]:
        return {"status": "not_run", "reason": "Ma trận predictor bị phụ thuộc tuyến tính; hãy bỏ bớt biến trùng/đa cộng tuyến hoàn hảo."}
    beta, _, _, _ = np.linalg.lstsq(design, y, rcond=None)
    predicted = design @ beta
    residuals = y - predicted
    sse = float(np.sum(residuals ** 2))
    centered_sst = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = 1 - sse / centered_sst if centered_sst > 0 else None
    df_resid = n - design.shape[1]
    mse = sse / df_resid if df_resid > 0 else float("nan")
    covariance = mse * np.linalg.pinv(design.T @ design)
    standard_errors = np.sqrt(np.maximum(np.diag(covariance), 0))
    coefficient_rows = []
    for idx, name in enumerate(["intercept"] + numeric_predictors):
        estimate = float(beta[idx])
        se = float(standard_errors[idx])
        t_value = estimate / se if se > 0 else None
        p_value = float(2 * stats.t.sf(abs(t_value), df_resid)) if t_value is not None else None
        critical = float(stats.t.ppf(0.975, df_resid))
        coefficient_rows.append({
            "term": name,
            "coefficient": _safe_float(estimate),
            "std_error": _safe_float(se),
            "t_statistic": _safe_float(t_value),
            "p_value": _safe_float(p_value),
            "ci_95_lower": _safe_float(estimate - critical * se),
            "ci_95_upper": _safe_float(estimate + critical * se),
        })
    if centered_sst > 0 and r_squared is not None and k > 0:
        model_ss = max(0.0, centered_sst - sse)
        f_stat = (model_ss / k) / (sse / df_resid) if sse > 0 else None
        f_p = float(stats.f.sf(f_stat, k, df_resid)) if f_stat is not None else None
        adjusted_r_squared = 1 - (1 - r_squared) * (n - 1) / df_resid
    else:
        f_stat, f_p, adjusted_r_squared = None, None, None
    return {
        "status": "ok",
        "model": "ordinary_least_squares",
        "target": target,
        "predictors": numeric_predictors,
        "n": int(n),
        "r_squared": _safe_float(r_squared),
        "adjusted_r_squared": _safe_float(adjusted_r_squared),
        "f_statistic": _safe_float(f_stat),
        "model_p_value": _safe_float(f_p),
        "coefficients": coefficient_rows,
        "interpretation_note": "OLS mô tả quan hệ tuyến tính trên dữ liệu được chọn; không chứng minh quan hệ nhân quả.",
    }


def _hypothesis_test(frame: pd.DataFrame, group_column: str, target_column: str) -> dict[str, Any]:
    if group_column not in frame.columns or target_column not in frame.columns:
        return {"status": "not_run", "reason": "group_column hoặc target_column không tồn tại trong dataset."}
    work = frame[[group_column, target_column]].dropna().copy()
    if work.empty:
        return {"status": "not_run", "reason": "Không có dòng đầy đủ cho hai biến đã chọn."}

    target_is_numeric = pd.api.types.is_numeric_dtype(frame[target_column]) and not pd.api.types.is_bool_dtype(frame[target_column])
    groups = list(work[group_column].astype(str).unique())
    if target_is_numeric:
        group_samples = []
        for group_name in groups:
            values = pd.to_numeric(work.loc[work[group_column].astype(str) == group_name, target_column], errors="coerce")
            values = values.replace([np.inf, -np.inf], np.nan).dropna().to_numpy(dtype=float)
            if len(values) >= 2:
                group_samples.append((group_name, values))
        if len(group_samples) < 2:
            return {"status": "not_run", "reason": "Mỗi nhóm cần ít nhất 2 giá trị số hợp lệ."}
        result: dict[str, Any] = {
            "status": "ok",
            "test_type": "numeric_target_by_group",
            "group_column": group_column,
            "target_column": target_column,
            "group_summary": [
                {"group": name, "n": int(len(values)), "mean": _safe_float(np.mean(values)), "std": _safe_float(np.std(values, ddof=1)) if len(values) > 1 else None}
                for name, values in group_samples
            ],
        }
        samples = [values for _, values in group_samples]
        try:
            if len(samples) == 2:
                t_result = stats.ttest_ind(samples[0], samples[1], equal_var=False, nan_policy="omit")
                u_result = stats.mannwhitneyu(samples[0], samples[1], alternative="two-sided")
                result["tests"] = [
                    {"name": "Welch independent t-test", "statistic": _safe_float(t_result.statistic), "p_value": _safe_float(t_result.pvalue), "purpose": "So sánh trung bình hai nhóm, không giả định phương sai bằng nhau."},
                    {"name": "Mann–Whitney U", "statistic": _safe_float(u_result.statistic), "p_value": _safe_float(u_result.pvalue), "purpose": "Kiểm định phi tham số bổ sung cho hai nhóm độc lập."},
                ]
            else:
                f_result = stats.f_oneway(*samples)
                h_result = stats.kruskal(*samples)
                result["tests"] = [
                    {"name": "One-way ANOVA", "statistic": _safe_float(f_result.statistic), "p_value": _safe_float(f_result.pvalue), "purpose": "So sánh trung bình từ ba nhóm trở lên."},
                    {"name": "Kruskal–Wallis", "statistic": _safe_float(h_result.statistic), "p_value": _safe_float(h_result.pvalue), "purpose": "Kiểm định phi tham số cho ba nhóm trở lên."},
                ]
        except ValueError as exc:
            result["status"] = "not_run"
            result["reason"] = f"Không thể tính kiểm định cho phân phối nhóm hiện tại: {exc}"
            result["tests"] = []
        return result

    crosstab = pd.crosstab(work[group_column].astype(str), work[target_column].astype(str))
    if crosstab.shape[0] < 2 or crosstab.shape[1] < 2:
        return {"status": "not_run", "reason": "Kiểm định chi-bình phương cần ít nhất 2 nhóm và 2 hạng mục."}
    chi2, p_value, dof, expected = stats.chi2_contingency(crosstab.to_numpy())
    return {
        "status": "ok",
        "test_type": "categorical_association",
        "group_column": group_column,
        "target_column": target_column,
        "test": {
            "name": "Chi-square test of independence",
            "statistic": _safe_float(chi2),
            "p_value": _safe_float(p_value),
            "degrees_of_freedom": int(dof),
            "minimum_expected_frequency": _safe_float(np.min(expected)),
        },
        "contingency_table": {str(index): {str(col): int(value) for col, value in row.items()} for index, row in crosstab.iterrows()},
        "caution": "Kiểm tra tần số kỳ vọng; nếu nhiều ô có expected count nhỏ, cân nhắc gộp nhóm hoặc dùng kiểm định phù hợp hơn.",
    }


def analyze_dataframe(
    frame: pd.DataFrame,
    dataset_meta: dict[str, Any],
    target_column: str | None = None,
    group_column: str | None = None,
    predictor_columns: list[str] | None = None,
) -> dict[str, Any]:
    if frame.empty or len(frame.columns) == 0:
        raise DataResearchError("Dataset đã chọn không có dữ liệu dạng bảng để phân tích.", 422)
    frame = frame.copy()
    frame.columns = [str(column) for column in frame.columns]
    numeric_cols = _numeric_columns(frame)
    categorical_cols = _categorical_columns(frame)
    actual_rows = int(len(frame))
    missing_by_col = [
        {"column": str(col), "missing_count": int(frame[col].isna().sum()), "missing_percent": round(100 * float(frame[col].isna().mean()), 3)}
        for col in frame.columns
    ]
    missing_by_col.sort(key=lambda x: x["missing_count"], reverse=True)

    column_summaries: dict[str, Any] = {}
    for col in numeric_cols[:50]:
        column_summaries[col] = {"type": str(frame[col].dtype), "kind": "numeric", **_numeric_summary(frame[col])}
    for col in categorical_cols[:50]:
        if col in column_summaries:
            continue
        column_summaries[col] = {"type": str(frame[col].dtype), "kind": "categorical", **_categorical_summary(frame[col])}

    correlation = None
    correlation_cols = numeric_cols[:20]
    if len(correlation_cols) >= 2:
        corr = frame[correlation_cols].corr(method="pearson", numeric_only=True).round(4)
        correlation = _json_safe(corr.to_dict())

    test_results = None
    if target_column and group_column:
        test_results = _hypothesis_test(frame, group_column=group_column, target_column=target_column)

    regression = None
    predictors = predictor_columns or []
    if target_column and predictors:
        if target_column not in frame.columns:
            regression = {"status": "not_run", "reason": f"target_column '{target_column}' không tồn tại."}
        elif not pd.api.types.is_numeric_dtype(frame[target_column]):
            regression = {"status": "not_run", "reason": "Hồi quy OLS hiện yêu cầu target_column là biến số."}
        else:
            regression = _regression_summary(frame, target_column, predictors)

    try:
        duplicate_rows = int(frame.duplicated().sum())
    except TypeError:
        duplicate_rows = None

    metadata = dict(dataset_meta)
    metadata["columns"] = [str(c) for c in frame.columns]
    metadata["numeric_columns"] = numeric_cols
    metadata["categorical_columns"] = categorical_cols
    metadata["rows_analyzed"] = actual_rows

    methods_draft = (
        f"Dữ liệu được lấy từ dataset công khai `{metadata.get('dataset_id', 'unknown')}` trên Hugging Face "
        f"(config: `{metadata.get('config', 'unknown')}`, split: `{metadata.get('split', 'unknown')}`). "
        f"Split được xử lý bằng Parquet; phân tích thực hiện trên {actual_rows:,} dòng và {len(frame.columns)} biến. "
        f"{('Do split lớn hơn giới hạn xử lý, ' + str(metadata.get('sampling_method')) + ' được sử dụng. ') if metadata.get('sampled') else 'Toàn bộ các dòng trong split được đưa vào phân tích. '}"
        f"Các bước gồm thống kê mô tả, kiểm tra dữ liệu thiếu/trùng, phân phối tần suất của biến phân loại và tương quan Pearson giữa tối đa 20 biến số đầu tiên. "
        f"{('Kiểm định nhóm được thực hiện cho ' + str(group_column) + ' và ' + str(target_column) + '. ') if test_results else ''}"
        f"{('Mô hình OLS được ước lượng với biến phụ thuộc ' + str(target_column) + ' và biến giải thích ' + ', '.join(predictors) + '. ') if regression else ''}"
        "Các phép kiểm định được xem là phân tích khám phá; cần kiểm tra giả định, cách lấy mẫu, dữ liệu thiếu và hiệu chỉnh đa kiểm định trước khi đưa ra kết luận chính thức."
    )

    results_sentences = [
        f"Bộ dữ liệu được phân tích gồm {actual_rows:,} dòng và {len(frame.columns)} cột; có {len(numeric_cols)} biến số và {len(categorical_cols)} biến phân loại nhận diện tự động.",
        f"Có {sum(x['missing_count'] for x in missing_by_col):,} ô thiếu tính trên toàn bộ bảng phân tích; số dòng trùng hoàn toàn là {duplicate_rows if duplicate_rows is not None else 'chưa xác định'}.",
    ]
    if numeric_cols:
        highlights = []
        for col in numeric_cols[:5]:
            summary = column_summaries[col]
            if summary.get("n", 0):
                highlights.append(f"{col}: mean={summary.get('mean')}, SD={summary.get('std')}, median={summary.get('median')}")
        if highlights:
            results_sentences.append("Thống kê mô tả một số biến: " + "; ".join(highlights) + ".")
    if test_results:
        if test_results.get("tests"):
            test_text = "; ".join(f"{t['name']}: statistic={t.get('statistic')}, p={t.get('p_value')}" for t in test_results["tests"])
            results_sentences.append("Kết quả kiểm định nhóm: " + test_text + ".")
        elif test_results.get("test"):
            t = test_results["test"]
            results_sentences.append(f"Kiểm định liên hệ phân loại: chi-square={t.get('statistic')}, df={t.get('degrees_of_freedom')}, p={t.get('p_value')}.")
        else:
            results_sentences.append("Kiểm định nhóm chưa thực hiện được: " + str(test_results.get("reason", "cần kiểm tra dữ liệu và giả định")) + ".")
    if regression:
        if regression.get("status") == "ok":
            results_sentences.append(
                f"Hồi quy OLS cho R²={regression.get('r_squared')}, adjusted R²={regression.get('adjusted_r_squared')}, "
                f"p tổng thể={regression.get('model_p_value')}. Cần đọc hệ số và khoảng tin cậy trong bảng chi tiết."
            )
        else:
            results_sentences.append("Hồi quy OLS chưa thực hiện được: " + str(regression.get("reason")) + ".")
    results_sentences.append(
        "Không nên diễn giải p-value như bằng chứng nhân quả; kết quả này là bản nháp sinh từ dữ liệu đã chọn, cần người nghiên cứu rà soát trước khi đưa vào bài báo."
    )

    return {
        "dataset": metadata,
        "overview": {
            "rows_analyzed": actual_rows,
            "columns_count": int(len(frame.columns)),
            "numeric_columns_count": len(numeric_cols),
            "categorical_columns_count": len(categorical_cols),
            "duplicate_rows": duplicate_rows,
            "missing_cells_total": int(sum(item["missing_count"] for item in missing_by_col)),
            "missing_by_column": missing_by_col,
        },
        "column_summaries": column_summaries,
        "correlation_pearson": correlation,
        "group_test": test_results,
        "ols_regression": regression,
        "methods_draft": methods_draft,
        "results_draft": "\n\n".join(results_sentences),
        "limitations": [
            "Dataset do người dùng chọn; kết quả phụ thuộc vào tính đại diện và chất lượng của dataset.",
            "Nếu có lấy mẫu, module dùng reservoir sampling với seed cố định 42; kết quả có thể khác khi đổi mẫu hoặc seed.",
            "P-value chưa được hiệu chỉnh cho nhiều kiểm định; cần kiểm tra giả định thống kê và xử lý dữ liệu thiếu.",
            "Tương quan và hồi quy quan sát không tự chứng minh quan hệ nhân quả.",
            "Các cột kiểu text phức tạp, ảnh, audio hoặc nested có thể chưa phù hợp với phân tích bảng tự động.",
        ],
    }
