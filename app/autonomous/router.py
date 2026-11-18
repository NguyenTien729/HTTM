import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from app.db import get_pool
from app.models import ResearchPlanRequest
from app.routers.planner import create_plan
from . import store
from .data import ROOT
from .models import StartRun, Decision, VersionRequest, ProposalRevision, ProposalDraft
from .state import STAGES

router = APIRouter(prefix="/autonomous", tags=["Autonomous Research"])


@router.post("/runs", status_code=201)
async def start(request: StartRun):
    settings = request.model_dump()
    if request.project_id:
        async with get_pool().acquire() as conn:
            project = await conn.fetchrow("SELECT topic,paper_type FROM research_projects WHERE id=$1", request.project_id)
        if not project:
            raise HTTPException(404, "Không tìm thấy project.")
        if project["paper_type"] != request.paper_type:
            raise HTTPException(409, "Loại bài đã chọn phải khớp với project. Tạo nghiên cứu mới nếu muốn đổi loại bài.")
        project_id, topic = request.project_id, project["topic"]
    else:
        result = await create_plan(ResearchPlanRequest(topic=request.topic, region=request.region,
            target_paper_count=request.target_paper_count, paper_type=request.paper_type))
        project_id, topic = result.id, request.topic
    return await store.create(project_id, settings, topic)


@router.get("/runs")
async def list_runs(project_id: int | None = None):
    return await store.recent(project_id)


@router.get("/runs/{run_id}")
async def get_run(run_id: int):
    return await store.get(run_id)


@router.post("/runs/{run_id}/decision")
async def decision(run_id: int, request: Decision):
    return await store.change(run_id, request)


@router.post("/runs/{run_id}/resume")
async def resume(run_id: int, request: VersionRequest):
    return await store.change(run_id, request, "resume")


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: int, request: VersionRequest):
    return await store.change(run_id, request, "cancel")


@router.post("/runs/{run_id}/proposal/continue")
async def continue_proposal(run_id: int, request: ProposalRevision):
    """Expand a blocked proposal without converting planned work into findings."""
    from app.services.llm import generate_structured
    from app.services.vietnamese_writing import VIETNAMESE_WRITING_RULE
    run = await store.get(run_id)
    if run["version"] != request.expected_version:
        raise HTTPException(409, "Run đã thay đổi; hãy tải lại trước khi viết tiếp.")
    draft = run["artifacts"].get("proposal_draft")
    if not draft:
        raise HTTPException(409, "Run này chưa có bản đề xuất để viết tiếp.")
    prior = dict(draft)
    valid_ids = {paper["id"] for paper in prior.get("references", [])}
    result = await generate_structured(
        VIETNAMESE_WRITING_RULE +
        "Continue and revise this Vietnamese research proposal according to the user's instruction. "
        "Return the complete proposal with all required sections, not only a note about what to write next. "
        "When asked to expand, develop the requested sections with substantive explanations and evidence comparisons; do not summarize or shorten them. "
        "Preserve sections not targeted by the user's instruction verbatim unless correcting an unsupported claim. "
        "Preserve the approved topic, question, scope, existing blockers, and evidence boundaries. "
        "This remains a proposal: never add empirical results, completed-study wording, coefficients, or claims that missing data were found. "
        "Cite only the supplied paper IDs in [id] format; do not invent citations. "
        "Keep supported_data empty because the empirical analysis has not run. Do not remove blockers or weaken the disclaimer.\n" +
        json.dumps({"user_instruction": request.instruction, "current_proposal": prior,
                    "valid_paper_ids": sorted(valid_ids)}, ensure_ascii=False, default=str), ProposalDraft)
    updated = result.model_dump(mode="json")
    updated["paper_story"] = prior.get("paper_story", {})
    updated["disclaimer"] = prior.get("disclaimer", "Bản đề xuất nghiên cứu — chưa có kết quả thực nghiệm.")
    updated["blockers"] = list(dict.fromkeys(prior.get("blockers", [])))
    for claim in updated["claim_evidence_map"]["claims"]:
        invalid = set(claim.get("evidence_paper_ids", [])) - valid_ids
        claim["evidence_paper_ids"] = sorted(set(claim.get("evidence_paper_ids", [])) & valid_ids)
        claim["source_keys"] = []
        if claim["status"] == "supported_data":
            claim["status"] = "unsupported"
        if invalid or (claim["status"] == "supported_literature" and not claim["evidence_paper_ids"]):
            claim["status"] = "unsupported"
        if claim["status"] == "unsupported":
            updated["blockers"].append("Có luận điểm trong claim-evidence map cần người dùng kiểm tra nguồn.")
    from app.services.citation_checker import extract_cited_ids
    body = updated["abstract"] + "\n" + "\n".join(updated["sections"].values())
    invalid_citations = set(extract_cited_ids(body)) - valid_ids
    if invalid_citations:
        import re
        pattern = re.compile(r"\[(?:" + "|".join(map(str, sorted(invalid_citations))) + r")\]")
        updated["abstract"] = pattern.sub("[citation cần xác minh]", updated["abstract"])
        for name, text in updated["sections"].items():
            updated["sections"][name] = pattern.sub("[citation cần xác minh]", text)
        updated["blockers"].append("Có citation mới chưa được xác minh; đã đánh dấu trong bản thảo.")
    used_ids = set(extract_cited_ids(updated["abstract"] + "\n" + "\n".join(updated["sections"].values())))
    used_ids.update(pid for claim in updated["claim_evidence_map"]["claims"]
                    for pid in claim.get("evidence_paper_ids", []))
    updated["references"] = [paper for paper in prior.get("references", []) if paper["id"] in used_ids]
    updated["blockers"] = list(dict.fromkeys(updated["blockers"]))
    return await store.save_proposal_revision(run_id, request.expected_version, updated, request.instruction)


@router.get("/stages")
async def stages():
    return {"stages": STAGES, "approval_gates": ["research_plan", "design", "verification"],
            "supported_methods": ["descriptive", "correlation", "ols"],
            "sources": ["Hugging Face", "Mendeley Data / DataCite"]}


def markdown(run):
    draft = run["artifacts"].get("manuscript")
    if not draft:
        raise HTTPException(409, "Chưa có bản thảo.")
    parts = ["# " + draft["title"],
        "Trạng thái: " + ("Đã được người dùng phê duyệt." if run["status"] == "completed" else "Bản thảo chưa phê duyệt."),
        "; ".join(a["name"] + (" — " + a["affiliation"] if a["affiliation"] else "") for a in draft["authors"]),
        "## Abstract", draft["abstract"], "Keywords: " + ", ".join(draft["keywords"])]
    if run["artifacts"].get("paper_story"):
        story = run["artifacts"]["paper_story"]
        parts.extend(["## Paper story", story.get("thesis", ""), story.get("research_gap", ""),
                      "### Claims to avoid", *["- " + value for value in story.get("claims_to_avoid", [])]])
    for name in draft.get("section_order", list(draft["sections"])):
        parts.extend(["## " + name, draft["sections"][name]])
    parts.append("## References")
    for ref in draft["references"]:
        parts.append(f"[{ref['id']}] {'; '.join(ref['authors'])} ({ref.get('year') or 'n.d.'}). "
                     f"{ref['title']}. {ref.get('venue') or ''}. {ref.get('doi') or ''}")
    if run["artifacts"].get("claim_evidence_map"):
        parts.append("## Claim–evidence map")
        for claim in run["artifacts"]["claim_evidence_map"].get("claims", []):
            refs = ", ".join(map(str, claim.get("evidence_paper_ids", []))) or "none"
            sources = ", ".join(claim.get("source_keys", [])) or "none"
            parts.append(f"- **{claim['claim_id']} · {claim['status']}** ({claim['section']}): {claim['claim']} — papers: {refs}; data sources: {sources}. {claim['rationale']}")
    return "\n\n".join(parts)


def proposal_markdown(run):
    draft = run["artifacts"].get("proposal_draft")
    if not draft:
        raise HTTPException(409, "Chưa có bản đề xuất nghiên cứu.")
    story = draft.get("paper_story", {})
    parts = ["# " + draft["title"], "> " + draft.get("disclaimer", "Bản đề xuất, chưa có kết quả thực nghiệm."),
             "## Abstract", draft["abstract"], "Keywords: " + ", ".join(draft["keywords"]),
             "## Paper story", story.get("thesis", ""), story.get("research_gap", "")]
    for name, text in draft["sections"].items():
        parts.extend(["## " + name, text])
    parts.append("## Blockers")
    parts.extend("- " + blocker for blocker in draft.get("blockers", []))
    parts.append("## Claim–evidence map")
    for claim in draft.get("claim_evidence_map", {}).get("claims", []):
        refs = ", ".join(map(str, claim.get("evidence_paper_ids", []))) or "chưa có"
        parts.append(f"- **{claim['claim_id']} · {claim['status']}** ({claim['section']}): {claim['claim']} — bằng chứng: {refs}. {claim['rationale']}")
    parts.append("## References")
    for ref in draft.get("references", []):
        authors = ref.get("authors") or []
        if isinstance(authors, str):
            authors = json.loads(authors)
        parts.append(f"[{ref['id']}] {'; '.join(authors)} ({ref.get('year') or 'n.d.'}). {ref['title']}. {ref.get('venue') or ''}. {ref.get('doi') or ''}")
    return "\n\n".join(part for part in parts if part is not None)


@router.get("/runs/{run_id}/proposal.md")
async def export_proposal_markdown(run_id: int):
    run = await store.get(run_id)
    return Response(proposal_markdown(run), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="research_proposal_{run_id}.md"'})


@router.get("/runs/{run_id}/manuscript.md")
async def export_markdown(run_id: int):
    return Response(markdown(await store.get(run_id)), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="research_run_{run_id}.md"'})


@router.get("/runs/{run_id}/export.pdf")
async def export_pdf(run_id: int):
    from app.routers.export import _build_paper_pdf
    run = await store.get(run_id)
    if run["status"] != "completed" or not run["artifacts"].get("verification", {}).get("ok"):
        raise HTTPException(409, "Bản thảo cần qua kiểm chứng và được duyệt trước khi xuất PDF cuối.")
    a = run["artifacts"]
    draft = a["manuscript"]
    if a.get("settings", {}).get("paper_type") == "review":
        return await export_review_file(run, "pdf")
    # Build from the approved immutable run snapshot, not latest mutable writer rows.
    data = {"project_topic": a["topic"], "article_title": draft["title"],
        "article_abstract": draft["abstract"], "article_authors": draft["authors"], "keywords": draft["keywords"],
        "rq": {"rq_text": a["research_plan"]["question"], "is_quantitative": True,
               "hypotheses": a["research_plan"]["hypotheses"],
               "variables": a["research_plan"]["variables"]},
        "gap": {"description": a["research_plan"]["gap"]},
        "introduction": a["introduction"]["introduction"], "related_work": a["related_work"]["related_work"],
        "discussion": a["discussion"]["discussion"] | {"analysis_snapshot": a["analysis"]},
        "references": draft["references"]}
    path = ROOT / str(run_id) / f"approved_v{run['version']}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_build_paper_pdf, data, path)
    return FileResponse(path, media_type="application/pdf", filename=f"research_run_{run_id}.pdf")


async def export_review_file(run, format):
    from app.services.review_export import build_review_pdf, build_review_docx
    from app.routers.export import PaperExportOptions
    if run["status"] != "completed" or not run["artifacts"].get("verification", {}).get("ok"):
        raise HTTPException(409, "Cần kiểm chứng và duyệt bản cuối trước khi xuất.")
    a = run["artifacts"]
    if a.get("settings", {}).get("paper_type") != "review":
        raise HTTPException(409, "Định dạng này dành cho bài tổng quan.")
    path = ROOT / str(run["id"]) / f"review_v{run['version']}.{format}"
    path.parent.mkdir(parents=True, exist_ok=True)
    options = PaperExportOptions(authors=a["settings"].get("authors", []))
    await asyncio.to_thread(build_review_pdf if format == "pdf" else build_review_docx,
                            a["review_document"], path, options)
    return FileResponse(path, filename=f"review_{run['id']}.{format}",
                        media_type="application/pdf" if format == "pdf" else
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@router.get("/runs/{run_id}/export.docx")
async def export_docx(run_id: int):
    return await export_review_file(await store.get(run_id), "docx")


@router.get("/runs/{run_id}/audit.json")
async def export_audit(run_id: int):
    run = await store.get(run_id)
    run["history"] = await store.history(run_id)
    return Response(json.dumps(run, ensure_ascii=False, default=str, indent=2),
        media_type="application/json", headers={"Content-Disposition": f'attachment; filename="research_run_{run_id}_audit.json"'})
