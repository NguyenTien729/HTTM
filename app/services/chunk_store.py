"""Lưu/đọc paper_chunks. Nhận `conn` (asyncpg) làm tham số, không tự quản lý pool."""

import re
import logging
from typing import Optional

from app.services.pdf_reader import download_pdf, extract_text, chunk_text, embed

logger = logging.getLogger(__name__)

# Từ khoá để chọn đoạn hữu ích cho việc trích method/population/finding/limitation
# khi KHÔNG dùng embedding.
_CUES = [
    "method", "methodology", "participants", "sample", "respondents", "survey",
    "interview", "experiment", "data collection", "questionnaire", "n =", "n=",
    "results", "findings", "found that", "significant", "limitation", "limitations",
    "future research", "future work", "conclusion",
]
DEFAULT_QUERY = "research method, participants and sample, main findings, limitations"


async def has_embedding_column(conn) -> bool:
    return bool(await conn.fetchval(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='paper_chunks' AND column_name='embedding'"
    ))


async def has_chunks(conn, paper_id: int) -> bool:
    return bool(await conn.fetchval("SELECT 1 FROM paper_chunks WHERE paper_id=$1 LIMIT 1", paper_id))


async def ingest_paper(conn, paper_id: int, pdf_url: Optional[str]) -> int:
    """Tải PDF -> chunk -> (embed) -> lưu. Trả số chunk mới; 0 nếu không có PDF dùng được
    hoặc đã ingest trước đó."""
    if await has_chunks(conn, paper_id):
        return 0
    pdf = await download_pdf(pdf_url)
    if not pdf:
        return 0
    text = extract_text(pdf).strip()
    if not text:
        return 0
    chunks = chunk_text(text)
    if not chunks:
        return 0

    vectors: Optional[list] = None
    use_vec = await has_embedding_column(conn)
    if use_vec:
        batch_vecs: list = []
        for i in range(0, len(chunks), 64):
            part = await embed(chunks[i : i + 64])
            if part is None:
                batch_vecs = None
                break
            batch_vecs += part
        vectors = batch_vecs

    async with conn.transaction():
        for idx, c in enumerate(chunks):
            if use_vec:
                vec = str(vectors[idx]) if vectors else None
                await conn.execute(
                    "INSERT INTO paper_chunks (paper_id, chunk_index, chunk_text, embedding) "
                    "VALUES ($1,$2,$3,$4::vector) ON CONFLICT DO NOTHING",
                    paper_id, idx, c, vec,
                )
            else:
                await conn.execute(
                    "INSERT INTO paper_chunks (paper_id, chunk_index, chunk_text) "
                    "VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                    paper_id, idx, c,
                )
    return len(chunks)


def _keyword_score(text: str) -> int:
    low = text.lower()
    return sum(low.count(cue) for cue in _CUES)


async def select_chunks(conn, paper_id: int, query: str = DEFAULT_QUERY, k: int = 6) -> list[str]:
    """Trả tối đa k đoạn liên quan nhất (theo thứ tự xuất hiện trong bài).
    Rỗng nghĩa là paper chưa có full-text -> phía gọi dùng abstract."""
    rows = await conn.fetch(
        "SELECT chunk_index, chunk_text FROM paper_chunks WHERE paper_id=$1 ORDER BY chunk_index", paper_id
    )
    if not rows:
        return []

    if await has_embedding_column(conn):
        qv = await embed([query])
        if qv:
            ranked = await conn.fetch(
                "SELECT chunk_index, chunk_text FROM paper_chunks "
                "WHERE paper_id=$1 AND embedding IS NOT NULL "
                "ORDER BY embedding <=> $2::vector LIMIT $3",
                paper_id, str(qv[0]), k,
            )
            if ranked:
                return [r["chunk_text"] for r in sorted(ranked, key=lambda r: r["chunk_index"])]

    scored = sorted(rows, key=lambda r: _keyword_score(r["chunk_text"]), reverse=True)[:k]
    return [r["chunk_text"] for r in sorted(scored, key=lambda r: r["chunk_index"])]
