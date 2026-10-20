import os

# Phải đặt TRƯỚC khi import app (llm.py tạo client lúc import).
os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://httm:httm@localhost:5432/httm")
os.environ.setdefault("ENABLE_SEMANTIC_SCHOLAR", "0")
os.environ.setdefault("EMBEDDING_PROVIDER", "none")

import httpx
import pytest

from app.main import app
from app import db


@pytest.fixture
async def client():
    """Client gọi thẳng vào app + DB thật (Postgres docker/CI). Mỗi test bắt đầu từ DB sạch.
    Nếu không kết nối được DB thì skip thay vì fail."""
    try:
        await db.connect_db()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Không kết nối được Postgres ({exc}). Chạy `docker compose up -d db` trước.")
    async with db.get_pool().acquire() as conn:
        await conn.execute("TRUNCATE research_projects RESTART IDENTITY CASCADE;")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await db.close_db()
