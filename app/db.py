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

        # research_questions: kết quả module RQ Generator. Chỉ sinh từ gap đã confirmed=true
        # (ràng buộc kiểm tra ở tầng code, không ở DB, để thông báo lỗi rõ ràng hơn cho client).
        # 1 gap có thể sinh lại nhiều lần (không unique) — mỗi lần chạy lưu thêm 1 dòng mới.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_questions (
                id                  SERIAL PRIMARY KEY,
                project_id          INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
                gap_id              INTEGER NOT NULL REFERENCES gaps(id) ON DELETE CASCADE,
                rq_text             TEXT NOT NULL,
                feasibility_score   INTEGER NOT NULL,
                feasibility_notes   TEXT NOT NULL,
                novelty_score       INTEGER NOT NULL,
                novelty_notes       TEXT NOT NULL,
                is_quantitative     BOOLEAN NOT NULL,
                variables           JSONB NOT NULL DEFAULT '[]',
                hypotheses          JSONB NOT NULL DEFAULT '[]',
                created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )


        # introductions: kết quả module Scientific Writer (phần Introduction).
        # citations_used/citations_invalid do CODE tính sau khi verify — không phải LLM tự khai.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS introductions (
                id                              SERIAL PRIMARY KEY,
                project_id                      INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
                research_question_id            INTEGER NOT NULL REFERENCES research_questions(id) ON DELETE CASCADE,
                background                      TEXT NOT NULL,
                problem_statement               TEXT NOT NULL,
                existing_knowledge              TEXT NOT NULL,
                literature_limitation           TEXT NOT NULL,
                research_gap_statement          TEXT NOT NULL,
                research_question_statement     TEXT NOT NULL,
                contribution_statement          TEXT NOT NULL,
                citations_used                  JSONB NOT NULL DEFAULT '[]',
                citations_invalid               JSONB NOT NULL DEFAULT '[]',
                created_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )

        # related_works: kết quả module Scientific Writer (phần Related Work).
        # theme_paragraphs là mảng {theme_name, paragraph} — 1 phần tử / theme.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS related_works (
                id                          SERIAL PRIMARY KEY,
                project_id                  INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
                research_question_id        INTEGER NOT NULL REFERENCES research_questions(id) ON DELETE CASCADE,
                theme_paragraphs            JSONB NOT NULL DEFAULT '[]',
                contradictions_paragraph    TEXT NOT NULL,
                gap_paragraph               TEXT NOT NULL,
                citations_used              JSONB NOT NULL DEFAULT '[]',
                citations_invalid           JSONB NOT NULL DEFAULT '[]',
                created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )