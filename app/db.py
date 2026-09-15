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
    Tạo bảng research_projects nếu chưa có.
    Dùng cột JSONB để lưu toàn bộ research plan (keywords, RQ, search strategy...)
    — linh hoạt hơn là tách cột cứng ở giai đoạn MVP.
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