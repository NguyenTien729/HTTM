"""
Entry point. Chạy:
    uvicorn app.main:app --reload --port 8000

Tích hợp quy trình tự động, các điểm phê duyệt và các công cụ thao tác từng bước.
"""

from contextlib import asynccontextmanager
import asyncio
import contextlib

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
    from app.autonomous.worker import run_worker
    worker = asyncio.create_task(run_worker())
    try:
        yield
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        await close_db()


from app.data_research.router import router as data_research_router
from app.routers.review import router as review_router
app = FastAPI(
    title="HTTM Research API",
    description="Điều phối nghiên cứu và viết bài khoa học, với kiểm chứng và phê duyệt của người dùng.",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(data_research_router)
app.include_router(review_router)

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
from app.autonomous.router import router as autonomous_router
app.include_router(autonomous_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# Frontend đơn giản (app/static/index.html): mở http://127.0.0.1:8000/ui/
# CORS mở cho môi trường dev để file index.html mở trực tiếp (file://) vẫn gọi được API.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/ui/autonomous.html" if _STATIC_DIR.exists() else "/docs")


if _STATIC_DIR.exists():  # thiếu thư mục thì bỏ qua thay vì làm server không khởi động được
    app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")
