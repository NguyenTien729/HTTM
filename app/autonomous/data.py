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
from app.data_research import worldbank
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


def need_search_context(need, brief, refinement=(), excluded_ids=()):
    """Discovery context for one variable (shared by collection and the feasibility probe)."""
    join_context = " ".join(dict.fromkeys(
        [term for item in brief.data_needs if item.role == "join_key"
         for term in [item.concept, *item.search_terms]]))
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
        context["_exclude_dataset_ids"] = list(excluded_ids)
    if brief.geography_terms:
        context["_search_scope_terms"] = brief.geography_terms
    return context


async def probe_feasibility(brief, user_sources=(), seconds=75.0):
    """Quick metadata-only search per variable, shown at the research-approval gate.

    It answers "is there any candidate source for each variable?" before the user
    approves the question. Candidates are not downloaded or validated here; the
    collection and design stages still check real columns, scope and units.
    """
    brief = ResearchBrief.model_validate(brief) if isinstance(brief, dict) else brief
    needs = [need for need in brief.data_needs if need.role != "join_key"]
    loop = asyncio.get_running_loop()
    started, errors = loop.time(), []
    per_need = max(8.0, min(25.0, seconds * 2 / max(1, len(needs))))
    slots = asyncio.Semaphore(2)

    async def one(need):
        async with slots:
            stable = await worldbank_candidates(need, brief, [], errors)
            try:
                hub = await asyncio.wait_for(discover_gap_datasets(
                    need_search_context(need, brief), limit=3, max_search_seconds=per_need), timeout=per_need + 2)
            except (DataResearchError, asyncio.TimeoutError) as exc:
                errors.append({"requirement": need.key, "error": getattr(exc, "message", "Search timeout")})
                hub = {"datasets": []}
            found = []
            for candidate in [*stable, *hub.get("datasets", [])]:
                if candidate.get("dataset_id") in {c["dataset_id"] for c in found}:
                    continue
                found.append({key: candidate.get(key) for key in (
                    "dataset_id", "title", "source", "dataset_url", "scope_match", "relevance_note")})
            return need, found

    tasks = [asyncio.create_task(one(need)) for need in needs]
    done, pending = await asyncio.wait(tasks, timeout=max(1.0, seconds)) if tasks else (set(), set())
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    results = {need.key: found for need, found in (task.result() for task in done if not task.exception())}
    rows = [{"key": need.key, "concept": need.concept, "role": need.role, "unit": need.unit,
             "resolution": need.resolution, "required": need.required,
             "searched": need.key in results, "candidates": results.get(need.key, [])[:6],
             "candidate_count": len(results.get(need.key, []))} for need in needs]
    required = [row for row in rows if row["required"]]
    missing = [row["concept"] for row in required if not row["candidate_count"]]
    verdict = "likely" if not missing else "team_data" if user_sources else "at_risk"
    return {"schema_version": "httm/data-feasibility-v1", "verdict": verdict, "needs": rows,
            "required_total": len(required), "required_with_candidates": len(required) - len(missing),
            "missing_required": missing, "user_sources": len(list(user_sources)),
            "errors": errors[:20], "time_limit_reached": bool(pending),
            "elapsed_seconds": round(loop.time() - started, 1),
            "note": "Ứng viên chỉ dựa trên metadata và chưa được tải về; cột, địa bàn, thời gian và đơn vị "
                    "sẽ được kiểm tra thật ở bước thu thập và thiết kế. Không tìm thấy ứng viên không chứng minh "
                    "rằng dữ liệu không tồn tại."}


def need_phrases(need, refinement=()):
    """English search phrases for one variable, most specific first."""
    return list(dict.fromkeys(p.strip() for p in [*need.search_terms, need.concept, *refinement] if p and p.strip()))


_SUBNATIONAL = ("cá nhân", "ca nhan", "individual", "person", "respondent", "student", "sinh viên", "người",
                "hộ", "household", "firm", "doanh nghiệp", "company", "tỉnh", "province", "district", "huyện",
                "xã", "commune", "city", "thành phố", "county", "state-level", "trạm", "station", "school", "trường")


def worldbank_resolution(resolution):
    """World Bank WDI is country × year only: skip variables measured below country level."""
    text = str(resolution or "").casefold()
    return not any(marker in text for marker in _SUBNATIONAL)


async def worldbank_candidates(need, brief, refinement, errors, excluded_ids=()):
    """World Bank indicators for one variable; failures are logged, never fatal."""
    if need.role == "join_key" or not worldbank_resolution(need.resolution):
        return []
    try:
        found = await asyncio.wait_for(worldbank.search_indicators(
            need_phrases(need, refinement), brief.geography, limit=3), timeout=40)
    except (worldbank.WorldBankError, asyncio.TimeoutError) as exc:
        errors.append({"requirement": need.key, "source": "World Bank",
                       "error": str(exc) or "World Bank timeout"})
        return []
    return [candidate for candidate in found if candidate["dataset_id"] not in set(excluded_ids)]


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
    # Files uploaded by the research team are always carried forward from the
    # run's own list (the single source of truth), never re-screened by scope.
    user_sources = [dict(source) for source in run["artifacts"].get("user_sources", [])]
    public_search = (run["artifacts"].get("data_preferences") or {}).get("public_search", True)
    sources, rejected_previous_sources = list(user_sources), []
    for source in previous_collection.get("sources", []):
        source = dict(source)
        candidate = source.get("candidate", {})
        if candidate.get("user_provided"):
            continue
        conflict = _explicit_geography_conflict(candidate, target_scope)
        if conflict:
            rejected_previous_sources.append({"dataset_id": candidate.get("dataset_id"),
                                              "title": candidate.get("title", ""),
                                              "reason": conflict})
        else:
            sources.append(source)
    if user_sources and public_search is False:
        return {"sources": sources, "searches": [], "search_coverage": [],
                "rejected_previous_sources": [], "errors": [], "user_sources_only": True,
                "search_guidance": {"round": 0, "targeted_need_keys": [], "supplemental_queries": {},
                                    "excluded_previous_dataset_ids": []},
                "elapsed_seconds": 0.0, "time_budget_seconds": 0.0, "time_limit_reached": False,
                "note": "Chỉ dùng dữ liệu do nhóm nghiên cứu cung cấp; bỏ qua tìm kiếm dữ liệu công khai theo yêu cầu."}
    previous_candidates = {source.get("candidate", {}).get("dataset_id"): source.get("candidate", {})
                           for source in sources if source.get("candidate", {}).get("dataset_id")
                           and not source.get("candidate", {}).get("user_provided")}
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
    async def search(need, refinement=None, search_seconds=24, wave="initial"):
        async with search_slots:
            refinement = refinement or []
            # Screen each dataset for the variable currently being sought. A
            # climate source must not be rejected merely because it lacks the
            # separate agricultural outcome that will be joined later.
            context = need_search_context(need, brief, refinement, excluded_ids)
            try:
                try:
                    result = await asyncio.wait_for(discover_gap_datasets(
                        context, limit=5 if wave == "supplemental" else 4,
                        max_search_seconds=search_seconds), timeout=search_seconds + 2)
                except (DataResearchError, asyncio.TimeoutError) as exc:
                    errors.append({"requirement": need.key, "error": getattr(exc, "message", "Search timeout")})
                    result = {"search": {}, "datasets": []}
                # World Bank: a stable country × year source searched for every variable.
                stable = await worldbank_candidates(need, brief, refinement, errors, excluded_ids)
                result = dict(result, datasets=list(result.get("datasets", [])) + stable)
                result["search"] = dict(result.get("search") or {}, world_bank_candidates=len(stable))
                return need, result
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
    attempted_ids = set(known_dataset_ids)
    required_keys = [need.key for need in needs if need.required]

    def covered_keys():
        return {key for source in sources if not source.get("candidate", {}).get("user_provided")
                for key in source.get("candidate", {}).get("requirements", [])}

    def uncovered_required():
        covered = covered_keys()
        return [key for key in required_keys if key not in covered]

    def rank(pool):
        ranked_all = sorted((candidate for candidate in pool if candidate.get("dataset_id") not in attempted_ids),
                            key=lambda c: (c.get("scope_match") != "mentioned_in_metadata",
                           -len(c.get("requirements", [])),
                           -sum(n.key in c.get("requirements", []) for n in needs if n.role == "outcome"),
                           c.get("dataset_id", "")))
        # Give each still-missing required variable a source before using the
        # remaining slots for duplicate/overlapping candidates.
        ranked, seen_ids, missing = [], set(), set(uncovered_required())
        priority_needs = sorted((need for need in needs if need.required and need.key in missing),
                                key=lambda n: (n.role != "outcome", n.role == "control", n.key))
        for need in priority_needs:
            for candidate in [c for c in ranked_all if need.key in c.get("requirements", [])
                              and c["dataset_id"] not in seen_ids][:2]:
                ranked.append(candidate)
                seen_ids.add(candidate["dataset_id"])
        ranked.extend(candidate for candidate in ranked_all if candidate["dataset_id"] not in seen_ids)
        # Keep alternates so one failed file does not consume the only chance for a variable.
        return ranked[:settings["max_sources"]*4]

    download_slots = asyncio.Semaphore(2)
    existing_slots = [int(source.get("source_key", "s00")[1:]) for source in sources
                      if str(source.get("source_key", "")).startswith("s")
                      and str(source.get("source_key", ""))[1:].isdigit()]
    next_slot = max(existing_slots, default=0)
    skipped = []

    def public_count():
        return len(sources) - len(user_sources)

    async def fetch(candidate):
        nonlocal next_slot
        async with download_slots:
            if public_count() >= settings["max_sources"] or loop.time() >= deadline:
                return
            dataset_id = candidate["dataset_id"]
            missing = uncovered_required()
            # Reserve the last slots for required variables that still have no source.
            if missing and not set(candidate.get("requirements", [])) & set(missing) \
                    and settings["max_sources"] - public_count() <= len(missing):
                skipped.append(dataset_id)
                return
            attempted_ids.add(dataset_id)
            await report("download", "Đang kiểm tra và tải: " + dataset_id)
            try:
                async def read():
                    if dataset_id.startswith(worldbank.DATASET_PREFIX):
                        frame, meta = await worldbank.download_indicator(dataset_id, brief.time_scope, settings["max_rows"])
                        return frame, meta, {"config": meta["config"], "split": meta["split"]}
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
                if public_count() >= settings["max_sources"]:
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
                    if isinstance(exc, (DataResearchError, ValueError, worldbank.WorldBankError)) else type(exc).__name__})
            finally:
                counters["sources_done"] += 1
                await report("download", "Đã kiểm tra: " + dataset_id)

    async def fetch_all(pool, seconds):
        ranked = rank(pool)
        counters["sources_total"] += len(ranked)
        # Sequential priority order matters for coverage, so start them in order.
        tasks = [asyncio.create_task(fetch(candidate)) for candidate in ranked]
        await bounded(tasks, seconds)

    counters["sources_total"] = len(sources)
    await fetch_all(list(candidates.values()), deadline-loop.time()-(20. if required_keys else 0.))

    # Go back and search again while a required variable has no downloaded source,
    # instead of handing the design step a collection that cannot answer the question.
    refill_rounds = []
    for round_number in (1, 2):
        missing = uncovered_required()
        if not missing or public_count() >= settings["max_sources"] or deadline-loop.time() < 20:
            break
        missing_needs = [need for need in needs if need.key in missing]
        await report("search", f"Tìm lại lần {round_number} cho biến còn thiếu: " + ", ".join(n.concept for n in missing_needs))
        excluded_ids.extend(sorted(attempted_ids - set(excluded_ids)))
        refill_guidance = {"missing_need_keys": missing,
                           "previous_missing_requirements": guidance.get("previous_missing_requirements", []),
                           "user_notes": guidance.get("user_notes", "")}
        queries = await _refine_missing_queries(brief, refill_guidance, min(15., max(0., deadline-loop.time()-15.)*.2))
        counters["searches_total"] += len(missing_needs)
        before = set(candidates)
        tasks = [asyncio.create_task(search(need, queries.get(need.key, []),
                                            min(30, max(8, (deadline-loop.time()-12.)/max(1, len(missing_needs)))),
                                            "refill"))
                 for need in missing_needs]
        merge_results(await bounded(tasks, max(0., (deadline-loop.time())*.5)), "refill")
        fresh = [candidate for dataset_id, candidate in candidates.items()
                 if dataset_id not in attempted_ids and set(candidate.get("requirements", [])) & set(missing)]
        refill_rounds.append({"round": round_number, "missing_need_keys": missing,
                              "new_candidates": len(set(candidates) - before), "tried": len(fresh)})
        if not fresh:
            continue
        await fetch_all(fresh, deadline-loop.time())
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
            "refill_rounds": refill_rounds, "uncovered_required": uncovered_required(),
            "search_guidance": {"round": guidance.get("round", 0), "targeted_need_keys": sorted(targeted_keys),
                                "supplemental_queries": supplemental_queries,
                                "excluded_previous_dataset_ids": excluded_ids},
            "elapsed_seconds": round(loop.time()-started, 2), "time_budget_seconds": budget,
            "time_limit_reached": exhausted,
            "note": "Snapshot có thể là mẫu giới hạn; cần đối chiếu phạm vi và biến trước khi phân tích."}


def prepare_data(run_id, brief, design, collection, max_rows, drop_optional_predictors=False):
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
        cleaning = (source.get("metadata") or {}).get("cleaning") or {}
        ambiguous = {item.get("column") for item in cleaning.get("ambiguous_numeric_columns", [])}
        for binding in selection.bindings:
            if binding.transform == "numeric" and binding.column in ambiguous:
                raise ValueError(f"Cột '{binding.column}' có định dạng số mơ hồ (ví dụ 1.234 có thể là 1,234 hoặc 1234). "
                                 "Hãy tải lại file và chọn định dạng số trước khi phân tích.")
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
            "origin": source["metadata"].get("origin", "public_repository"),
            "filename": source["metadata"].get("filename"),
            "raw_sha256": source["metadata"].get("raw_sha256"),
            "cleaning_summary": source["metadata"].get("cleaning_summary"),
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
    # Join keys only matter when sources are joined: a single source needs none.
    required = {n.key for n in brief.data_needs if n.required
                and not (n.role == "join_key" and len(design.sources) == 1)}
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
    predictors, dropped = [], []
    for predictor in design.predictors:
        problem = None
        if predictor not in frame or predictor == design.target or predictor not in needs \
                or needs[predictor].role == "join_key":
            problem = f"Biến giải thích '{predictor}' không hợp lệ hoặc chưa được ánh xạ tới cột."
        elif not pd.api.types.is_numeric_dtype(frame[predictor]):
            problem = f"Biến giải thích '{predictor}' phải là cột số."
        if problem:
            # An optional covariate (control / secondary exposure) that is text or
            # unmapped is removed and reported, instead of blocking the design.
            if drop_optional_predictors and predictor in needs and not needs[predictor].required:
                dropped.append({"variable": predictor, "reason": problem})
                continue
            raise ValueError(problem)
        predictors.append(predictor)
    if design.method in ("ols", "correlation") and not predictors:
        raise ValueError("Phương pháp cần có biến giải thích dạng số.")
    subset = [design.target] + predictors
    complete = frame[subset].replace([float("inf"), -float("inf")], float("nan")).dropna()
    if len(complete) < max(3, len(predictors)+3 if design.method == "ols" else 3):
        raise ValueError("Không đủ bản ghi hợp lệ cho phương pháp đề xuất.")
    if design.method in ("ols", "correlation") and any(complete[c].nunique() < 2 for c in subset):
        raise ValueError("Biến phân tích không có biến thiên.")
    if design.method == "ols":
        import numpy as np
        matrix = np.column_stack([np.ones(len(complete)), complete[predictors].to_numpy(dtype=float)])
        if np.linalg.matrix_rank(matrix) < matrix.shape[1]:
            raise ValueError("Các biến giải thích phụ thuộc tuyến tính; cần điều chỉnh mô hình.")
    return frame, {"ok": True, "rows": len(frame), "complete_analysis_rows": len(complete),
                   "predictors": predictors, "dropped_predictors": dropped,
                   "columns": list(frame), "joins": joins, "provenance": provenance,
                   "warnings": ["Chỉ phân tích liên hệ quan sát; chưa có thiết kế nhận diện tác động nhân quả."]}


def design_context(collection):
    """Bound prompt size while retaining observed schema and source provenance."""
    profiles = []
    for source in collection["sources"]:
        candidate = source["candidate"]
        cleaning = source["metadata"].get("cleaning") or {}
        user = {"user_provided": True, "user_description": candidate.get("description", "")[:2500],
                "cleaning_summary": source["metadata"].get("cleaning_summary", ""),
                "ambiguous_numeric_columns": [item.get("column") for item in cleaning.get("ambiguous_numeric_columns", [])],
                "code_like_columns": cleaning.get("code_like_columns", [])} if candidate.get("user_provided") else {}
        profiles.append({"source_key": source["source_key"], "rows": source["rows"], **user,
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
