"""
Module Paper Analyst (rút gọn — V1 dùng abstract, chưa đọc full-text PDF).

Luồng:
    papers (đã có title/abstract từ Literature Search)
        ↓ với mỗi paper chưa phân tích
    LLM (ép JSON qua response_schema=PaperAnalysisExtraction)
        ↓
    Lưu bảng `paper_analyses`

Đây là nền tảng cho module Synthesis + Gap Detector ở bước sau.
Lưu ý: phân tích chỉ dựa trên abstract nên method/population/context có thể
null nếu abstract không nêu rõ — KHÔNG suy đoán thêm, tránh hallucination.
"""

import logging

from fastapi import APIRouter, HTTPException

from app.models import PaperAnalysis, PaperAnalysisExtraction, AnalyzeResponse
from app.db import get_pool
from app.services.llm import generate_structured

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analyze", tags=["paper-analyst"])


async def _fetch_papers_to_analyze(project_id: int) -> list[dict]:
    """Lấy paper CHƯA có analysis và có abstract (không phân tích được nếu rỗng)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id, p.title, p.abstract
            FROM papers p
            LEFT JOIN paper_analyses pa ON pa.paper_id = p.id
            WHERE p.project_id = $1
              AND pa.id IS NULL
              AND p.abstract IS NOT NULL
              AND p.abstract <> '';
            """,
            project_id,
        )
    return [dict(r) for r in rows]


async def _analyze_one(paper: dict, project_id: int) -> PaperAnalysis:
    prompt = (
        f"Title: {paper['title']}\n"
        f"Abstract: {paper['abstract']}\n\n"
        "Hãy phân tích abstract này: xác định phương pháp nghiên cứu, đối tượng/mẫu, "
        "bối cảnh (nếu có), phát hiện chính, và hạn chế (nếu abstract có đề cập). "
        "Nếu thông tin nào không có trong abstract, để null — KHÔNG suy đoán/bịa thêm."
    )
    extracted = await generate_structured(prompt, PaperAnalysisExtraction)
    return PaperAnalysis(
        paper_id=paper["id"],
        project_id=project_id,
        method=extracted.method,
        population=extracted.population,
        context=extracted.context,
        key_finding=extracted.key_finding,
        limitation=extracted.limitation,
    )


async def _save_analysis(analysis: PaperAnalysis) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO paper_analyses (paper_id, project_id, method, population, context, key_finding, limitation)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (paper_id) DO NOTHING;
            """,
            analysis.paper_id,
            analysis.project_id,
            analysis.method,
            analysis.population,
            analysis.context,
            analysis.key_finding,
            analysis.limitation,
        )


async def _fetch_all_analyses(project_id: int) -> list[PaperAnalysis]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM paper_analyses WHERE project_id = $1 ORDER BY id;", project_id
        )
    return [
        PaperAnalysis(
            id=r["id"], paper_id=r["paper_id"], project_id=r["project_id"],
            method=r["method"], population=r["population"], context=r["context"],
            key_finding=r["key_finding"], limitation=r["limitation"],
        )
        for r in rows
    ]


@router.post("/{project_id}", response_model=AnalyzeResponse)
async def analyze_papers(project_id: int) -> AnalyzeResponse:
    """Phân tích abstract của các paper chưa được phân tích trong project.
    Gọi LLM tuần tự từng paper (đơn giản cho MVP) — với project nhiều paper,
    endpoint này có thể mất vài chục giây tới vài phút."""
    to_analyze = await _fetch_papers_to_analyze(project_id)
    if not to_analyze:
        all_analyses = await _fetch_all_analyses(project_id)
        return AnalyzeResponse(
            project_id=project_id, analyzed_now=0,
            total_analyzed=len(all_analyses), analyses=all_analyses,
        )

    analyzed_now = 0
    for paper in to_analyze:
        analysis = await _analyze_one(paper, project_id)
        await _save_analysis(analysis)
        analyzed_now += 1

    all_analyses = await _fetch_all_analyses(project_id)
    return AnalyzeResponse(
        project_id=project_id,
        analyzed_now=analyzed_now,
        total_analyzed=len(all_analyses),
        analyses=all_analyses,
    )


@router.get("/{project_id}", response_model=list[PaperAnalysis])
async def list_analyses(project_id: int) -> list[PaperAnalysis]:
    return await _fetch_all_analyses(project_id)