"""
Paper Reader (full-text): tải PDF open-access -> trích text -> chia đoạn -> (tuỳ chọn) embedding.

Mọi hàm ở đây KHÔNG raise khi paper không có PDF/PDF hỏng — trả None/rỗng để phía gọi
fallback về abstract.

Embedding cấu hình qua biến môi trường (DeepSeek không có API embedding):
    EMBEDDING_PROVIDER = none (mặc định) | openai
    EMBEDDING_API_KEY, EMBEDDING_MODEL (mặc định text-embedding-3-small),
    EMBEDDING_BASE_URL (tuỳ chọn, cho API tương thích OpenAI), EMBEDDING_DIM (mặc định 1536)
Với `none`, chunk vẫn được lưu và chọn đoạn theo từ khoá — không cần key nào thêm.
"""

import os
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 30 * 1024 * 1024  # bỏ qua PDF > 30MB
_HEADERS = {"User-Agent": "Mozilla/5.0 (research-copilot)"}


async def download_pdf(url: Optional[str], timeout: float = 30.0) -> Optional[bytes]:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=_HEADERS) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        logger.info("Không tải được PDF %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        return None
    body = resp.content
    if len(body) > MAX_PDF_BYTES:
        return None
    # Nhiều link "pdf_url" thực chất trả HTML (paywall/landing) -> kiểm tra magic bytes.
    if not body.lstrip()[:5] == b"%PDF-":
        return None
    return body


def extract_text(pdf_bytes: bytes) -> str:
    import pymupdf  # import trễ để app vẫn chạy nếu chưa cài
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            return "\n".join(page.get_text() for page in doc)
    except Exception as exc:  # noqa: BLE001
        logger.info("Không đọc được PDF: %s", exc)
        return ""


def chunk_text(text: str, max_words: int = 500, overlap: int = 60) -> list[str]:
    words = text.split()
    if not words:
        return []
    step = max(max_words - overlap, 1)
    chunks = []
    for i in range(0, len(words), step):
        chunks.append(" ".join(words[i : i + max_words]))
        if i + max_words >= len(words):
            break
    return chunks


def embeddings_enabled() -> bool:
    return os.environ.get("EMBEDDING_PROVIDER", "none").lower() == "openai"


async def embed(texts: list[str]) -> Optional[list[list[float]]]:
    """Trả list vector, hoặc None nếu embedding đang tắt."""
    if not embeddings_enabled():
        return None
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key=os.environ["EMBEDDING_API_KEY"],
        base_url=os.environ.get("EMBEDDING_BASE_URL") or None,
    )
    model = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
    out = await client.embeddings.create(model=model, input=texts)
    return [d.embedding for d in out.data]
