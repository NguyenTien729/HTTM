import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from app.db import get_pool
from app.models import ResearchPlanRequest
from app.routers.planner import create_plan
from . import store
from .data import ROOT
from .models import StartRun, Decision, VersionRequest
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
        if project["paper_type"] == "review":
            raise HTTPException(409, "Luồng này dành cho nghiên cứu thực nghiệm; hãy tạo project riêng từ chủ đề.")
        project_id, topic = request.project_id, project["topic"]
    else:
        result = await create_plan(ResearchPlanRequest(topic=request.topic, region=request.region,
            target_paper_count=request.target_paper_count, paper_type="original_research"))
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
    for name, text in draft["sections"].items():
        parts.extend(["## " + name, text])
    parts.append("## References")
    for ref in draft["references"]:
        parts.append(f"[{ref['id']}] {'; '.join(ref['authors'])} ({ref.get('year') or 'n.d.'}). "
                     f"{ref['title']}. {ref.get('venue') or ''}. {ref.get('doi') or ''}")
    return "\n\n".join(parts)


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


@router.get("/runs/{run_id}/audit.json")
async def export_audit(run_id: int):
    run = await store.get(run_id)
    run["history"] = await store.history(run_id)
    return Response(json.dumps(run, ensure_ascii=False, default=str, indent=2),
        media_type="application/json", headers={"Content-Disposition": f'attachment; filename="research_run_{run_id}_audit.json"'})
