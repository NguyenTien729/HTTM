"""Dataset discovery and analysis services.

Public datasets are discovered through Hugging Face Hub and its Dataset Viewer.
The client never supplies a download URL: only URLs returned by Hugging Face are
accepted, which keeps this service from becoming an arbitrary URL fetcher.
"""

from __future__ import annotations

import asyncio
import json
import io
import logging
import math
import random
import re
import unicodedata
from collections import Counter
from typing import Any

from pydantic import BaseModel, Field
from urllib.parse import urlparse

import httpx
import numpy as np
import pandas as pd
from scipy import stats

HUB_API = "https://huggingface.co/api/datasets"
VIEWER_API = "https://datasets-server.huggingface.co"
USER_AGENT = "HTTM-DataResearch/1.0 (academic research prototype)"
REQUEST_TIMEOUT = 25.0
QUERY_EXPANSION_TIMEOUT = 25.0
MAX_HUB_QUERY_RESULTS = 100
logger = logging.getLogger(__name__)
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
    if value.startswith("mendeley:"):
        from .repositories import parse_id
        parse_id(value)
        return value
    if not _DATASET_ID_RE.fullmatch(value):
        raise DataResearchError(
            "dataset_id không hợp lệ. Hãy dùng ID do API discover trả về, ví dụ owner/dataset-name.",
            422,
        )
    return value


def _normalized_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.replace("đ", "d").replace("Đ", "D")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _topic_terms(topic: str) -> list[str]:
    """Return useful English/Vietnamese tokens for lightweight metadata scoring."""
    stop_words = {
        "the", "and", "for", "with", "from", "into", "about", "data", "dataset",
        "datasets", "study", "studies", "research", "of", "to", "in", "on", "a", "an",
        "by", "as", "at", "is", "are", "be", "been", "being", "that", "this", "these",
        "those", "which", "using", "use", "used", "based", "among", "between", "across",
        "impact", "effect", "effects", "analysis", "exploring", "investigating", "examining",
        "va", "cua", "cho", "trong", "nhung", "mot", "cac", "ve", "tai", "anh", "huong",
        "nghien", "cuu", "doi", "voi", "duoc", "su", "dung", "cac", "nhom", "doi", "tuong",
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


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    request_timeout: float | None = None,
) -> dict[str, Any] | list[Any]:
    try:
        if request_timeout is None:
            response = await client.get(url, params=params)
        else:
            response = await client.get(url, params=params, timeout=request_timeout)
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


class DatasetSearchQueryExpansion(BaseModel):
    """Semantic paraphrases used as separate Hugging Face text searches."""

    queries: list[str] = Field(default_factory=list, max_length=12)
    concept_groups: list[list[str]] = Field(default_factory=list, max_length=5)
    scope_terms: list[str] = Field(default_factory=list, max_length=12)



# HTTM_DATASET_SEARCH_V3: search Hugging Face with English task/data terms.
# This is a search aid, NOT a claim that a suitable dataset exists.
def _has_phrase(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text))


def _search_topic(gap):
    return str(gap.get("project_topic") or " ".join(str(gap.get(k) or "") for k in ("description", "proposed_research_question")))


def _dataset_short_queries(gap):
    # No domain-specific templates: derive every fallback from this project.
    terms = list(dict.fromkeys(_topic_terms(_search_topic(gap))))
    return [" ".join(terms[i:i+3]) for i in range(0, len(terms), 2) if len(terms[i:i+3]) >= 2][:12]


def _gap_seed_queries(gap: dict[str, Any]) -> list[str]:
    """Derive fallback phrases from the project topic, or a legacy gap context."""
    description = str(gap.get("project_topic") or gap.get("description") or "").strip()
    question = "" if gap.get("project_topic") else str(gap.get("proposed_research_question") or "").strip()
    queries: list[str] = []

    for value in (description, question):
        # Keep one original-language phrase (including Vietnamese accents) for
        # sources whose index contains non-English dataset cards.
        if len(value) >= 3:
            queries.append(value[:120])
        terms = _topic_terms(value)
        if len(terms) >= 2:
            queries.append(" ".join(terms[:6]))
            # Preserve different parts of a long gap statement as separate searches.
            for start in range(0, max(0, len(terms) - 1), 3):
                phrase = terms[start:start + 4]
                if len(phrase) >= 2:
                    queries.append(" ".join(phrase))

    queries = _dataset_short_queries(gap) + queries
    return _dedupe_queries(queries)


def _dedupe_queries(queries: list[str], excluded: set[str] | None = None) -> list[str]:
    seen = set(excluded or set())
    result: list[str] = []
    for raw in queries:
        query = re.sub(r"\s+", " ", str(raw or "")).strip(" \t\r\n-;,.:")
        normalized = _normalized_text(query)
        if len(normalized) < 3 or normalized in seen:
            continue
        seen.add(normalized)
        result.append(query[:120])
    return result


async def _expand_gap_queries(
    gap: dict[str, Any],
    excluded_queries: list[str],
    round_number: int,
) -> list[str]:
    """Ask the configured LLM for near-synonym searches; caller enforces timeout."""
    from app.services.llm import generate_structured

    prompt = (
        "Tìm dữ liệu theo CHỦ ĐỀ PROJECT làm tiêu chí chính; gap chỉ bổ sung, không đổi lĩnh vực.\n"
        f"Chủ đề project: {_search_topic(gap)}\n"
        f"Gap bổ sung: {gap.get('description', '')}\n"
        f"Câu hỏi của gap: {gap.get('proposed_research_question', '')}\n"
        "Trả queries: tối đa 10 truy vấn tiếng Anh ngắn 2-6 từ cho Hugging Face, gắn với biến, "
        "đối tượng và dạng dữ liệu của project. Hiểu đúng nghĩa cụm từ: ảnh hưởng là impact, không phải image.\n"
        "Trả concept_groups: 2-4 nhóm khái niệm cốt lõi bắt buộc của CHỦ ĐỀ PROJECT. Mỗi nhóm là "
        "danh sách từ/cụm đồng nghĩa tiếng Việt không dấu và tiếng Anh. Các nhóm thể hiện các khía cạnh "
        "khác nhau như vấn đề, đối tượng, biến nghiên cứu; không lặp cùng ý hoặc thêm ý ngoài chủ đề. "
        "Địa điểm/thời gian chỉ để rà soát phạm vi, không đưa thành nhóm bắt buộc. "
        "Trả thêm scope_terms: các tên/biến thể tiếng Anh và tiếng Việt không dấu của địa bàn trong topic; "
        "để rỗng nếu topic không giới hạn địa bàn. Dùng từ đồng nghĩa về cách đo biến nhưng không khẳng định "
        "chỉ số cảm nhận hoặc biến thay thế là kết quả đo khách quan. "
        "Không áp đặt lĩnh vực học tập hay bất cứ ngành cố định nào.\n"
        f"Vòng {round_number}; không lặp queries: {json.dumps(excluded_queries, ensure_ascii=False)}"
    )
    result = await generate_structured(prompt, DatasetSearchQueryExpansion)
    groups = [[_normalized_text(term) for term in group if 2 <= len(str(term).strip()) <= 100] for group in result.concept_groups]
    groups = [list(dict.fromkeys(term for term in group if len(term) >= 2)) for group in groups]
    groups = [group for group in groups if group]
    if len(groups) >= 2 and "_search_concept_groups" not in gap:
        gap["_search_concept_groups"] = groups
    if "_search_scope_terms" not in gap:
        gap["_search_scope_terms"] = [_normalized_text(t) for t in result.scope_terms if len(_normalized_text(t)) >= 3]
    return result.queries


def _to_candidate(item: dict[str, Any], queries: list[str]) -> dict[str, Any]:
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
    search_terms = set(search_text.split())

    query_matches: list[tuple[str, float, list[str]]] = []
    for query in queries:
        terms = _topic_terms(query)
        if not terms:
            continue
        matched = [term for term in terms if term in search_terms]
        score = len(matched) / max(1, len(set(terms)))
        if score > 0:
            query_matches.append((query, score, matched))

    query_matches.sort(key=lambda value: value[1], reverse=True)
    best_score = query_matches[0][1] if query_matches else 0.0
    matched_terms = list(dict.fromkeys(term for _, _, terms in query_matches for term in terms))
    matched_queries = [query for query, _, _ in query_matches]
    return {
        "dataset_id": dataset_id,
        "title": dataset_id.split("/", 1)[-1].replace("-", " ").replace("_", " "),
        "description": description[:6000],
        "downloads": int(item.get("downloads") or 0),
        "likes": int(item.get("likes") or 0),
        "last_modified": item.get("lastModified"),
        "license": card.get("license"),
        "task_categories": (card.get("task_categories") or [])[:10],
        "tags": (item.get("tags") or [])[:15],
        "gap_match_score": round(best_score, 3),
        "matched_terms": matched_terms,
        "matched_queries": matched_queries[:8],
        "dataset_url": f"https://huggingface.co/datasets/{dataset_id}",
        "source": "Hugging Face Hub",
    }


def _candidate_relevance(candidate, gap):
    """Require evidence in metadata, independently of LLM-generated search terms."""
    text = _normalized_text(" ".join([candidate["dataset_id"], candidate["description"],
        " ".join(candidate["tags"]), " ".join(candidate["task_categories"])]))
    groups = gap.get("_search_concept_groups") or []
    if groups:
        evidence = [[term for term in group if _has_phrase(text, term)] for group in groups]
        matched_count = sum(bool(group) for group in evidence)
        if matched_count < 2 or matched_count / len(groups) < .5:
            return False
        score = sum(bool(group) for group in evidence) / len(groups)
        matched = [term for group in evidence for term in group]
    else:
        # Without semantic expansion, only accept direct project-topic evidence.
        terms = set(_topic_terms(_search_topic(gap)))
        matched = sorted(terms & set(text.split()))
        score = len(matched) / max(1, len(terms))
        if len(matched) < 2 or score < .3:
            return False
    candidate["topic_match_score"] = round(score, 3)
    candidate["gap_match_score"] = round(score, 3)
    candidate["relevance_evidence"] = {"project_topic": _search_topic(gap), "matched_topic_terms": matched,
        "concept_groups": groups}
    missing = [group for group, matches in zip(groups, evidence) if not matches] if groups else []
    candidate["missing_topic_concepts"] = missing
    scope_terms = gap.get("_search_scope_terms", [])
    scope_matches = [term for term in scope_terms if _has_phrase(text, term)]
    candidate["scope_match"] = "mentioned_in_metadata" if scope_matches else "unverified" if scope_terms else "not_specified"
    candidate["relevance_evidence"]["scope_terms_matched"] = scope_matches
    if scope_terms:
        candidate["topic_match_score"] = candidate["gap_match_score"] = round(.9*score + (.1 if scope_matches else 0), 3)
    candidate["match_scope"] = "partial_topic_match" if missing else "project_topic_metadata_match"
    candidate["review_required"] = True
    candidate["relevance_note"] = ("Khớp một phần chủ đề; chưa xác nhận các khái niệm: " + "; ".join(" / ".join(g[:3]) for g in missing) + ". Cần kiểm tra biến trước khi dùng."
        if missing else "Khớp khái niệm của chủ đề project trong metadata; cần xác minh biến, đối tượng, địa bàn và giấy phép.")
    if scope_terms and not scope_matches:
        candidate["relevance_note"] += " Chưa xác nhận địa bàn theo đề tài."
    return True


async def discover_gap_datasets(
    gap: dict[str, Any],
    limit: int = 10,
    max_search_seconds: float = 60,
) -> dict[str, Any]:
    """Search for the persisted project topic, with an optional gap as context.

    The whole workflow (LLM query expansion + all Hub requests) shares one deadline.
    It stops as soon as ``limit`` relevant candidates have been collected or that
    deadline expires. Candidate metadata must match topic concepts, regardless
    of the query that retrieved the candidate.
    """
    gap = dict(gap)  # Semantic search state must not mutate the stored gap.
    description = str(gap.get("description") or "").strip()
    if len(_search_topic(gap).strip()) < 3:
        raise DataResearchError("Research gap được chọn không có mô tả hợp lệ.", 422)
    limit = max(1, min(int(limit), 20))
    max_search_seconds = max(1.0, min(float(max_search_seconds), 180.0))

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    deadline = started_at + max_search_seconds
    seed_queries = _gap_seed_queries(gap)
    used_queries: list[str] = []
    query_queue: list[str] = []
    found: dict[str, dict[str, Any]] = {}
    rejected_ids: set[str] = set()
    request_errors: list[str] = []
    expansion_round = 0
    expansion_mode = "llm_with_gap_fallback"
    llm_expansion_failed = False
    stop_reason = "query_exhausted"

    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers
    ) as client:
        # LLM expansion is part of the same time budget. Keep a share of the
        # deadline for actual Hub calls so a slow LLM cannot consume everything.
        remaining = deadline - loop.time()
        if remaining > 1.0:
            expansion_round = 1
            expansion_timeout = min(QUERY_EXPANSION_TIMEOUT, max(0.5, remaining * 0.35))
            try:
                expanded = await asyncio.wait_for(
                    _expand_gap_queries(gap, [], expansion_round),
                    timeout=expansion_timeout,
                )
                query_queue.extend(_dedupe_queries(expanded))
            except Exception as exc:  # fallback keeps discovery available without an LLM key
                logger.warning("Gap query expansion failed; using deterministic queries: %s", exc)
                expansion_mode = "deterministic_fallback"
                llm_expansion_failed = True
        else:
            expansion_mode = "deterministic_fallback"

        query_queue.extend(_dedupe_queries(seed_queries, {
            _normalized_text(query) for query in query_queue
        }))

        from .repositories import search_mendeley
        remaining = deadline - loop.time()
        if remaining > .2:
            external_timeout = min(15., max(.1, remaining * .3))
            try:
                external = await asyncio.wait_for(search_mendeley(client, gap, query_queue, external_timeout), timeout=external_timeout)
                found.update({candidate["dataset_id"]: candidate for candidate in external})
            except Exception as exc:
                logger.warning("Research repository search failed: %s", type(exc).__name__)
                request_errors.append("Mendeley/DataCite: " + type(exc).__name__)

        while len(found) < limit:
            remaining = deadline - loop.time()
            if remaining <= 0:
                stop_reason = "time_limit"
                break

            if not query_queue:
                # The first synonym batch may not find enough data. Ask for a
                # genuinely different batch, excluding every phrase already used.
                if llm_expansion_failed or expansion_round >= 3 or remaining < 1.0:
                    stop_reason = "query_exhausted"
                    break
                expansion_round += 1
                expansion_timeout = min(QUERY_EXPANSION_TIMEOUT, max(0.5, remaining * 0.2))
                try:
                    expanded = await asyncio.wait_for(
                        _expand_gap_queries(gap, used_queries, expansion_round),
                        timeout=expansion_timeout,
                    )
                except Exception as exc:
                    logger.warning("Further gap query expansion failed: %s", exc)
                    expansion_mode = "deterministic_fallback"
                    llm_expansion_failed = True
                    expanded = []
                query_queue = _dedupe_queries(expanded, {
                    _normalized_text(query) for query in used_queries
                })
                if not query_queue:
                    stop_reason = "query_exhausted"
                    break

            query = query_queue.pop(0)
            normalized_query = _normalized_text(query)
            if normalized_query in {_normalized_text(x) for x in used_queries}:
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                stop_reason = "time_limit"
                break
            used_queries.append(query)

            try:
                result = await asyncio.wait_for(
                    _get_json(
                        client,
                        HUB_API,
                        {
                            "search": query,
                            "limit": min(MAX_HUB_QUERY_RESULTS, max(50, limit * 5)),
                            "sort": "downloads",
                            "direction": "-1",
                            "full": "true",
                        },
                        request_timeout=max(0.1, min(REQUEST_TIMEOUT, remaining)),
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                stop_reason = "time_limit"
                break
            except DataResearchError as exc:
                request_errors.append(f"{query}: {exc.message}")
                if exc.status_code == 429:
                    stop_reason = "source_rate_limited"
                    break
                if loop.time() >= deadline:
                    stop_reason = "time_limit"
                    break
                continue

            if not isinstance(result, list):
                continue
            for item in result:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                dataset_id = str(item["id"])
                if not _DATASET_ID_RE.fullmatch(dataset_id):
                    continue
                candidate = _to_candidate(item, seed_queries)
                # Hugging Face search already matched the query; the metadata
                # score is an additional relevance check/ranking signal.
                if not _candidate_relevance(candidate, gap):
                    rejected_ids.add(dataset_id)
                    continue
                candidate["discovered_by_query"] = query
                existing = found.get(dataset_id)
                if existing is None:
                    found[dataset_id] = candidate
                else:
                    existing["matched_queries"] = list(dict.fromkeys(
                        existing["matched_queries"] + candidate["matched_queries"]
                    ))[:12]
                    existing["matched_terms"] = list(dict.fromkeys(
                        existing["matched_terms"] + candidate["matched_terms"]
                    ))
                    if candidate["gap_match_score"] > existing["gap_match_score"]:
                        candidate["matched_queries"] = existing["matched_queries"]
                        candidate["matched_terms"] = existing["matched_terms"]
                        found[dataset_id] = candidate

            if len(found) >= limit:
                stop_reason = "enough_results"
                break

    elapsed = round(loop.time() - started_at, 2)
    if len(found) >= limit and stop_reason == "query_exhausted":
        stop_reason = "enough_results"
    candidates = sorted(
        found.values(),
        key=lambda item: (item.get("scope_match") == "mentioned_in_metadata", item["gap_match_score"], item["downloads"], item["likes"]),
        reverse=True,
    )[:limit]
    gap_summary = {
        "gap_id": gap.get("gap_id", gap.get("id")),
        "project_id": gap.get("project_id"),
        "gap_type": gap.get("gap_type"),
        "description": description,
        "proposed_research_question": gap.get("proposed_research_question", ""),
        "confirmed": bool(gap.get("confirmed", False)),
    }
    return {
        "input_source": "project_topic" if gap.get("project_topic") else "selected_research_gap",
        "project_topic": gap.get("project_topic"),
        "gap": gap_summary,
        "source": "Hugging Face Hub + Mendeley Data (DataCite)",
        "count": len(candidates),
        "requested_count": limit,
        "datasets": candidates,
        "search": {
            "strategy": "Project-topic queries and dynamic concept groups; gap as supplementary context; metadata evidence required",
            "topic_concept_groups": gap.get("_search_concept_groups", []),
            "query_expansion_mode": expansion_mode,
            "queries_used": used_queries,
            "searched_query_count": len(used_queries),
            "elapsed_seconds": elapsed,
            "time_budget_seconds": max_search_seconds,
            "time_limit_reached": stop_reason == "time_limit",
            "stop_reason": stop_reason,
            "request_errors": request_errors[:5],
            "english_fallback_queries": _dataset_short_queries(gap),
            "rejected_irrelevant_count": len(rejected_ids),
            "insufficient_relevant_results": len(candidates) < limit,
            "sources_searched": ["Hugging Face", "Mendeley Data / DataCite"],
            "note": "Không có kết quả không chứng minh dữ liệu không tồn tại. Ứng viên khớp một phần không bảo đảm đo được toàn bộ đề tài.",
        },
        "review_note": (
            "Danh sách được tìm theo chủ đề project; research gap bổ sung yêu cầu. "
            "Các truy vấn gần nghĩa giúp mở rộng cách diễn đạt; điểm khớp chỉ dựa trên metadata "
            "của các kho dữ liệu, vì vậy vẫn cần mở dataset, kiểm tra cột, mẫu, giấy phép và phạm vi dữ liệu trước khi chọn."
        ),
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
    """Inspect a public dataset; keep metadata available when row preview fails."""
    dataset_id = validate_dataset_id(dataset_id)
    if dataset_id.startswith("mendeley:"):
        from .repositories import inspect
        return await inspect(dataset_id, config, split)
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers) as client:
        available_splits = await _get_splits(client, dataset_id)
        selected = next((x for x in available_splits if (not config or x["config"] == config) and (not split or x["split"] == split)), None)
        if selected is None:
            raise DataResearchError("config/split bạn chọn không nằm trong danh sách split của dataset.", 422)

        preview_available = True
        preview_error = None
        preview_error_code = None
        rows_result: dict[str, Any] = {}
        try:
            # A successful /splits response does NOT guarantee /rows works.
            response = await client.get(f"{VIEWER_API}/rows", params={
                "dataset": dataset_id, "config": selected["config"],
                "split": selected["split"], "offset": 0, "length": MAX_PREVIEW_ROWS,
            })
            if response.status_code >= 400:
                preview_available = False
                try:
                    error_payload = response.json()
                except ValueError:
                    error_payload = {}
                raw_error = str(error_payload.get("error") or "") if isinstance(error_payload, dict) else ""
                if "Scan size limit exceeded" in raw_error:
                    preview_error_code = "parquet_scan_limit"
                    preview_error = (
                        "Dataset tồn tại và có split hợp lệ, nhưng Hugging Face Dataset Viewer "
                        "không thể xem trước vì file Parquet vượt giới hạn quét dữ liệu. "
                        "Đây không phải lỗi tìm kiếm; không nên thử tải toàn bộ file để vượt giới hạn."
                    )
                elif response.status_code in (401, 403):
                    preview_error_code = "access_restricted"
                    preview_error = "Dataset Viewer không cho phép xem các dòng dữ liệu (HTTP %d)." % response.status_code
                elif response.status_code == 429:
                    preview_error_code = "rate_limited"
                    preview_error = "Dataset Viewer đang giới hạn yêu cầu (HTTP 429); hãy thử lại sau."
                else:
                    preview_error_code = "viewer_error"
                    preview_error = "Dataset Viewer không thể trả dữ liệu mẫu (HTTP %d): %s" % (
                        response.status_code, raw_error[:350] or "không có thông tin bổ sung"
                    )
            else:
                try:
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("Response không phải JSON object")
                    rows_result = payload
                except ValueError:
                    preview_available = False
                    preview_error_code = "invalid_viewer_response"
                    preview_error = "Dataset Viewer trả dữ liệu mẫu không hợp lệ."
        except httpx.TimeoutException:
            preview_available = False
            preview_error_code = "viewer_timeout"
            preview_error = "Dataset Viewer quá thời gian phản hồi khi xem dữ liệu mẫu."
        except httpx.HTTPError:
            preview_available = False
            preview_error_code = "viewer_connection_error"
            preview_error = "Không kết nối được tới Dataset Viewer để xem dữ liệu mẫu."

        # The Hub API supplies metadata even when the Viewer cannot read Parquet rows.
        hub_metadata: dict[str, Any] = {}
        try:
            hub_result = await _get_json(client, f"{HUB_API}/{dataset_id}", {})
            if isinstance(hub_result, dict):
                card = hub_result.get("cardData") or {}
                siblings = hub_result.get("siblings") or []
                hub_metadata = {
                    "description": str(card.get("description") or "")[:1200],
                    "license": card.get("license"),
                    "downloads": hub_result.get("downloads"),
                    "last_modified": hub_result.get("lastModified"),
                    "files": [
                        {"filename": item["rfilename"]}
                        for item in siblings[:100]
                        if isinstance(item, dict) and isinstance(item.get("rfilename"), str)
                    ],
                }
        except DataResearchError:
            logger.warning("Cannot fetch Hub metadata for %s", dataset_id)

    rows = rows_result.get("rows", [])
    features = rows_result.get("features", [])
    preview_rows = []
    for wrapped in rows if isinstance(rows, list) else []:
        row = wrapped.get("row", {}) if isinstance(wrapped, dict) else {}
        if isinstance(row, dict):
            preview_rows.append({str(k): _json_safe(v) for k, v in row.items()})
    total = rows_result.get("num_rows_total", selected.get("num_rows"))
    return {
        "dataset_id": dataset_id,
        "dataset_url": f"https://huggingface.co/datasets/{dataset_id}",
        "source": "Hugging Face Dataset Viewer / Hub API",
        "available_splits": available_splits,
        "preview_selection": {"config": selected["config"], "split": selected["split"]},
        "num_rows_total": int(total) if total is not None else None,
        "columns": [{"name": f.get("name"), "type": f.get("type")} for f in features if isinstance(f, dict)],
        "preview_rows": preview_rows,
        "preview_available": preview_available,
        "preview_error_code": preview_error_code,
        "preview_error": preview_error,
        "analysis_ready": bool(preview_available and preview_rows),
        "analysis_note": (
            "Chưa xem được dữ liệu mẫu. Không nên xác nhận Analyze cho đến khi kiểm tra "
            "được định dạng, cột và kích thước file. Module Analyze chỉ hỗ trợ Parquet "
            "dạng bảng và giới hạn tải 100 MB truyền dữ liệu tổng, tối đa 40 file."
            if not preview_available else None
        ),
        "hub_metadata": hub_metadata,
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


class _RemoteParquetReader(io.RawIOBase):
    """Bounded HTTP Range reader: never download the complete Parquet shard."""

    def __init__(self, client, url: str, budget: dict[str, int]):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "huggingface.co" or host.endswith(".huggingface.co") or host == "hf.co" or host.endswith(".hf.co")):
            raise DataResearchError("URL Parquet không thuộc nguồn Hugging Face được phép.", 502)
        self.client, self.url, self.budget = client, url, budget
        try:
            response = client.head(url)
            response.raise_for_status()
            self.length = int(response.headers.get("content-length") or 0)
        except (httpx.HTTPError, ValueError) as exc:
            raise DataResearchError("Không xác định được kích thước file Parquet để đọc theo vùng.", 502) from exc
        if self.length <= 0:
            raise DataResearchError("Nguồn Parquet không cung cấp Content-Length.", 422)
        self.pos = 0
        self.cache_start = -1
        self.cache = b""

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        if whence == 0:
            pos = offset
        elif whence == 1:
            pos = self.pos + offset
        elif whence == 2:
            pos = self.length + offset
        else:
            raise ValueError("invalid whence")
        if pos < 0:
            raise ValueError("negative seek")
        self.pos = pos
        return pos

    def read(self, size=-1):
        if self.pos >= self.length:
            return b""
        if size is None or size < 0:
            size = self.length - self.pos
        size = min(size, self.length - self.pos)
        if not size:
            return b""
        if size > 8 * 1024 * 1024:
            raise DataResearchError("Một vùng Parquet cần đọc vượt 8 MB. Dataset không phù hợp để phân tích bảng an toàn.", 413)
        if self.cache_start <= self.pos and self.pos + size <= self.cache_start + len(self.cache):
            offset = self.pos - self.cache_start
            result = self.cache[offset:offset + size]
            self.pos += len(result)
            return result
        fetch_size = min(self.length - self.pos, max(size, 256 * 1024))
        if self.budget["used"] + fetch_size > MAX_DOWNLOAD_BYTES:
            raise DataResearchError("Đọc Parquet vượt ngân sách truyền dữ liệu 100 MB. Dataset quá lớn để phân tích an toàn.", 413)
        begin = self.pos
        try:
            with self.client.stream("GET", self.url, headers={"Range": f"bytes={begin}-{begin + fetch_size - 1}"}) as response:
                if response.status_code != 206 or not response.headers.get("content-range", "").startswith(f"bytes {begin}-"):
                    raise DataResearchError("Máy chủ không hỗ trợ HTTP Range cho file Parquet; đã ngừng để tránh tải toàn bộ file.", 422)
                chunks, received = [], 0
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received > fetch_size:
                        raise DataResearchError("Nguồn Parquet trả về nhiều dữ liệu hơn vùng yêu cầu.", 502)
                    chunks.append(chunk)
                data = b"".join(chunks)
        except httpx.HTTPError as exc:
            raise DataResearchError("Không đọc được vùng dữ liệu Parquet từ Hugging Face.", 502) from exc
        if len(data) != fetch_size:
            raise DataResearchError("Vùng Parquet trả về thiếu dữ liệu.", 502)
        self.budget["used"] += len(data)
        self.cache_start, self.cache = begin, data
        result = data[:size]
        self.pos += len(result)
        return result


def _safe_parquet_columns(schema):
    """Project scalar leaves, including metadata inside structs, without binaries."""
    import pyarrow as pa
    accepted = []
    def visit(field, prefix=""):
        path = f"{prefix}.{field.name}" if prefix else field.name
        dtype = field.type
        if pa.types.is_struct(dtype):
            for child in dtype:
                visit(child, path)
            return
        if pa.types.is_dictionary(dtype):
            dtype = dtype.value_type
        if (pa.types.is_integer(dtype) or pa.types.is_floating(dtype) or
            pa.types.is_boolean(dtype) or pa.types.is_string(dtype) or
            pa.types.is_large_string(dtype) or pa.types.is_date(dtype) or
            pa.types.is_timestamp(dtype) or pa.types.is_decimal(dtype)):
            accepted.append(path)
    for field in schema:
        visit(field)
    return accepted


def _project_record(record, columns):
    """Arrow retains struct parents when reading selected leaf columns."""
    result = {}
    for path in columns:
        if path in record:
            result[path] = record[path]
            continue
        value = record
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        result[path] = value
    return result


async def download_dataset_dataframe(
    dataset_id: str, config: str, split: str, max_rows: int = 50_000
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Bounded remote Parquet column reads and deterministic reservoir sampling.

    Avoids downloading full image-heavy shards. Only scalar columns are loaded.
    """
    dataset_id = validate_dataset_id(dataset_id)
    if dataset_id.startswith("mendeley:"):
        from .repositories import download
        return await download(dataset_id, config, split, max_rows)
    if max_rows < 100 or max_rows > MAX_ANALYSIS_ROWS:
        raise DataResearchError(f"max_rows phải nằm trong khoảng 100–{MAX_ANALYSIS_ROWS}.", 422)
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise DataResearchError("Thiếu pyarrow. Hãy cài requirements.txt.", 500) from exc

    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers) as client:
        splits = await _get_splits(client, dataset_id)
        if not any(item["config"] == config and item["split"] == split for item in splits):
            raise DataResearchError("config/split không hợp lệ.", 422)
        result = await _get_json(client, f"{VIEWER_API}/parquet", {"dataset": dataset_id})
    files = [item for item in result.get("parquet_files", [])
             if isinstance(item, dict) and item.get("config") == config
             and item.get("split") == split and item.get("url")]
    if not files:
        raise DataResearchError("Không có Parquet công khai cho split này.", 422)
    if len(files) > MAX_PARQUET_FILES:
        raise DataResearchError(f"Split có hơn {MAX_PARQUET_FILES} file; chưa thể phân tích an toàn.", 413)

    budget = {"used": 0}
    rng = random.Random(42)
    reservoir = []
    seen_rows = 0
    total_rows = 0
    columns = None
    source_schema = None
    excluded_columns = []
    # Bound the number of Python row objects to keep image-rich datasets from exhausting RAM.
    effective_max_rows = max_rows
    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers) as client:
        for info in files:
            reader = _RemoteParquetReader(client, str(info["url"]), budget)
            try:
                parquet = pq.ParquetFile(reader)
                total_rows += parquet.metadata.num_rows
                schema_signature = str(parquet.schema_arrow)
                if source_schema is not None and source_schema != schema_signature:
                    raise DataResearchError("Các file Parquet có schema không đồng nhất.", 422)
                source_schema = schema_signature
                safe = _safe_parquet_columns(parquet.schema_arrow)
                excluded_columns = [str(f.name) for f in parquet.schema_arrow if not any(c == f.name or c.startswith(f.name + ".") for c in safe)]
                if not safe:
                    continue
                if columns is None:
                    columns = safe
                elif set(columns) != set(safe):
                    raise DataResearchError("Các file Parquet có schema bảng không đồng nhất.", 422)
                for batch in parquet.iter_batches(batch_size=256, columns=columns):
                    for record in batch.to_pylist():
                        record = _project_record(record, columns)
                        seen_rows += 1
                        if len(reservoir) < effective_max_rows:
                            reservoir.append(record)
                        else:
                            index = rng.randrange(seen_rows)
                            if index < effective_max_rows:
                                reservoir[index] = record
            except DataResearchError:
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                raise DataResearchError("Không đọc được các cột bảng Parquet bằng HTTP Range. Dataset có thể cần chuyển đổi trước khi phân tích.", 422) from exc
    structure_only = not columns and total_rows > 0
    if not reservoir and not structure_only:
        raise DataResearchError("Dataset không có dòng dữ liệu bảng để phân tích.", 422)
    frame = pd.DataFrame.from_records(reservoir)
    sampled = seen_rows > effective_max_rows
    return frame, {
        "dataset_id": dataset_id,
        "config": config,
        "split": split,
        "source_url": f"https://huggingface.co/datasets/{dataset_id}",
        "rows_in_selected_split": int(total_rows),
        "rows_analyzed": len(frame),
        "columns_count": len(frame.columns),
        "parquet_files_used": len(files),
        "downloaded_bytes": budget["used"],
        "sampled": sampled,
        "sampling_method": "Chỉ đọc metadata, không lấy mẫu nội dung" if structure_only else ("Reservoir sampling, random seed=42" if sampled else "Toàn bộ các dòng trong split đã chọn"),
        "max_rows": effective_max_rows,
        "requested_max_rows": max_rows,
        "excluded_non_tabular_columns": True,
        "excluded_columns": excluded_columns,
        "source_schema": source_schema,
        "analysis_mode": "structure_only" if structure_only else "tabular_metadata",
        "content_analyzed": False,
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
    column_roles: dict[str, str] | None = None,
    correlation_columns: list[str] | None = None,
) -> dict[str, Any]:
    if dataset_meta.get("analysis_mode") == "structure_only":
        count = dataset_meta.get("rows_in_selected_split", 0)
        reason = "Không có cột scalar để kiểm định/hồi quy; chưa đọc hoặc phân tích nội dung ảnh, binary hay danh sách lồng nhau."
        return {
            "dataset": dict(dataset_meta, rows_analyzed=0),
            "overview": {"rows_analyzed": 0, "rows_in_selected_split": count,
                "columns_count": 0, "numeric_columns_count": 0, "categorical_columns_count": 0,
                "duplicate_rows": None, "missing_cells_total": None, "missing_by_column": []},
            "column_summaries": {}, "column_roles": {}, "charts": [],
            "correlation_pearson": None, "correlation_spearman": None,
            "group_test": {"status": "not_run", "reason": reason},
            "ols_regression": {"status": "not_run", "reason": reason},
            "quality_report": {"scope": "parquet_metadata", "columns": [], "note": reason},
            "analysis_plan": {"research_gap": dataset_meta.get("research_gap"),
                "suitability_status": "requires_content_analysis", "reason": reason},
            "methods_draft": "Chỉ đọc schema và số bản ghi từ metadata Parquet; không giải mã nội dung hoặc tạo thống kê từ mã dòng sinh tự động.",
            "results_draft": f"Split đã chọn có {count:,} bản ghi theo metadata Parquet. " + reason,
            "limitations": [reason, "Cần bộ phân tích chuyên biệt để đo kích thước/chất lượng ảnh, phân bố đối tượng hoặc đặc trưng audio."],
        }
    if frame.empty or len(frame.columns) == 0:
        raise DataResearchError("Dataset đã chọn không có dữ liệu dạng bảng để phân tích.", 422)
    frame = frame.copy()
    frame.columns = [str(column) for column in frame.columns]
    from .profiling import prepare_analysis, enrich_analysis
    frame, roles, quality = prepare_analysis(frame, column_roles or {})
    numeric_cols = [c for c in frame.columns if roles[c] == "continuous"]
    categorical_cols = [c for c in frame.columns if roles[c] == "categorical"]
    if target_column and roles.get(target_column) == "categorical":
        frame[target_column] = frame[target_column].astype("string")
    predictors = predictor_columns or []
    if target_column and target_column == group_column:
        raise DataResearchError("Biến nhóm phải khác target.", 422)
    if target_column and roles.get(target_column) in {"identifier", "text", "datetime"}:
        raise DataResearchError("Target phải là biến liên tục hoặc phân loại đã xác nhận.", 422)
    if group_column and roles.get(group_column) != "categorical":
        raise DataResearchError("Biến nhóm phải được xác nhận là phân loại.", 422)
    if len(set(predictors)) != len(predictors) or any(roles.get(p) != "continuous" or p == target_column for p in predictors):
        raise DataResearchError("Predictor phải là biến liên tục khác nhau và khác target.", 422)
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
    correlation_cols = correlation_columns if correlation_columns is not None else numeric_cols[:20]
    if len(correlation_cols) > 20 or len(set(correlation_cols)) != len(correlation_cols) or any(c not in numeric_cols for c in correlation_cols):
        raise DataResearchError("Chọn tối đa 20 biến liên tục khác nhau cho tương quan.", 422)
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

    research_topic = str(metadata.get("research_topic") or "").strip()
    topic_context = f"Chủ đề nghiên cứu được kế thừa từ Research Planner: {research_topic}. " if research_topic else ""
    methods_draft = (
        topic_context
        + f"Dữ liệu được lấy từ dataset công khai `{metadata.get('dataset_id', 'unknown')}` trên {metadata.get('source', 'Hugging Face')} "
        f"(config: `{metadata.get('config', 'unknown')}`, split: `{metadata.get('split', 'unknown')}`). "
        f"Dữ liệu bảng được đọc {'từ file ' + str(metadata['filename']) if metadata.get('filename') else 'từ split Parquet'}; phân tích thực hiện trên {actual_rows:,} dòng và {len(frame.columns)} biến. "
        f"{('Do split lớn hơn giới hạn xử lý, ' + str(metadata.get('sampling_method')) + ' được sử dụng. ') if metadata.get('sampled') else 'Toàn bộ các dòng trong split được đưa vào phân tích. '}"
        f"Các bước gồm thống kê mô tả, kiểm tra dữ liệu thiếu/trùng, phân phối tần suất của biến phân loại và tương quan trên các biến liên tục đã chọn: {', '.join(correlation_cols) or 'không chọn'}. "
        f"{('Kiểm định nhóm được thực hiện cho ' + str(group_column) + ' và ' + str(target_column) + '. ') if test_results and test_results.get('status') == 'ok' else ''}"
        f"{('Mô hình OLS được ước lượng với biến phụ thuộc ' + str(target_column) + ' và biến giải thích ' + ', '.join(predictors) + '. ') if regression and regression.get('status') == 'ok' else ''}"
        "Các phép kiểm định được xem là phân tích khám phá; cần kiểm tra giả định, cách lấy mẫu, dữ liệu thiếu và hiệu chỉnh đa kiểm định trước khi đưa ra kết luận chính thức."
    )

    results_sentences = []
    if research_topic:
        results_sentences.append(
            f"Phân tích được thực hiện trong bối cảnh chủ đề nghiên cứu: {research_topic}. "
            "Các thống kê dưới đây mô tả dataset đã chọn và tự thân chưa đủ để kết luận giả thuyết nghiên cứu."
        )
    results_sentences.extend([
        f"Bộ dữ liệu được phân tích gồm {actual_rows:,} dòng và {len(frame.columns)} cột; có {len(numeric_cols)} biến số và {len(categorical_cols)} biến phân loại nhận diện tự động.",
        f"Có {sum(x['missing_count'] for x in missing_by_col):,} ô thiếu tính trên toàn bộ bảng phân tích; số dòng trùng hoàn toàn là {duplicate_rows if duplicate_rows is not None else 'chưa xác định'}.",
    ])
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

    result = {
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
            "P-value gốc và p-value hiệu chỉnh BY trong họ kiểm định nhóm được báo riêng; cần kiểm tra giả định và xử lý dữ liệu thiếu.",
            "Tương quan và hồi quy quan sát không tự chứng minh quan hệ nhân quả.",
            "Các cột kiểu text phức tạp, ảnh, audio hoặc nested có thể chưa phù hợp với phân tích bảng tự động.",
        ],
    }
    return enrich_analysis(result, frame, roles, quality, correlation_cols, group_column, target_column)
