"""
Module Literature Synthesis + Research Gap Detector.

Luồng:
    paper_analyses (đã có từ POST /analyze/{project_id})
        ↓ Topic clustering (1 lần gọi LLM cho toàn bộ tập, không phải 1 lần/paper)
    themes
        ↓ Gap analysis dựa trên gap matrix (population/method/context) + themes
    gaps (mặc định confirmed=false — 👤 CẦN người duyệt qua /gaps/{id}/approve)

Không prompt kiểu "đọc N paper rồi viết literature review" — dễ hallucination.
Thay vào đó bắt LLM luôn tham chiếu paper_id thật đã cho trong prompt.
"""

import json
import logging

from fastapi import APIRouter, HTTPException

from app.models import (
    Theme, ThemesExtraction, SynthesizeResponse,
    Gap, GapsExtraction, GapsResponse,
)
from app.db import get_pool
from app.services.llm import generate_structured

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/synthesize", tags=["synthesis"])


async def _fetch_analyses_with_title(project_id: int) -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pa.paper_id, p.title, pa.method, pa.population, pa.context,
                   pa.key_finding, pa.limitation
            FROM paper_analyses pa
            JOIN papers p ON p.id = pa.paper_id
            WHERE pa.project_id = $1
            ORDER BY pa.paper_id;
            """,
            project_id,
        )
    return [dict(r) for r in rows]


def _format_analyses_for_prompt(analyses: list[dict]) -> str:
    lines = []
    for a in analyses:
        lines.append(
            f"paper_id={a['paper_id']} | title=\"{a['title']}\" | "
            f"method={a['method']} | population={a['population']} | context={a['context']} | "
            f"finding=\"{a['key_finding']}\" | limitation={a['limitation']}"
        )
    return "\n".join(lines)


async def _load_themes(project_id: int) -> list[Theme]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM themes WHERE project_id = $1 ORDER BY id;", project_id)
    return [
        Theme(
            id=r["id"], project_id=r["project_id"], name=r["name"], description=r["description"],
            paper_ids=json.loads(r["paper_ids"]) if isinstance(r["paper_ids"], str) else r["paper_ids"],
        )
        for r in rows
    ]


@router.post("/{project_id}", response_model=SynthesizeResponse)
async def synthesize_themes(project_id: int) -> SynthesizeResponse:
    """Gom các paper đã phân tích thành các theme (cluster theo phát hiện chính giống/đối lập nhau)."""
    analyses = await _fetch_analyses_with_title(project_id)
    if not analyses:
        raise HTTPException(
            status_code=400,
            detail="Chưa có paper nào được phân tích. Gọi POST /analyze/{project_id} trước.",
        )

    prompt = (
        "Dưới đây là danh sách paper đã được phân tích (mỗi dòng 1 paper, có paper_id thật):\n\n"
        f"{_format_analyses_for_prompt(analyses)}\n\n"
        "Hãy gom các paper này thành các theme (nhóm phát hiện chính giống nhau hoặc đối lập nhau). "
        "Mỗi theme cần: tên ngắn gọn, mô tả, và danh sách paper_id thuộc theme đó — "
        "PHẢI dùng đúng paper_id có trong danh sách trên, không được bịa số mới. "
        "1 paper có thể thuộc nhiều theme nếu phù hợp."
    )

    extracted = await generate_structured(prompt, ThemesExtraction)

    pool = get_pool()
    saved_themes: list[Theme] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Xoá theme cũ trước khi lưu bộ mới (MVP: không versioning theme)
            await conn.execute("DELETE FROM themes WHERE project_id = $1;", project_id)
            for t in extracted.themes:
                row = await conn.fetchrow(
                    """
                    INSERT INTO themes (project_id, name, description, paper_ids)
                    VALUES ($1, $2, $3, $4::jsonb)
                    RETURNING id;
                    """,
                    project_id, t.name, t.description, json.dumps(t.paper_ids),
                )
                saved_themes.append(
                    Theme(id=row["id"], project_id=project_id, name=t.name,
                          description=t.description, paper_ids=t.paper_ids)
                )

    return SynthesizeResponse(project_id=project_id, themes=saved_themes)


@router.get("/{project_id}", response_model=list[Theme])
async def list_themes(project_id: int) -> list[Theme]:
    return await _load_themes(project_id)


@router.post("/{project_id}/gaps", response_model=GapsResponse)
async def detect_gaps(project_id: int) -> GapsResponse:
    """Phát hiện research gap dựa trên gap matrix (population/method/context) + themes đã có."""
    analyses = await _fetch_analyses_with_title(project_id)
    themes = await _load_themes(project_id)

    if not analyses:
        raise HTTPException(
            status_code=400,
            detail="Chưa có paper nào được phân tích. Gọi POST /analyze/{project_id} trước.",
        )
    if not themes:
        raise HTTPException(
            status_code=400,
            detail="Chưa có theme nào. Gọi POST /synthesize/{project_id} trước.",
        )

    themes_text = "\n".join(f"- {t.name}: {t.description} (papers: {t.paper_ids})" for t in themes)

    prompt = (
        "GAP MATRIX — danh sách paper đã phân tích (population/method/context):\n\n"
        f"{_format_analyses_for_prompt(analyses)}\n\n"
        "THEMES đã tổng hợp:\n"
        f"{themes_text}\n\n"
        "Hãy phát hiện research gap dựa trên dữ liệu trên. Mỗi gap gồm: loại gap "
        "(population / method / context / theoretical / dataset / contradiction / temporal), "
        "mô tả (bằng chứng quan sát được + khoảng trống tiềm năng), danh sách paper_id làm bằng chứng "
        "(PHẢI lấy đúng từ danh sách trên, không bịa số mới), và 1 câu hỏi nghiên cứu đề xuất cho gap đó. "
        "Phân biệt rõ 'không tìm thấy trong tập paper này' với 'chắc chắn chưa có nghiên cứu nào từng làm' — "
        "chỉ nêu gap có cơ sở từ dữ liệu đã cho, không suy đoán quá xa ngoài dữ liệu."
    )

    extracted = await generate_structured(prompt, GapsExtraction)

    pool = get_pool()
    saved_gaps: list[Gap] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Chỉ xoá gap CHƯA được người dùng duyệt — gap đã confirmed=true giữ nguyên,
            # không để lần chạy lại xoá mất quyết định khoa học người dùng đã chốt.
            await conn.execute(
                "DELETE FROM gaps WHERE project_id = $1 AND confirmed = false;", project_id
            )
            for g in extracted.gaps:
                row = await conn.fetchrow(
                    """
                    INSERT INTO gaps (project_id, gap_type, description, evidence_paper_ids, proposed_research_question)
                    VALUES ($1, $2, $3, $4::jsonb, $5)
                    RETURNING id;
                    """,
                    project_id, g.gap_type, g.description,
                    json.dumps(g.evidence_paper_ids), g.proposed_research_question,
                )
                saved_gaps.append(
                    Gap(id=row["id"], project_id=project_id, gap_type=g.gap_type,
                        description=g.description, evidence_paper_ids=g.evidence_paper_ids,
                        proposed_research_question=g.proposed_research_question, confirmed=False)
                )

    return GapsResponse(project_id=project_id, gaps=saved_gaps)


@router.get("/{project_id}/gaps", response_model=list[Gap])
async def list_gaps(project_id: int) -> list[Gap]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM gaps WHERE project_id = $1 ORDER BY id;", project_id)
    return [
        Gap(
            id=r["id"], project_id=r["project_id"], gap_type=r["gap_type"], description=r["description"],
            evidence_paper_ids=json.loads(r["evidence_paper_ids"]) if isinstance(r["evidence_paper_ids"], str) else r["evidence_paper_ids"],
            proposed_research_question=r["proposed_research_question"], confirmed=r["confirmed"],
        )
        for r in rows
    ]


@router.post("/{project_id}/gaps/{gap_id}/approve", response_model=Gap)
async def approve_gap(project_id: int, gap_id: int) -> Gap:
    """👤 Human approval checkpoint — người dùng chủ động xác nhận gap này là thật,
    không phải chỉ là 'không tìm thấy trong tập paper đã search'."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE gaps SET confirmed = true WHERE id = $1 AND project_id = $2 RETURNING *;",
            gap_id, project_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy gap.")
    return Gap(
        id=row["id"], project_id=row["project_id"], gap_type=row["gap_type"], description=row["description"],
        evidence_paper_ids=json.loads(row["evidence_paper_ids"]) if isinstance(row["evidence_paper_ids"], str) else row["evidence_paper_ids"],
        proposed_research_question=row["proposed_research_question"], confirmed=row["confirmed"],
    )