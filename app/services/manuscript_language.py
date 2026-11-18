"""Translate manuscript headings, never original reference titles or identifiers."""
import logging
import re
from pydantic import BaseModel, Field
from .vietnamese_writing import vietnamese_prose

logger = logging.getLogger(__name__)
_HEADING_CACHE = {}


async def generate_structured(prompt, schema):
    from .llm import generate_structured as call
    return await call(prompt, schema)
ENGLISH_HEADING = re.compile(r"\b(?:of|the|and|as|side|usage|improves|learning|outcomes|positive|negative|mediating|moderating|mechanisms|conditions|challenges|inequalities|privacy|literacy|self.efficacy|engagement|achievement|intensity|integrity|management|compulsive|distress|technostress|overdependence|mismatch|support|year|regulation)\b", re.I)


class Headings(BaseModel):
    titles: list[str] = Field(max_length=60)


def safe_heading(text, index=1):
    translated = vietnamese_prose(text)
    return f"Chủ đề tổng hợp {index}" if ENGLISH_HEADING.search(translated) else translated


async def translate_headings(titles):
    if not any(ENGLISH_HEADING.search(t) for t in titles):
        return [vietnamese_prose(t) for t in titles]
    key = tuple(titles)
    if key in _HEADING_CACHE:
        return list(_HEADING_CACHE[key])
    try:
        result = await generate_structured(
            "Dịch các TIÊU ĐỀ mục sau sang tiếng Việt học thuật tự nhiên, đầy đủ và liền mạch. "
            "Giữ nguyên ý, thứ tự và số lượng; không dịch từng từ rời rạc hoặc chèn tiếng Anh. "
            "Giữ các ký hiệu chuẩn AI, OLS, GPA và tên riêng cần thiết. Không thêm kết quả nghiên cứu.\n"
            + "\n".join(f"{i+1}. {title}" for i, title in enumerate(titles)), Headings)
        if len(result.titles) == len(titles):
            translated = [safe_heading(t, i+1) for i, t in enumerate(result.titles)]
            if len(_HEADING_CACHE) >= 128:
                _HEADING_CACHE.pop(next(iter(_HEADING_CACHE)))
            _HEADING_CACHE[key] = translated
            return list(translated)
    except Exception as exc:
        logger.warning("Heading translation unavailable: %s", type(exc).__name__)
    return [safe_heading(t, i+1) for i, t in enumerate(titles)]
