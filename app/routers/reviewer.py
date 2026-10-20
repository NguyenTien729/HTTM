"""
Module 12 — Reviewer Agent #1: Citation checker.

- POST /review/citations/{project_id}         : kiểm tra 1 đoạn draft bất kỳ
- GET  /review/citations/{project_id}/stored  : kiểm tra Introduction + Related Work đã lưu trong DB
"""

import json
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.db import get_pool
from app.services.citation_checker import check_text

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/review/citations", tags=["reviewer"])


class DraftCheckRequest(BaseModel):
    text: str


async def _valid_paper_ids(project_id: int) -> set[int]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM papers WHERE project_id = $1;", project_id)
    return {r["id"] for r in rows}


def _as_list(value):
    return json.loads(value) if isinstance(value, str) else (value or [])


@router.post("/{project_id}")
async def check_draft(project_id: int, payload: DraftCheckRequest) -> dict:
    valid = await _valid_paper_ids(project_id)
    return {"project_id": project_id, **check_text(payload.text, valid)}


@router.get("/{project_id}/stored")
async def check_stored(project_id: int) -> dict:
    valid = await _valid_paper_ids(project_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        intros = await conn.fetch("SELECT * FROM introductions WHERE project_id = $1 ORDER BY id;", project_id)
        works = await conn.fetch("SELECT * FROM related_works WHERE project_id = $1 ORDER BY id;", project_id)

    reports = []
    for r in intros:
        text = "\n".join(r[k] for k in (
            "background", "problem_statement", "existing_knowledge", "literature_limitation",
            "research_gap_statement", "research_question_statement", "contribution_statement",
        ))
        reports.append({"type": "introduction", "id": r["id"], **check_text(text, valid)})
    for r in works:
        parts = [tp.get("paragraph", "") for tp in _as_list(r["theme_paragraphs"])]
        parts += [r["contradictions_paragraph"], r["gap_paragraph"]]
        reports.append({"type": "related_work", "id": r["id"], **check_text("\n".join(parts), valid)})

    return {
        "project_id": project_id,
        "all_ok": all(rep["ok"] for rep in reports),
        "reports": reports,
    }
