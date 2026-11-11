"""
Quản lý kết nối PostgreSQL (asyncpg pool) và khởi tạo schema tối thiểu
cho module Research Planner (V1). Các bảng papers/chunks/claims sẽ
được thêm ở các module sau (Literature Search, Paper Reader...).
"""

import os
import logging
import asyncpg
from typing import Optional

logger = logging.getLogger(__name__)
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

        await conn.execute("ALTER TABLE research_projects ADD COLUMN IF NOT EXISTS paper_type TEXT NOT NULL DEFAULT 'original_research';")
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS literature_search_runs (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
                audit JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );"""
        )
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS review_manuscripts (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
                manuscript JSONB NOT NULL,
                validation JSONB NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS review_manuscripts_project_idx ON review_manuscripts(project_id, id);
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
                gap_id              INTEGER REFERENCES gaps(id) ON DELETE CASCADE,
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


        # Topic-driven research questions have no gap; preserve the foreign key for branch B.
        await conn.execute("ALTER TABLE research_questions ALTER COLUMN gap_id DROP NOT NULL;")

        # Discussion writer and paper export share this persisted snapshot.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discussions (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
                research_question_id INTEGER NOT NULL REFERENCES research_questions(id) ON DELETE CASCADE,
                key_findings_paragraph TEXT NOT NULL,
                literature_comparison_paragraph TEXT NOT NULL,
                hypothesis_assessments JSONB NOT NULL DEFAULT '[]',
                implications_paragraph TEXT NOT NULL,
                limitations_paragraph TEXT NOT NULL,
                future_work_paragraph TEXT NOT NULL,
                conclusion_paragraph TEXT NOT NULL,
                citations_used JSONB NOT NULL DEFAULT '[]',
                citations_invalid JSONB NOT NULL DEFAULT '[]',
                numbers_unverified JSONB NOT NULL DEFAULT '[]',
                analysis_snapshot JSONB NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        # Upgrade databases that already have the writer's older table.
        await conn.execute("ALTER TABLE discussions ADD COLUMN IF NOT EXISTS analysis_snapshot JSONB NOT NULL DEFAULT '{}';")
        await conn.execute("CREATE INDEX IF NOT EXISTS discussions_project_id_idx ON discussions (project_id);")

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

        # ---- Paper Reader (full-text) ----
        # pdf_url: link PDF open-access (OpenAlex/Semantic Scholar). ALTER idempotent nên DB cũ vẫn chạy.
        await conn.execute("ALTER TABLE papers ADD COLUMN IF NOT EXISTS pdf_url TEXT;")
        await conn.execute("ALTER TABLE papers ADD COLUMN IF NOT EXISTS publication_date TEXT;")

        # paper_chunks: full-text chia đoạn. Cột embedding dùng pgvector; nếu DB không có
        # extension `vector` (image postgres thường) thì vẫn tạo bảng nhưng KHÔNG có cột embedding,
        # khi đó chunk_store tự chuyển sang xếp hạng theo từ khoá.
        embedding_dim = int(os.environ.get("EMBEDDING_DIM", "1536"))
        has_vector = True
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        except Exception as exc:  # noqa: BLE001
            has_vector = False
            logger.warning("Không bật được pgvector (%s) — paper_chunks sẽ không có embedding.", exc)
        embedding_col = f"embedding vector({embedding_dim})," if has_vector else ""
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS paper_chunks (
                id              SERIAL PRIMARY KEY,
                paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                chunk_index     INTEGER NOT NULL,
                chunk_text      TEXT NOT NULL,
                {embedding_col}
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (paper_id, chunk_index)
            );
            """
        )
