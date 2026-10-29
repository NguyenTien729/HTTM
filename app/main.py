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

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import connect_db, close_db
from app.routers import planner, search, analysis, synthesis, research_questions, writer, related_work, discussion, export, reviewer 


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    yield
    await close_db()


from app.data_research.router import router as data_research_router
app = FastAPI(
    title="Research Copilot API",
    description="Hệ thống hỗ trợ nghiên cứu khoa học có kiểm chứng (V1: Research Planner).",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(data_research_router)

app.include_router(planner.router)
app.include_router(search.router)
app.include_router(analysis.router)
app.include_router(synthesis.router)
app.include_router(research_questions.router)
app.include_router(writer.router)
app.include_router(related_work.router)
app.include_router(discussion.router)
app.include_router(export.router)
app.include_router(reviewer.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# Frontend đơn giản (app/static/index.html): mở http://127.0.0.1:8000/ui/
# CORS mở cho môi trường dev để file index.html mở trực tiếp (file://) vẫn gọi được API.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/ui/" if _STATIC_DIR.exists() else "/docs")


if _STATIC_DIR.exists():  # thiếu thư mục thì bỏ qua thay vì làm server không khởi động được
    app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")