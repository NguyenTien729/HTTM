"""World Bank Open Data (World Development Indicators) as a stable data source.

Hugging Face and Mendeley rarely hold socio-economic tables. The World Bank API
is free, needs no key and returns country × year indicators for every country,
Viet Nam included, which joins naturally on ``country_iso3`` + ``year``.

* ``search_indicators`` ranks WDI indicators against a variable's search terms,
  using a locally cached catalogue (one download, then offline matching).
* ``download_indicator`` returns a long table: country_iso3, country, year and
  one value column named after the indicator code. Regional/income aggregates
  are removed, only the requested years are kept and the result is bounded.

Only URLs on api.worldbank.org are requested; no client-supplied URL is fetched.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

API = "https://api.worldbank.org/v2"
WDI_SOURCE = "2"
USER_AGENT = "HTTM-DataResearch/1.0 (academic research prototype)"
TIMEOUT = 30.0
CACHE_SECONDS = 7 * 24 * 3600
CACHE_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "worldbank-cache"
LICENSE = "CC BY 4.0 (World Bank Open Data)"
SOURCE_LABEL = "World Bank Open Data (WDI)"
DATASET_PREFIX = "worldbank:"

_STOP = {"the", "of", "and", "in", "for", "to", "a", "an", "by", "per", "on", "at", "data", "dataset",
         "rate", "total", "annual", "level", "index", "country", "countries", "year", "yearly", "value"}
_INDICATOR_ID = re.compile(r"^[A-Za-z0-9_.]{2,60}$")
_memory: dict[str, Any] = {}


class WorldBankError(Exception):
    """Expected failure talking to the World Bank API."""


def _norm(text: Any) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _tokens(text: str) -> list[str]:
    return [t for t in _norm(text).split() if len(t) > 1 and t not in _STOP]


async def _get_json(params_path: str, params: dict[str, Any]) -> Any:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT},
                                     follow_redirects=True) as client:
            response = await client.get(f"{API}/{params_path}", params={"format": "json", **params})
    except httpx.HTTPError as exc:
        raise WorldBankError("Không kết nối được World Bank API: " + type(exc).__name__) from exc
    if response.status_code >= 400:
        raise WorldBankError(f"World Bank API trả lỗi HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise WorldBankError("World Bank API trả dữ liệu không phải JSON.") from exc
    # Errors come back as [{"message": [...]}] with HTTP 200.
    if isinstance(payload, list) and payload and isinstance(payload[0], dict) and payload[0].get("message"):
        raise WorldBankError("World Bank API từ chối truy vấn: " + json.dumps(payload[0]["message"])[:300])
    return payload


def _rows(payload: Any) -> list[dict]:
    if isinstance(payload, list) and len(payload) >= 2 and isinstance(payload[1], list):
        return [row for row in payload[1] if isinstance(row, dict)]
    return []


def _read_cache(name: str) -> Any:
    if name in _memory:
        return _memory[name]
    path = CACHE_DIR / name
    try:
        if time.time() - path.stat().st_mtime < CACHE_SECONDS:
            value = json.loads(path.read_text(encoding="utf-8"))
            _memory[name] = value
            return value
    except (OSError, ValueError):
        pass
    return None


def _write_cache(name: str, value: Any) -> None:
    _memory[name] = value
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # The in-memory copy is enough for this process.


FAILURE_BACKOFF_SECONDS = 120


async def indicator_catalog() -> list[dict]:
    """WDI indicators as compact records {id, name, note, topics}."""
    cached = _read_cache("wdi_indicators.json")
    if cached is not None:
        return cached
    failed_at = _memory.get("_catalog_failed_at")
    if failed_at and time.time() - failed_at < FAILURE_BACKOFF_SECONDS:
        raise WorldBankError("World Bank API vừa không truy cập được; tạm bỏ qua nguồn này.")
    try:
        return await _load_catalog()
    except WorldBankError:
        _memory["_catalog_failed_at"] = time.time()
        raise


async def _load_catalog() -> list[dict]:
    try:
        rows = _rows(await _get_json(f"sources/{WDI_SOURCE}/indicators", {"per_page": 5000}))
    except WorldBankError:
        rows = []
    if not rows:  # Fallback: the full catalogue, filtered to WDI.
        rows = [r for r in _rows(await _get_json("indicator", {"per_page": 20000}))
                if str((r.get("source") or {}).get("id")) == WDI_SOURCE]
    catalog = [{"id": str(r.get("id")), "name": str(r.get("name") or ""),
                "note": str(r.get("sourceNote") or "")[:600],
                "topics": [str(t.get("value")) for t in (r.get("topics") or []) if isinstance(t, dict) and t.get("value")]}
               for r in rows if r.get("id") and _INDICATOR_ID.fullmatch(str(r.get("id")))]
    if not catalog:
        raise WorldBankError("Không lấy được danh mục chỉ số World Bank.")
    _write_cache("wdi_indicators.json", catalog)
    return catalog


async def country_table() -> list[dict]:
    """Real economies only: {iso3, iso2, name}; regional and income aggregates removed."""
    cached = _read_cache("countries.json")
    if cached is not None:
        return cached
    rows = _rows(await _get_json("country", {"per_page": 400}))
    table = [{"iso3": str(r.get("id")), "iso2": str(r.get("iso2Code") or ""), "name": str(r.get("name") or "")}
             for r in rows if (r.get("region") or {}).get("value") not in (None, "Aggregates")
             and len(str(r.get("id") or "")) == 3]
    if not table:
        raise WorldBankError("Không lấy được danh sách quốc gia World Bank.")
    _write_cache("countries.json", table)
    return table


def score_indicator(indicator: dict, phrases: list[str]) -> tuple[float, list[str]]:
    """Best fraction of one phrase's words found in the name (full weight) or note (half)."""
    name, note = set(_tokens(indicator["name"])), set(_tokens(indicator["note"]))
    best, matched_phrases = 0.0, []
    for phrase in phrases:
        words = _tokens(phrase)
        if not words:
            continue
        score = sum(1.0 if w in name else .5 if w in note else 0 for w in words) / len(words)
        if score > best + 1e-9:
            best, matched_phrases = score, [phrase]
        elif abs(score - best) < 1e-9 and score > 0:
            matched_phrases.append(phrase)
    return best, matched_phrases


def find_countries(text: str, countries: list[dict]) -> list[dict]:
    target = f" {_norm(text)} "
    aliases = {"VNM": ["vietnam", "viet nam"], "USA": ["united states", "usa", "hoa ky"],
               "GBR": ["united kingdom", "uk"], "KOR": ["south korea", "korea rep", "han quoc"],
               "CHN": ["china", "trung quoc"], "JPN": ["japan", "nhat ban"], "IND": ["india", "an do"]}
    found = []
    for country in countries:
        names = [_norm(country["name"])] + aliases.get(country["iso3"], [])
        if any(name and f" {name} " in target for name in names):
            found.append(country)
    return found


async def search_indicators(phrases: list[str], geography: str = "", limit: int = 4,
                            min_score: float = .67) -> list[dict]:
    """Candidates in the same shape as other data sources (dataset_id, title, …)."""
    catalog = await indicator_catalog()
    ranked = []
    for indicator in catalog:
        score, matched = score_indicator(indicator, phrases)
        if score >= min_score:
            ranked.append((score, -len(indicator["name"]), indicator, matched))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    try:
        countries = find_countries(geography, await country_table()) if geography else []
    except WorldBankError:
        countries = []
    scope = ", ".join(c["name"] for c in countries)
    results = []
    for score, _, indicator, matched in ranked[:limit]:
        results.append({
            "dataset_id": DATASET_PREFIX + indicator["id"], "title": indicator["name"],
            "description": indicator["note"], "dataset_url": f"https://data.worldbank.org/indicator/{indicator['id']}",
            "source": SOURCE_LABEL, "license": LICENSE, "tags": indicator["topics"], "task_categories": [],
            "downloads": 0, "likes": 0, "topic_match_score": round(score, 3), "matched_queries": matched,
            "resolution": "country-year",
            "scope_match": "mentioned_in_metadata" if countries else "not_specified",
            "scope_verification_required": not countries,
            "relevance_note": ("Chỉ số World Bank theo quốc gia × năm cho mọi nền kinh tế"
                               + (f", có {scope}" if scope else "")
                               + ". Lọc quốc gia/năm trong thiết kế; không có số liệu cấp tỉnh hoặc cá nhân."),
        })
    return results


def parse_years(time_scope: str, default_span: int = 20) -> tuple[int, int]:
    years = [int(y) for y in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", str(time_scope or ""))]
    if len(years) >= 2:
        return min(years), max(years)
    if len(years) == 1:
        return years[0], years[0]
    end = time.gmtime().tm_year - 1
    return end - default_span + 1, end


async def download_indicator(dataset_id: str, time_scope: str = "", max_rows: int = 10000) -> tuple[pd.DataFrame, dict]:
    indicator = dataset_id[len(DATASET_PREFIX):] if dataset_id.startswith(DATASET_PREFIX) else dataset_id
    if not _INDICATOR_ID.fullmatch(indicator):
        raise WorldBankError("Mã chỉ số World Bank không hợp lệ.")
    start, end = parse_years(time_scope)
    economies = {c["iso3"]: c["name"] for c in await country_table()}
    rows, page, pages = [], 1, 1
    while page <= pages and page <= 10:
        payload = await _get_json(f"country/all/indicator/{indicator}",
                                  {"date": f"{start}:{end}", "per_page": 20000, "page": page})
        meta = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], dict) else {}
        pages = int(meta.get("pages") or 1)
        rows.extend(_rows(payload))
        page += 1
    records = []
    for row in rows:
        iso3, value = str(row.get("countryiso3code") or ""), row.get("value")
        if iso3 not in economies or value is None:
            continue
        try:
            records.append({"country_iso3": iso3, "country": economies[iso3],
                            "year": int(str(row.get("date"))[:4]), indicator: float(value)})
        except (TypeError, ValueError):
            continue
    if not records:
        raise WorldBankError(f"Chỉ số {indicator} không có giá trị nào trong giai đoạn {start}–{end}.")
    frame = pd.DataFrame.from_records(records).sort_values(["country_iso3", "year"]).reset_index(drop=True)
    trimmed_years = []
    while len(frame) > max_rows and frame["year"].nunique() > 1:
        oldest = int(frame["year"].min())
        trimmed_years.append(oldest)
        frame = frame[frame["year"] != oldest]
    if len(frame) > max_rows:
        raise WorldBankError("Dữ liệu World Bank vượt giới hạn dòng của tiến trình.")
    name = next((r["indicator"].get("value") for r in rows
                 if isinstance(r.get("indicator"), dict) and r["indicator"].get("value")), indicator)
    return frame.reset_index(drop=True), {
        "dataset_id": DATASET_PREFIX + indicator, "config": "WDI", "split": f"{start}-{end}",
        "source_url": f"https://data.worldbank.org/indicator/{indicator}", "indicator_name": name,
        "rows_in_selected_split": int(len(frame)), "rows_analyzed": int(len(frame)),
        "columns_count": int(len(frame.columns)), "sampled": False,
        "sampling_method": "Toàn bộ quốc gia (không gồm vùng/nhóm thu nhập) trong giai đoạn yêu cầu",
        "years_requested": [start, end], "years_trimmed_for_row_limit": trimmed_years,
        "economies": int(frame["country_iso3"].nunique()), "origin": "world_bank",
        "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


async def warm_up() -> None:
    """Fetch the catalogue and countries concurrently (optional, ignores errors)."""
    await asyncio.gather(indicator_catalog(), country_table(), return_exceptions=True)
