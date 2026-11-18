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

import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException

from app.models import PaperAnalysis, PaperAnalysisExtraction, AnalyzeResponse
from app.db import get_pool
from app.services.llm import generate_structured
from app.services.chunk_store import ingest_paper, select_chunks

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analyze", tags=["paper-analyst"])


async def _fetch_papers_to_analyze(project_id: int) -> list[dict]:
    """Lấy paper CHƯA có analysis và có abstract HOẶC có pdf_url (để thử đọc full-text)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id, p.title, p.abstract, p.pdf_url
            FROM papers p
            LEFT JOIN paper_analyses pa ON pa.paper_id = p.id
            WHERE p.project_id = $1
              AND (pa.id IS NULL OR pa.plain_language_summary = '')
              AND ((p.abstract IS NOT NULL AND p.abstract <> '') OR p.pdf_url IS NOT NULL);
            """,
            project_id,
        )
    return [dict(r) for r in rows]


async def _ingest_fulltext(papers: list[dict], concurrency: int = 4) -> None:
    """Tải + chunk PDF cho các paper có pdf_url (song song có giới hạn). Không bao giờ raise:
    paper nào lỗi thì bỏ qua, sẽ fallback về abstract."""
    pool = get_pool()
    sem = asyncio.Semaphore(concurrency)

    async def one(paper: dict) -> None:
        if not paper.get("pdf_url"):
            return
        async with sem:
            try:
                async with pool.acquire() as conn:
                    await ingest_paper(conn, paper["id"], paper["pdf_url"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Ingest PDF paper %s lỗi, dùng abstract: %s", paper["id"], exc)

    await asyncio.gather(*(one(p) for p in papers))


async def _load_excerpts(paper_id: int) -> list[str]:
    pool = get_pool()
    async with pool.acquire() as conn:
        return await select_chunks(conn, paper_id)


async def _analyze_one(paper: dict, project_id: int, excerpts: list[str] | None = None) -> PaperAnalysis:
    abstract = paper.get("abstract") or "(không có abstract)"
    if excerpts:
        joined = "\n\n".join(f"[Đoạn {i}] {c}" for i, c in enumerate(excerpts, 1))
        prompt = (
            f"Title: {paper['title']}\n"
            f"Abstract: {abstract}\n\n"
            f"Các đoạn trích từ full-text của bài báo:\n{joined}\n\n"
            "Hãy phân tích bài báo dựa trên abstract và các đoạn trích trên: xác định phương pháp nghiên cứu, "
            "đối tượng/mẫu (kèm cỡ mẫu nếu có), bối cảnh, phát hiện chính, và hạn chế (ưu tiên hạn chế do chính tác giả nêu). "
            "Chỉ dùng thông tin có trong văn bản đã cho. Thông tin nào không có thì để null — KHÔNG suy đoán/bịa thêm."
        )
    else:
        prompt = (
            f"Title: {paper['title']}\n"
            f"Abstract: {abstract}\n\n"
            "Hãy phân tích abstract này: xác định phương pháp nghiên cứu, đối tượng/mẫu, "
            "bối cảnh (nếu có), phát hiện chính, và hạn chế (nếu abstract có đề cập). "
            "Nếu thông tin nào không có trong abstract, để null — KHÔNG suy đoán/bịa thêm."
        )
    prompt += (
        "\n\nWrite key_finding as a concise academic finding. Also provide plain_language_summary in clear Vietnamese: "
        "state what was studied, what was found, and what the evidence does not prove. Explain technical terms briefly. "
        "Do not infer causality from observational results. Clearly distinguish facts reported by the authors from interpretation. "
        "If the supplied text does not state a detail, leave its field null rather than guessing."
        " Provide one short, verbatim evidence_excerpt supporting the main finding, or null if no excerpt does so."
    )
    extracted = await generate_structured(prompt, PaperAnalysisExtraction)
    supplied_text = " ".join([paper["title"], abstract, *(excerpts or [])]).casefold()
    quote = re.sub(r"\s+", " ", extracted.evidence_excerpt or "").strip()
    normalized_source = re.sub(r"\s+", " ", supplied_text)
    if not quote or len(quote) > 700 or re.sub(r"\s+", " ", quote.casefold()) not in normalized_source:
        quote = None
    return PaperAnalysis(
        paper_id=paper["id"],
        project_id=project_id,
        method=extracted.method,
        population=extracted.population,
        context=extracted.context,
        key_finding=extracted.key_finding,
        plain_language_summary=extracted.plain_language_summary,
        evidence_excerpt=quote,
        evidence_basis="fulltext_pdf_excerpts" if excerpts else "abstract_only",
        title=paper["title"],
        limitation=extracted.limitation,
    )


async def _save_analysis(analysis: PaperAnalysis) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO paper_analyses (paper_id, project_id, method, population, context, key_finding, plain_language_summary, evidence_excerpt, evidence_basis, limitation)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (paper_id) DO UPDATE SET
                method=EXCLUDED.method,
                population=EXCLUDED.population,
                context=EXCLUDED.context,
                key_finding=EXCLUDED.key_finding,
                plain_language_summary=EXCLUDED.plain_language_summary,
                evidence_excerpt=EXCLUDED.evidence_excerpt,
                evidence_basis=EXCLUDED.evidence_basis,
                limitation=EXCLUDED.limitation;
            """,
            analysis.paper_id,
            analysis.project_id,
            analysis.method,
            analysis.population,
            analysis.context,
            analysis.key_finding,
            analysis.plain_language_summary,
            analysis.evidence_excerpt,
            analysis.evidence_basis,
            analysis.limitation,
        )


async def _fetch_all_analyses(project_id: int) -> list[PaperAnalysis]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT pa.*, p.title FROM paper_analyses pa JOIN papers p ON p.id=pa.paper_id WHERE pa.project_id = $1 ORDER BY pa.id;", project_id
        )
    return [
        PaperAnalysis(
            id=r["id"], paper_id=r["paper_id"], project_id=r["project_id"],
            method=r["method"], population=r["population"], context=r["context"],
            key_finding=r["key_finding"], plain_language_summary=r["plain_language_summary"],
            evidence_excerpt=r["evidence_excerpt"],
            evidence_basis=r["evidence_basis"], title=r["title"], limitation=r["limitation"],
        )
        for r in rows
    ]


@router.post("/{project_id}", response_model=AnalyzeResponse)
async def analyze_papers(project_id: int, use_fulltext: bool = True) -> AnalyzeResponse:
    """Phân tích các paper chưa được phân tích trong project.
    use_fulltext=true (mặc định): thử tải PDF open-access và đọc full-text; paper nào không có/không tải
    được PDF thì fallback về abstract. use_fulltext=false: chỉ dùng abstract như bản cũ."""
    to_analyze = await _fetch_papers_to_analyze(project_id)
    if not to_analyze:
        all_analyses = await _fetch_all_analyses(project_id)
        return AnalyzeResponse(
            project_id=project_id, analyzed_now=0,
            total_analyzed=len(all_analyses), analyses=all_analyses,
        )

    if use_fulltext:
        await _ingest_fulltext(to_analyze)

    analyzed_now = 0
    fulltext_used = 0
    for paper in to_analyze:
        excerpts = await _load_excerpts(paper["id"]) if use_fulltext else []
        if not excerpts and not paper.get("abstract"):
            continue  # không có cả full-text lẫn abstract -> không có gì để phân tích
        analysis = await _analyze_one(paper, project_id, excerpts)
        await _save_analysis(analysis)
        analyzed_now += 1
        fulltext_used += 1 if excerpts else 0

    all_analyses = await _fetch_all_analyses(project_id)
    return AnalyzeResponse(
        project_id=project_id,
        analyzed_now=analyzed_now,
        fulltext_used=fulltext_used,
        total_analyzed=len(all_analyses),
        analyses=all_analyses,
    )


@router.get("/{project_id}", response_model=list[PaperAnalysis])
async def list_analyses(project_id: int) -> list[PaperAnalysis]:
    return await _fetch_all_analyses(project_id)
