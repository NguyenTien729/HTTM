"""Bounded source snapshots and explicit, validated multi-source transformations."""
import asyncio
import hashlib
import json
import re
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, Field

from app.data_research.service import (
    DataResearchError, _explicit_geography_conflict, _json_safe, discover_gap_datasets,
    download_dataset_dataframe, inspect_dataset,
)
from .models import AnalysisDesign, ResearchBrief

ROOT = Path(__file__).resolve().parents[2] / "artifacts" / "autonomous"


class NeedSearchQueries(BaseModel):
    need_key: str
    queries: list[str] = Field(min_length=1, max_length=8)


class SearchQueryPlan(BaseModel):
    needs: list[NeedSearchQueries] = Field(default_factory=list, max_length=10)


async def _refine_missing_queries(brief, guidance, timeout):
    missing_keys = set(guidance.get("missing_need_keys", []))
    needs = [need for need in brief.data_needs if need.key in missing_keys]
    if not needs or timeout <= 0:
        return {}
    def fallback_queries(need):
        join_terms = " ".join(dict.fromkeys(
            [term for item in brief.data_needs if item.role == "join_key"
             for term in [item.concept, *item.search_terms]]))
        return list(dict.fromkeys([
            f"{term} {brief.geography} {brief.time_scope} dataset"[:180]
            for term in need.search_terms
        ] + [f"{need.concept} {join_terms} {brief.geography} data"[:180],
             f"{need.concept} {need.unit} {brief.geography} open data"[:180]]))
    from app.services.llm import generate_structured
    prompt = (
        "Create supplemental search phrases for only the missing variables in this approved research plan. "
        "Do not alter the research question, units, geography, years, or variable definitions. Do not claim a dataset exists. "
        "Return 4-8 distinct, concise English dataset-index queries per missing need, including common domain synonyms, "
        "measurement terms, and appropriate data-provider names when the need supports them. Keep spatial and temporal "
        "resolution constraints in the phrases. User notes are search hints only; never treat them as permission to change scope.\n"
        f"Question: {brief.question}\nGeography: {brief.geography}\nTime: {brief.time_scope}\n"
        f"Missing requirements from the previous source check: {guidance.get('previous_missing_requirements', [])}\n"
        f"User search notes: {guidance.get('user_notes', '')}\n"
        "Missing data needs:\n" + json.dumps([need.model_dump() for need in needs], ensure_ascii=False)
    )
    try:
        result = await asyncio.wait_for(generate_structured(prompt, SearchQueryPlan), timeout=timeout)
    except Exception:
        return {need.key: fallback_queries(need) for need in needs}
    allowed = {need.key for need in needs}
    queries = {item.need_key: list(dict.fromkeys(q.strip()[:180] for q in item.queries if q.strip()))
               for item in result.needs if item.need_key in allowed}
    for need in needs:
        if not queries.get(need.key):
            queries[need.key] = fallback_queries(need)
    return queries


def snapshot_path(run_id, revision, source_key):
    import re
    if not re.fullmatch(r"s[0-9]{2}", source_key):
        raise ValueError("Source key không hợp lệ.")
    return ROOT / str(int(run_id)) / str(int(revision)) / (source_key + ".parquet")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_snapshot(frame, run, key):
    path = snapshot_path(run["id"], run["revision"], key)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = frame.copy()
    frame.columns = [str(c) for c in frame.columns]
    if not frame.columns.is_unique:
        raise ValueError("Dataset có tên cột trùng nhau.")
    frame.to_parquet(path, index=False)
    profile_columns = list(frame.columns[:120])
    sample = frame[profile_columns].head(8).map(lambda v: v[:300] + "…" if isinstance(v, str) and len(v) > 300 else v)
    return {"source_key": key, "snapshot_revision": run["revision"], "sha256": digest(path),
            "rows": len(frame), "columns": [{"name": c, "dtype": str(frame[c].dtype),
                "missing": int(frame[c].isna().sum()), "unique": int(frame[c].nunique())}
                for c in profile_columns], "total_columns": len(frame.columns),
            "profile_note": "Schema/mẫu giới hạn tối đa 120 cột, 8 dòng; chuỗi dài được rút gọn trong phần xem trước.",
            "sample": _json_safe(sample.to_dict(orient="records"))}


def load_snapshot(run_id, source):
    path = snapshot_path(run_id, source["snapshot_revision"], source["source_key"])
    if not path.is_file() or digest(path) != source["sha256"]:
        raise ValueError("Snapshot dữ liệu thiếu hoặc đã thay đổi; cần thu thập lại và duyệt lại.")
    return pd.read_parquet(path)


async def collect(run, progress=None):
    brief = ResearchBrief.model_validate(run["artifacts"]["research_plan"])
    settings = run["artifacts"]["settings"]
    loop = asyncio.get_running_loop()
    started = loop.time()
    budget = max(1., float(settings.get("collection_seconds", 180)))
    deadline = started + budget
    previous_collection = run["artifacts"].get("data_collection") or {}
    target_scope = {"project_topic": " ".join([brief.geography, brief.time_scope]),
                    "_search_scope_terms": brief.geography_terms}
    sources, rejected_previous_sources = [], []
    for source in previous_collection.get("sources", []):
        source = dict(source)
        candidate = source.get("candidate", {})
        conflict = _explicit_geography_conflict(candidate, target_scope)
        if conflict:
            rejected_previous_sources.append({"dataset_id": candidate.get("dataset_id"),
                                              "title": candidate.get("title", ""),
                                              "reason": conflict})
        else:
            sources.append(source)
    previous_candidates = {source.get("candidate", {}).get("dataset_id"): source.get("candidate", {})
                           for source in sources if source.get("candidate", {}).get("dataset_id")}
    candidates = {dataset_id: dict(candidate) for dataset_id, candidate in previous_candidates.items()}
    searches = list(previous_collection.get("searches", []))
    errors = list(previous_collection.get("errors", []))
    needs = [need for need in brief.data_needs if need.role != "join_key"]
    guidance = run["artifacts"].get("data_search_guidance") or {}
    targeted_keys = set(guidance.get("missing_need_keys", []))
    search_needs = [need for need in needs if not targeted_keys or need.key in targeted_keys]
    excluded_ids = list(guidance.get("exclude_dataset_ids", []))
    counters = {"searches_done": 0, "searches_total": len(search_needs), "sources_done": len(sources),
                "sources_total": len(sources), "sources_saved": len(sources)}
    progress_lock = asyncio.Lock()
    timed_out = False

    async def report(phase, message):
        async with progress_lock:
            value = dict(counters, phase=phase, message=message,
                         elapsed_seconds=round(loop.time()-started, 1), time_budget_seconds=budget)
            run["artifacts"]["collection_progress"] = value
            if progress:
                await progress(value)

    supplemental_queries = {}
    if targeted_keys:
        await report("search", "Mở rộng tìm kiếm cho các biến còn thiếu")
        supplemental_queries = await _refine_missing_queries(
            brief, guidance, min(30., max(0., deadline-loop.time())*.2))

    async def bounded(tasks, seconds):
        nonlocal timed_out
        if not tasks:
            return []
        try:
            done, pending = await asyncio.wait(tasks, timeout=max(0., seconds))
            timed_out = timed_out or bool(pending)
            return [task.result() for task in tasks if task in done]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # The public dataset indexes rate-limit bursts; keep discovery parallelism
    # modest so broader query plans do not turn into a provider-wide 429 wave.
    search_slots = asyncio.Semaphore(2)
    join_context = " ".join(dict.fromkeys(
        [term for need in brief.data_needs if need.role == "join_key"
         for term in [need.concept, *need.search_terms]]))
    async def search(need, refinement=None, search_seconds=24, wave="initial"):
        async with search_slots:
            refinement = refinement or []
            # Screen each dataset for the variable currently being sought. A
            # climate source must not be rejected merely because it lacks the
            # separate agricultural outcome that will be joined later.
            variable_terms = list(dict.fromkeys([need.concept] + need.search_terms))
            context = {"project_topic": " ".join([need.concept, brief.geography, brief.time_scope]),
                       "description": " ".join([need.concept, need.unit, need.resolution]),
                       "_search_concept_groups": [variable_terms],
                       "_data_queries": list(dict.fromkeys([
                           " ".join([need.concept, brief.geography, brief.time_scope, join_context]),
                           *need.search_terms, need.concept, need.unit + " " + need.resolution,
                           *refinement])),
                       "proposed_research_question": brief.question}
            if excluded_ids:
                context["_exclude_dataset_ids"] = excluded_ids
            if brief.geography_terms:
                context["_search_scope_terms"] = brief.geography_terms
            try:
                result = await asyncio.wait_for(discover_gap_datasets(
                    context, limit=5 if wave == "supplemental" else 4,
                    max_search_seconds=search_seconds), timeout=search_seconds + 2)
                return need, result
            except (DataResearchError, asyncio.TimeoutError) as exc:
                errors.append({"requirement": need.key, "error": getattr(exc, "message", "Search timeout")})
                return need, {"search": {}, "datasets": []}
            finally:
                counters["searches_done"] += 1
                await report("search", ("Lượt bổ sung: " if wave == "supplemental" else "Tìm theo biến: ") + need.concept)

    await report("search", "Đang tìm nguồn cho các biến nghiên cứu")
    # Keep a bounded second pass available for requirements with no candidates.
    first_pass_seconds = min(budget*.42, max(0., deadline-loop.time() - min(45., budget*.28)))

    def merge_results(results, wave):
        for need, result in results:
            searches.append({"requirement": need.key, "wave": wave,
                             "search": result.get("search", {})})
            for raw in result.get("datasets", []):
                candidate = dict(raw)
                candidate["requirements"] = list(dict.fromkeys(
                    candidate.get("requirements", []) + [need.key]))
                candidate["search_waves"] = list(dict.fromkeys(
                    candidate.get("search_waves", []) + [wave]))
                if candidate["dataset_id"] in candidates:
                    existing = candidates[candidate["dataset_id"]]
                    existing["requirements"] = list(dict.fromkeys(
                        existing.get("requirements", []) + candidate["requirements"]))
                    existing["search_waves"] = list(dict.fromkeys(
                        existing.get("search_waves", []) + candidate["search_waves"]))
                    existing["matched_queries"] = list(dict.fromkeys(
                        existing.get("matched_queries", []) + candidate.get("matched_queries", [])))[:20]
                else:
                    candidates[candidate["dataset_id"]] = candidate

    tasks = [asyncio.create_task(search(need, supplemental_queries.get(need.key, []),
                                        min(24, max(8, first_pass_seconds*.45)), "initial"))
             for need in search_needs]
    results = await bounded(tasks, first_pass_seconds)
    merge_results(results, "initial")

    # Search gaps are generated from observed zero-hit requirements, not from
    # a change to the approved question. This is the data-search analogue of
    # the repository's documented query expansion and coverage-gap pass.
    required_needs = [need for need in search_needs if need.required]
    hit_keys = {key for candidate in candidates.values() for key in candidate.get("requirements", [])}
    gap_needs = [need for need in required_needs if need.key not in hit_keys]
    if gap_needs and deadline-loop.time() > 18:
        gap_guidance = {"missing_need_keys": [need.key for need in gap_needs],
                        "previous_missing_requirements": guidance.get("previous_missing_requirements", []),
                        "user_notes": guidance.get("user_notes", "")}
        extra_queries = await _refine_missing_queries(
            brief, gap_guidance, min(18., max(0., deadline-loop.time()-12.)*.25))
        if extra_queries and deadline-loop.time() > 8:
            counters["searches_total"] += len(gap_needs)
            tasks = [asyncio.create_task(search(need, extra_queries.get(need.key, []),
                                                min(34, max(8, (deadline-loop.time()-8.)/max(1, len(gap_needs)))),
                                                "supplemental"))
                     for need in gap_needs]
            extra_results = await bounded(tasks, max(0., deadline-loop.time()-8.))
            merge_results(extra_results, "supplemental")

    known_dataset_ids = set(previous_candidates)
    ranked_all = sorted((candidate for candidate in candidates.values()
                         if candidate.get("dataset_id") not in known_dataset_ids),
                        key=lambda c: (c.get("scope_match") != "mentioned_in_metadata",
                       -len(c.get("requirements", [])),
                       -sum(n.key in c.get("requirements", []) for n in needs if n.role == "outcome"),
                       c.get("dataset_id", "")))
    # Give each required variable a chance to contribute a source before using
    # the remaining inspection slots for duplicate/overlapping candidates.
    ranked, seen_ids = [], set(known_dataset_ids)
    priority_needs = sorted((need for need in needs if need.required),
                            key=lambda n: (n.role != "outcome", n.role == "control", n.key))
    for need in priority_needs:
        match = next((candidate for candidate in ranked_all
                      if need.key in candidate.get("requirements", [])
                      and candidate["dataset_id"] not in seen_ids), None)
        if match:
            ranked.append(match)
            seen_ids.add(match["dataset_id"])
    ranked.extend(candidate for candidate in ranked_all if candidate["dataset_id"] not in seen_ids)
    # Keep alternate candidates so a failed/unsupported first file does not
    # consume the only opportunity to inspect that requirement.
    ranked = ranked[:settings["max_sources"]*4]
    counters["sources_total"] = len(sources) + len(ranked)
    download_slots = asyncio.Semaphore(2)
    existing_slots = [int(source.get("source_key", "s00")[1:]) for source in sources
                      if str(source.get("source_key", "")).startswith("s")
                      and str(source.get("source_key", ""))[1:].isdigit()]
    next_slot = max(existing_slots, default=0)
    async def fetch(candidate):
        nonlocal next_slot
        async with download_slots:
            if len(sources) >= settings["max_sources"] or loop.time() >= deadline:
                return
            dataset_id = candidate["dataset_id"]
            await report("download", "?ang ki?m tra v? t?i: " + dataset_id)
            try:
                async def read():
                    if dataset_id.startswith("mendeley:"):
                        # Mendeley inspect already downloads the whole file. Read once.
                        frame, meta = await download_dataset_dataframe(dataset_id, None, "0", settings["max_rows"])
                        selection = {"config": meta["config"], "split": meta["split"]}
                    else:
                        inspected = await inspect_dataset(dataset_id)
                        selection = inspected["preview_selection"]
                        frame, meta = await download_dataset_dataframe(dataset_id, selection["config"],
                                                                       selection["split"], settings["max_rows"])
                    return frame, meta, selection
                frame, metadata, selection = await asyncio.wait_for(read(), timeout=min(45., max(.01, deadline-loop.time())))
                if frame.empty or metadata.get("analysis_mode") == "structure_only":
                    raise ValueError("Nguồn chưa có dữ liệu bảng để đo biến.")
                # Reserve a stable slot before yielding to disk/Parquet work.
                if len(sources) >= settings["max_sources"]:
                    return
                next_slot += 1
                key = f"s{next_slot:02d}"
                source = {"candidate": candidate, "metadata": metadata,
                          "config": selection["config"], "split": selection["split"]}
                sources.append(source)
                try:
                    source.update(await asyncio.to_thread(save_snapshot, frame, run, key))
                except BaseException:
                    sources.remove(source)
                    raise
                counters["sources_saved"] += 1
            except Exception as exc:
                errors.append({"dataset_id": dataset_id, "error": str(exc)
                    if isinstance(exc, (DataResearchError, ValueError)) else type(exc).__name__})
            finally:
                counters["sources_done"] += 1
                await report("download", "Đã kiểm tra: " + dataset_id)

    tasks = [asyncio.create_task(fetch(candidate)) for candidate in ranked]
    await bounded(tasks, deadline-loop.time())
    exhausted = timed_out or loop.time() >= deadline-.05
    await report("finished", "Hoàn tất thu thập" if not exhausted else "Đạt giới hạn thời gian; giữ các nguồn đã tải")
    search_coverage = [{"requirement": need.key, "concept": need.concept,
                        "required": need.required,
                        "candidate_ids": sorted(candidate["dataset_id"] for candidate in candidates.values()
                                                 if need.key in candidate.get("requirements", [])),
                        "candidate_count": sum(need.key in candidate.get("requirements", [])
                                               for candidate in candidates.values()),
                        "search_rounds": list(dict.fromkeys(item["wave"] for item in searches
                                                             if item["requirement"] == need.key)),
                        "searches": [item["search"] for item in searches if item["requirement"] == need.key]}
                       for need in needs]
    return {"sources": sources, "searches": searches, "search_coverage": search_coverage,
            "rejected_previous_sources": rejected_previous_sources, "errors": errors,
            "search_guidance": {"round": guidance.get("round", 0), "targeted_need_keys": sorted(targeted_keys),
                                "supplemental_queries": supplemental_queries,
                                "excluded_previous_dataset_ids": excluded_ids},
            "elapsed_seconds": round(loop.time()-started, 2), "time_budget_seconds": budget,
            "time_limit_reached": exhausted,
            "note": "Snapshot có thể là mẫu giới hạn; cần đối chiếu phạm vi và biến trước khi phân tích."}


def prepare_data(run_id, brief, design, collection, max_rows):
    """Execute only declared transformations; never execute LLM-generated code."""
    brief = ResearchBrief.model_validate(brief)
    design = AnalysisDesign.model_validate(design)
    if not design.adequate or design.missing_requirements or design.method == "unsupported":
        raise ValueError("Thiết kế chưa khả thi: " + "; ".join(design.missing_requirements))
    if not design.sources:
        raise ValueError("Chưa chọn nguồn dữ liệu.")
    known = {s["source_key"]: s for s in collection["sources"]}
    needs = {n.key: n for n in brief.data_needs}
    frames, provenance = {}, []
    for selection in design.sources:
        key = selection.source_key
        if key not in known or key in frames:
            raise ValueError("Nguồn được chọn không tồn tại hoặc bị lặp.")
        source = known[key]
        raw = load_snapshot(run_id, source)
        original_rows = len(raw)
        for rule in selection.filters:
            if rule.column not in raw:
                raise ValueError("Cột lọc mẫu không tồn tại trong nguồn.")
            values = raw[rule.column]
            if rule.operator == "eq" and len(rule.values) == 1:
                mask = values.eq(rule.values[0])
            elif rule.operator == "in":
                mask = values.isin(rule.values)
            elif rule.operator == "between" and len(rule.values) == 2:
                mask = values.between(rule.values[0], rule.values[1])
            else:
                raise ValueError("Số giá trị trong điều kiện lọc mẫu không hợp lệ.")
            raw = raw.loc[mask.fillna(False)].copy()
        if raw.empty:
            raise ValueError("Không có bản ghi đáp ứng điều kiện lọc địa bàn/thời gian.")
        converted = pd.DataFrame(index=raw.index)
        for binding in selection.bindings:
            if binding.variable not in needs or binding.column not in raw:
                raise ValueError("Ánh xạ biến không thuộc kế hoạch hoặc cột không tồn tại.")
            if binding.variable in converted:
                raise ValueError("Ánh xạ trùng biến trong một nguồn.")
            if binding.target_unit != needs[binding.variable].unit:
                raise ValueError("Đơn vị đích khác yêu cầu đã duyệt.")
            values = raw[binding.column].copy()
            if binding.transform == "numeric":
                numeric = pd.to_numeric(values, errors="coerce")
                if (values.notna() & numeric.isna()).any():
                    raise ValueError("Chuyển kiểu số làm mất dữ liệu; cần điều chỉnh ánh xạ.")
                values = numeric
            elif binding.transform == "year":
                if pd.api.types.is_numeric_dtype(values):
                    raise ValueError("Cột năm dạng số không được giải mã như timestamp.")
                dates = pd.to_datetime(values, errors="coerce")
                if (values.notna() & dates.isna()).any():
                    raise ValueError("Không chuyển được thời gian sang năm.")
                values = dates.dt.year
            elif binding.transform in ("strip", "casefold"):
                values = values.astype("string").str.strip()
                if binding.transform == "casefold":
                    values = values.str.casefold()
            if binding.scale != 1 or binding.offset != 0:
                if not pd.api.types.is_numeric_dtype(values):
                    raise ValueError("Chỉ đổi đơn vị trên cột số.")
                values = values * binding.scale + binding.offset
            elif binding.source_unit != binding.target_unit and binding.transform != "year":
                raise ValueError("Đổi đơn vị phải khai báo hệ số chuyển đổi.")
            converted[binding.variable] = values
        frames[key] = converted
        provenance.append({"source_key": key, "dataset_id": source["candidate"]["dataset_id"],
            "dataset_url": source["candidate"]["dataset_url"], "license": source["candidate"].get("license"),
            "snapshot_sha256": source["sha256"], "rows": len(raw), "rows_before_filter": original_rows,
            "filters": [rule.model_dump() for rule in selection.filters],
            "sampled": source["metadata"].get("sampled", False),
            "bindings": [b.model_dump() for b in selection.bindings]})
    base = design.sources[0].source_key
    frame = frames[base]
    used, joins = {base}, []
    for join in design.joins:
        if join.right_source not in frames or join.right_source in used:
            raise ValueError("Thứ tự kết hợp nguồn không hợp lệ.")
        right = frames[join.right_source]
        if any(k not in frame or k not in right for k in join.keys):
            raise ValueError("Không có khóa ghép chung trong hai nguồn.")
        if frame[join.keys].isna().any().any() or right[join.keys].isna().any().any():
            raise ValueError("Khóa ghép chứa giá trị thiếu.")
        overlap = (set(frame) & set(right)) - set(join.keys)
        if overlap:
            raise ValueError("Hai nguồn cùng cung cấp biến ngoài khóa ghép: " + ", ".join(sorted(overlap)))
        before = len(frame)
        frame = frame.merge(right, on=join.keys, how="left", validate=join.cardinality, indicator=True)
        matched = int((frame["_merge"] == "both").sum())
        if not matched:
            raise ValueError("Không có bản ghi khớp giữa hai nguồn.")
        frame = frame.drop(columns="_merge")
        joins.append({"right_source": join.right_source, "keys": join.keys, "left_rows": before,
                      "matched_rows": matched, "unmatched_rows": before-matched, "output_rows": len(frame)})
        used.add(join.right_source)
    if used != set(frames):
        raise ValueError("Có nguồn được chọn nhưng chưa khai báo cách kết hợp.")
    if len(frame) > max_rows:
        raise ValueError("Dữ liệu sau kết hợp vượt giới hạn dòng đã cấu hình.")
    required = {n.key for n in brief.data_needs if n.required}
    if required - set(frame):
        raise ValueError("Thiếu biến bắt buộc: " + ", ".join(sorted(required-set(frame))))
    year_need = next((n for n in brief.data_needs if n.role == "join_key" and n.key == "year"), None)
    requested_years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", brief.time_scope)
    if year_need and year_need.key in frame and len(requested_years) >= 2:
        expected_start, expected_end = int(requested_years[0]), int(requested_years[1])
        observed_years = pd.to_numeric(frame[year_need.key], errors="coerce").dropna()
        if observed_years.empty:
            raise ValueError("Không tìm thấy năm quan sát dạng số sau khi ghép và lọc nguồn.")
        actual_start, actual_end = int(observed_years.min()), int(observed_years.max())
        if actual_start > expected_start or actual_end < expected_end:
            raise ValueError(
                f"Độ phủ thời gian không đủ: dữ liệu có {actual_start}–{actual_end}, "
                f"kế hoạch yêu cầu {expected_start}–{expected_end}."
            )
    if design.target not in frame or needs[design.target].role != "outcome":
        raise ValueError("Biến kết quả chưa đúng yêu cầu nghiên cứu.")
    if not pd.api.types.is_numeric_dtype(frame[design.target]):
        raise ValueError("Hiện chỉ hỗ trợ biến kết quả dạng số.")
    if len(set(design.predictors)) != len(design.predictors):
        raise ValueError("Biến giải thích bị lặp.")
    for predictor in design.predictors:
        if predictor not in frame or predictor == design.target or needs[predictor].role == "join_key":
            raise ValueError("Biến giải thích không hợp lệ.")
        if not pd.api.types.is_numeric_dtype(frame[predictor]):
            raise ValueError("Biến giải thích phải là cột số.")
    if design.method in ("ols", "correlation") and not design.predictors:
        raise ValueError("Phương pháp cần có biến giải thích.")
    subset = [design.target] + design.predictors
    complete = frame[subset].replace([float("inf"), -float("inf")], float("nan")).dropna()
    if len(complete) < max(3, len(design.predictors)+3 if design.method == "ols" else 3):
        raise ValueError("Không đủ bản ghi hợp lệ cho phương pháp đề xuất.")
    if design.method in ("ols", "correlation") and any(complete[c].nunique() < 2 for c in subset):
        raise ValueError("Biến phân tích không có biến thiên.")
    if design.method == "ols":
        import numpy as np
        matrix = np.column_stack([np.ones(len(complete)), complete[design.predictors].to_numpy(dtype=float)])
        if np.linalg.matrix_rank(matrix) < matrix.shape[1]:
            raise ValueError("Các biến giải thích phụ thuộc tuyến tính; cần điều chỉnh mô hình.")
    return frame, {"ok": True, "rows": len(frame), "complete_analysis_rows": len(complete),
                   "columns": list(frame), "joins": joins, "provenance": provenance,
                   "warnings": ["Chỉ phân tích liên hệ quan sát; chưa có thiết kế nhận diện tác động nhân quả."]}


def design_context(collection):
    """Bound prompt size while retaining observed schema and source provenance."""
    profiles = []
    for source in collection["sources"]:
        candidate = source["candidate"]
        profiles.append({"source_key": source["source_key"], "rows": source["rows"],
            "columns": source["columns"], "sample": source["sample"][:3],
            "sampled": source["metadata"].get("sampled", False),
            "candidate": {"dataset_id": candidate["dataset_id"], "title": candidate["title"],
                          "description": candidate.get("description", "")[:2500],
                          "license": candidate.get("license"),
                          "requirements": candidate.get("requirements", []),
                          "scope_match": candidate.get("scope_match"),
                          "scope_verification_required": candidate.get("scope_verification_required", False),
                          "relevance_note": candidate.get("relevance_note", ""),
                          "matched_queries": candidate.get("matched_queries", [])[:8]},
            "profile_note": source.get("profile_note", "")})
    return json.dumps(profiles, ensure_ascii=False, default=str)
