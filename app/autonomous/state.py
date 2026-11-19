"""Pure state transitions; approvals are tied to the exact stored version."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

from fastapi import HTTPException

STAGES = ("literature_search", "literature_read", "literature_synthesis", "research_plan",
          "data_collection", "design", "analysis", "introduction", "related_work",
          "discussion", "manuscript", "verification")
GATES = {"research_plan": "awaiting_research_approval", "design": "awaiting_design_approval",
         "verification": "awaiting_final_approval"}

REVIEW_STAGES = ("literature_search", "literature_read", "literature_synthesis",
                 "research_plan", "manuscript", "verification")


def is_review(run):
    return run["artifacts"].get("settings", {}).get("paper_type") == "review"


def stages_for(run):
    return REVIEW_STAGES if is_review(run) else STAGES


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def event(run, message, stage=None):
    run.setdefault("events", []).append({"at": datetime.now(timezone.utc).isoformat(),
                                         "stage": stage or run["stage"], "message": message})


def check_version(run, version):
    if run["version"] != version:
        raise HTTPException(409, "Tiến trình đã thay đổi. Tải lại trước khi quyết định.")


def decide(run, request):
    run = deepcopy(run)
    check_version(run, request.expected_version)
    status = run["status"]
    if status not in GATES.values():
        raise HTTPException(409, "Tiến trình không ở bước chờ phê duyệt.")
    artifacts = run["artifacts"]
    if request.action == "approve":
        if status == "awaiting_design_approval" and not artifacts.get("design_validation", {}).get("ok"):
            raise HTTPException(409, "Thiết kế chưa đáp ứng yêu cầu dữ liệu/phương pháp; cần điều chỉnh.")
        if status == "awaiting_final_approval" and not artifacts.get("verification", {}).get("ok"):
            raise HTTPException(409, "Còn lỗi kiểm chứng. Cần sửa bản thảo trước khi duyệt.")
        artifacts.setdefault("approvals", []).append({"stage": run["stage"], "version": run["version"],
            "revision": run["revision"], "notes": request.notes,
            "fingerprint": fingerprint(artifacts[run["stage"]])})
        if status == "awaiting_final_approval":
            run["status"] = "completed"
            if artifacts.get("paper_state"):
                artifacts["paper_state"]["stage"] = "complete"
                artifacts["paper_state"]["blockers"] = []
        else:
            stages = stages_for(run)
            run["stage"] = stages[stages.index(run["stage"]) + 1]
            run["status"] = "queued"
        event(run, "Người dùng đã phê duyệt: " + request.notes)
    else:
        if not request.notes.strip():
            raise HTTPException(422, "Hãy nêu yêu cầu điều chỉnh.")
        run["revision"] += 1
        if is_review(run):
            restart = request.restart_from or ("research_plan" if status == "awaiting_research_approval" else "manuscript")
            allowed_review = {"research_plan"} if status == "awaiting_research_approval" else {"research_plan", "manuscript", "verification"}
            if restart not in allowed_review:
                raise HTTPException(409, "Bài tổng quan chỉ quay lại kế hoạch hoặc bản thảo.")
            keep = {"settings", "topic", "literature_search", "literature_read", "literature_synthesis", "approvals", "review_corpus"}
            if restart in ("manuscript", "verification"):
                keep.add("research_plan")
            if restart == "verification":
                keep |= {"review_document", "manuscript"}
            run["artifacts"] = {k: v for k, v in artifacts.items() if k in keep}
            run["artifacts"]["feedback"] = request.notes
            run.update(stage=restart, status="queued", error=None)
            event(run, "Yêu cầu sửa bài tổng quan: " + request.notes)
            return run
        # A research revision invalidates discovery, design, analysis and writing.
        # A design revision rechecks data; final revision only rewrites the draft.
        restart = {"awaiting_research_approval": "research_plan",
                   "awaiting_design_approval": "data_collection",
                   "awaiting_final_approval": "introduction"}[status]
        allowed = {"awaiting_research_approval": {"research_plan"},
                   "awaiting_design_approval": {"research_plan", "data_collection", "design"},
                   "awaiting_final_approval": {"research_plan", "data_collection", "design", "introduction",
                                               "manuscript", "verification"}}
        if request.restart_from:
            if request.restart_from not in allowed[status]:
                raise HTTPException(409, "Không thể bắt đầu lại từ bước đã chọn.")
            restart = request.restart_from
        search_guidance = None
        if status == "awaiting_design_approval" and restart == "data_collection":
            brief = artifacts.get("research_plan") or {}
            previous_design = artifacts.get("design") or {}
            previous_validation = artifacts.get("design_validation") or {}
            previous_collection = artifacts.get("data_collection") or {}
            available = {source.get("source_key"): {column.get("name") for column in source.get("columns", [])}
                         for source in previous_collection.get("sources", [])}
            mapped = {binding.get("variable") for source in previous_design.get("sources", [])
                      if source.get("source_key") in available
                      for binding in source.get("bindings", [])
                      if binding.get("column") in available[source.get("source_key")]}
            # A blocked design stores its raw mappings separately as candidates.
            # Prefer deterministic coverage from the source/citation gate so a
            # useful climate source is not searched again when the missing item
            # is actually the agricultural outcome or merge key.
            mapped.update(row.get("key") for row in previous_validation.get("coverage", [])
                          if row.get("sources"))
            missing = [need.get("key") for need in brief.get("data_needs", [])
                       if need.get("required", True) and need.get("role") != "join_key"
                       and need.get("key") not in mapped]
            if not missing and previous_design.get("method") == "unsupported":
                missing = [need.get("key") for need in brief.get("data_needs", [])
                           if need.get("required", True) and need.get("role") != "join_key"]
            search_guidance = {
                "missing_need_keys": missing,
                "previous_missing_requirements": previous_design.get("missing_requirements", []),
                "exclude_dataset_ids": sorted({
                    source.get("candidate", {}).get("dataset_id")
                    for source in previous_collection.get("sources", [])
                    if source.get("candidate", {}).get("dataset_id")
                }),
                "user_notes": request.notes.strip(),
                "round": int((artifacts.get("data_search_guidance") or {}).get("round", 0)) + 1,
            }
        # Files uploaded by the team are user inputs, not derived artifacts: a
        # revision re-validates them in the design stage but never discards them.
        keep = {"settings", "topic", "literature_search", "literature_read", "literature_synthesis", "approvals",
                "user_sources", "data_preferences"}
        if restart != "research_plan":
            keep |= {"research_plan", "data_feasibility"}
        elif request.keep_sources and status == "awaiting_design_approval":
            # A data-grounded alternative question reuses the sources it was built from.
            keep.add("data_collection")
        if restart == "data_collection":
            keep.add("data_collection")
        if search_guidance is not None:
            keep.add("data_search_guidance")
        if restart in ("design", "introduction", "manuscript", "verification"):
            keep |= {"data_collection"}
        if restart in ("introduction", "manuscript", "verification"):
            keep |= {"data_collection", "design", "design_validation", "analysis", "research_question_id"}
        # Cheaper final-gate revisions: re-assemble the draft from the written
        # sections, or only re-run verification after a checker fix.
        if restart in ("manuscript", "verification"):
            keep |= {"introduction", "related_work", "discussion", "paper_story", "paper_state"}
        if restart == "verification":
            keep |= {"manuscript", "claim_evidence_map"}
        artifacts = {k: v for k, v in artifacts.items() if k in keep}
        if search_guidance is not None:
            artifacts["data_search_guidance"] = search_guidance
        artifacts["feedback"] = request.notes
        run.update(artifacts=artifacts, stage=restart, status="queued", error=None)
        event(run, "Yêu cầu điều chỉnh: " + request.notes)
    return run


def finish_stage(run, output):
    run = deepcopy(run)
    stage = run["stage"]
    run["artifacts"][stage] = output
    run["error"] = None
    if stage in GATES:
        run["status"] = GATES[stage]
    else:
        stages = stages_for(run)
        run["stage"] = stages[stages.index(stage) + 1]
        run["status"] = "queued"
    event(run, "Hoàn tất " + stage, stage=stage)
    return run
