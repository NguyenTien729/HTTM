"""
Entry point. Chạy:
    uvicorn app.main:app --reload --port 8000

MVP hiện tại chỉ có Research Planner (module V1 phần 2 trong kế hoạch).
Các router tiếp theo (search, paper reader, gap detector...) sẽ được
include ở đây theo cùng pattern.
"""

from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()  # đọc .env trước khi bất cứ module nào đọc os.environ

from fastapi import FastAPI

from app.db import connect_db, close_db
from app.routers import planner, search


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    yield
    await close_db()


app = FastAPI(
    title="Research Copilot API",
    description="Hệ thống hỗ trợ nghiên cứu khoa học có kiểm chứng (V1: Research Planner).",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(planner.router)
app.include_router(search.router)


@app.get("/health")
async def health():
    return {"status": "ok"}