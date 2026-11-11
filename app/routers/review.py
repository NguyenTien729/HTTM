"""Narrative-review workflow: corpus -> editable manuscript -> validation -> export."""
import json
import asyncio
from pathlib import Path
from tempfile import gettempdir
from typing import Literal
from uuid import uuid4
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from app.db import get_pool
from app.services.review_manuscript import (
    WrittenSection, SECTION_TITLES, write_review, check_manuscript, repair_and_validate,
)
from app.routers.export import PaperExportOptions

router = APIRouter(prefix="/reviews", tags=["review-manuscript"])


def unpack(value):
    return json.loads(value) if isinstance(value, str) else value


async def load_corpus(project_id):
    async with get_pool().acquire() as conn:
        project = await conn.fetchrow("SELECT * FROM research_projects WHERE id=$1", project_id)
        if not project:
            raise HTTPException(404, "Không tìm thấy project.")
        if project["paper_type"] != "review":
            raise HTTPException(409, "Project này thuộc nhánh nghiên cứu gốc; hãy chọn loại bài tổng quan khi tạo project.")
        rows = await conn.fetch(
            """SELECT p.*, pa.method, pa.population, pa.key_finding, pa.limitation
               FROM papers p JOIN paper_analyses pa ON pa.paper_id=p.id
               WHERE p.project_id=$1 ORDER BY p.year DESC NULLS LAST, p.id LIMIT 41""", project_id)
        ids = [r["id"] for r in rows[:40]]
        chunks = await conn.fetch(
            """SELECT * FROM (
                SELECT id, paper_id, chunk_index, chunk_text,
                       row_number() OVER (PARTITION BY paper_id ORDER BY
                         CASE WHEN lower(chunk_text) LIKE '%result%' OR lower(chunk_text) LIKE '%finding%' THEN 0 ELSE 1 END,
                         chunk_index) AS rank
                FROM paper_chunks WHERE paper_id=ANY($1::int[])
               ) selected WHERE rank<=2 ORDER BY paper_id, chunk_index""", ids)
        audit = await conn.fetchrow("SELECT audit FROM literature_search_runs WHERE project_id=$1 ORDER BY id DESC LIMIT 1", project_id)
    sources = []
    for row in rows[:40]:
        if row["abstract"] and len(row["abstract"].strip()) >= 20:
            sources.append({"source_id": f"paper:{row['id']}:abstract", "paper_id": row["id"],
                "kind": "abstract", "text": row["abstract"][:2600]})
    for chunk in chunks:
        if len(chunk["chunk_text"].strip()) >= 20:
            sources.append({"source_id": f"chunk:{chunk['id']}", "paper_id": chunk["paper_id"],
                "kind": "fulltext_chunk", "chunk_index": chunk["chunk_index"], "text": chunk["chunk_text"][:1600]})
    available = {s["paper_id"] for s in sources}
    papers = [{k: row[k] for k in ("id", "title", "authors", "year", "doi", "venue", "method", "population", "key_finding", "limitation")}
              for row in rows[:40] if row["id"] in available]
    if len(papers) < 2:
        raise HTTPException(400, "Cần ít nhất hai bài đã phân tích và có tóm tắt/toàn văn đọc được để viết bài tổng quan. Hãy tìm và phân tích tài liệu trước.")
    plan = unpack(project["research_plan"])
    return {"topic": project["topic"], "keywords": plan.get("keywords", [])[:8], "papers": papers,
            "evidence_sources": sources, "search_audit": unpack(audit["audit"]) if audit else None,
            "corpus_limit": 40, "truncated": len(rows) > 40}


async def store(project_id, manuscript, validation):
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO review_manuscripts(project_id, manuscript, validation)
               VALUES($1,$2::jsonb,$3::jsonb) RETURNING id, created_at""",
            project_id, json.dumps(manuscript, ensure_ascii=False, default=str), json.dumps(validation, ensure_ascii=False))
    return {"id": row["id"], "project_id": project_id, "status": "draft", "created_at": row["created_at"],
            "manuscript": manuscript, "validation": validation}


async def load_review(manuscript_id):
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM review_manuscripts WHERE id=$1", manuscript_id)
    if not row:
        raise HTTPException(404, "Không tìm thấy bản thảo tổng quan.")
    return dict(row) | {"manuscript": unpack(row["manuscript"]), "validation": unpack(row["validation"])}


@router.post("/project/{project_id}")
async def create_review(project_id: int):
    corpus = await load_corpus(project_id)
    manuscript = await write_review(corpus)
    report = check_manuscript(manuscript)
    return await store(project_id, manuscript, report)


@router.get("/project/{project_id}")
async def list_reviews(project_id: int):
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT id, status, created_at FROM review_manuscripts WHERE project_id=$1 ORDER BY id DESC", project_id)
    return [dict(row) for row in rows]


@router.get("/{manuscript_id}")
async def get_review(manuscript_id: int):
    return await load_review(manuscript_id)


class ReviewEdit(BaseModel):
    title: str = Field(min_length=5, max_length=500)
    sections: dict[str, WrittenSection]


@router.put("/{manuscript_id}")
async def edit_review(manuscript_id: int, edit: ReviewEdit):
    saved = await load_review(manuscript_id)
    expected = set(SECTION_TITLES) - {"methodology"}
    if set(edit.sections) != expected:
        raise HTTPException(422, "Cần gửi đủ các mục trừ methodology; phương pháp được giữ theo nhật ký thực tế.")
    manuscript = saved["manuscript"] | {"title": edit.title}
    manuscript["sections"] = [section if section["key"] == "methodology" else
        section | {"paragraphs": [p.model_dump() for p in edit.sections[section["key"]].paragraphs]}
        for section in manuscript["sections"]]
    # New immutable version, so any prior validation cannot authorize changed text.
    manuscript["previous_version_id"] = manuscript_id
    return await store(saved["project_id"], manuscript, check_manuscript(manuscript))


@router.post("/{manuscript_id}/validate")
async def validate_review(manuscript_id: int):
    saved = await load_review(manuscript_id)
    if saved["status"] == "validated" and saved["validation"].get("ok"):
        return {"id": manuscript_id, "status": "validated", "validation": saved["validation"],
                "manuscript": saved["manuscript"]}
    manuscript, report = await repair_and_validate(saved["manuscript"])
    if manuscript != saved["manuscript"]:
        manuscript["previous_version_id"] = manuscript_id
        saved = await store(saved["project_id"], manuscript, report)
        manuscript_id = saved["id"]
    status = "validated" if report["ok"] else "needs_revision"
    async with get_pool().acquire() as conn:
        await conn.execute("UPDATE review_manuscripts SET validation=$2::jsonb, status=$3 WHERE id=$1",
                           manuscript_id, json.dumps(report, ensure_ascii=False), status)
    return {"id": manuscript_id, "status": status, "validation": report,
            "manuscript": manuscript}


@router.post("/{manuscript_id}/export/{format}")
async def export_review(manuscript_id: int, format: Literal["pdf", "docx"], options: PaperExportOptions | None = None):
    saved = await load_review(manuscript_id)
    if saved["status"] != "validated" or not saved["validation"].get("ok"):
        raise HTTPException(409, "Bản thảo phải qua bước kiểm chứng trước khi xuất. Hãy sửa các đoạn được đánh dấu rồi kiểm chứng lại.")
    # Content changes require a new manuscript version and a new validation.
    if options and options.title and options.title != saved["manuscript"]["title"]:
        raise HTTPException(409, "Hãy sửa tiêu đề trong bản thảo và kiểm chứng phiên bản mới trước khi xuất.")
    from app.services.review_export import build_review_pdf, build_review_docx
    directory = Path(gettempdir()) / "httm_review_exports"
    directory.mkdir(exist_ok=True)
    path = directory / f"review_{manuscript_id}_{uuid4().hex}.{format}"
    builder = build_review_pdf if format == "pdf" else build_review_docx
    try:
        await asyncio.to_thread(builder, saved["manuscript"], path, options)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return FileResponse(path, filename=f"review_{manuscript_id}.{format}",
        media_type="application/pdf" if format == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        background=BackgroundTask(path.unlink, missing_ok=True))
