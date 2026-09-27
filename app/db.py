"""
Quản lý kết nối PostgreSQL (asyncpg pool) và khởi tạo schema tối thiểu
cho module Research Planner (V1). Các bảng papers/chunks/claims sẽ
được thêm ở các module sau (Literature Search, Paper Reader...).
"""

import os
import asyncpg
from typing import Optional

_pool: Optional[asyncpg.Pool] = None


async def connect_db() -> None:
    """Mở connection pool. Gọi 1 lần khi app khởi động (lifespan)."""
    global _pool
    database_url = os.environ["DATABASE_URL"]
    _pool = await asyncpg.create_pool(
        dsn=database_url,
        min_size=1,
        max_size=10,
    )
    await _init_schema(_pool)


async def close_db() -> None:
    """Đóng connection pool. Gọi khi app shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Lấy pool hiện tại. Raise nếu chưa connect_db()."""
    if _pool is None:
        raise RuntimeError("Database pool chưa được khởi tạo. Gọi connect_db() trước.")
    return _pool


async def _init_schema(pool: asyncpg.Pool) -> None:
    """
    Tạo các bảng còn thiếu (idempotent — chạy lại không lỗi).

    research_projects: lưu research plan (JSONB) — module Research Planner (V1).
    papers: lưu metadata paper lấy về từ Literature Search (OpenAlex...) — module V1→V2.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_projects (
                id              SERIAL PRIMARY KEY,
                topic           TEXT NOT NULL,
                research_plan   JSONB NOT NULL,
                status          TEXT NOT NULL DEFAULT 'planned',
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS papers (
                id                SERIAL PRIMARY KEY,
                project_id        INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
                title             TEXT NOT NULL,
                authors           JSONB NOT NULL DEFAULT '[]',
                year              INTEGER,
                doi               TEXT,
                abstract          TEXT,
                venue             TEXT,
                url               TEXT,
                source            TEXT NOT NULL,
                citation_count    INTEGER NOT NULL DEFAULT 0,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        # Dedup theo DOI: 2 paper cùng DOI trong cùng project không được lưu 2 lần.
        # Dùng partial unique index vì nhiều paper không có DOI (NULL không xung đột nhau trong Postgres).
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS papers_project_doi_unique
            ON papers (project_id, doi)
            WHERE doi IS NOT NULL;
            """
        )

        # paper_analyses: kết quả phân tích 1 paper (rút gọn — chỉ dựa trên abstract,
        # chưa đọc full-text PDF). 1 paper chỉ có tối đa 1 analysis (unique paper_id).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_analyses (
                id              SERIAL PRIMARY KEY,
                paper_id        INTEGER NOT NULL UNIQUE REFERENCES papers(id) ON DELETE CASCADE,
                project_id      INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
                method          TEXT,
                population      TEXT,
                context         TEXT,
                key_finding     TEXT NOT NULL,
                limitation      TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )

        # themes: kết quả gom cụm của module Synthesis. Mỗi lần chạy lại /synthesize
        # sẽ xoá theme cũ của project và lưu theme mới (đơn giản cho MVP, chưa versioning).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS themes (
                id              SERIAL PRIMARY KEY,
                project_id      INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
                name            TEXT NOT NULL,
                description     TEXT NOT NULL,
                paper_ids       JSONB NOT NULL DEFAULT '[]',
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )

        # gaps: kết quả module Gap Detector. `confirmed` là checkpoint 👤 human approval —
        # mặc định false, người dùng phải chủ động duyệt qua endpoint riêng.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gaps (
                id                          SERIAL PRIMARY KEY,
                project_id                  INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
                gap_type                    TEXT NOT NULL,
                description                 TEXT NOT NULL,
                evidence_paper_ids          JSONB NOT NULL DEFAULT '[]',
                proposed_research_question  TEXT NOT NULL,
                confirmed                   BOOLEAN NOT NULL DEFAULT false,
                created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )