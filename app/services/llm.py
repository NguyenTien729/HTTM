"""
Helper dùng chung để gọi LLM với structured output.

LỊCH SỬ THIẾT KẾ (quan trọng để hiểu tại sao code như hiện tại):
Ban đầu dùng function-calling (tool_choice ép gọi 1 tool) để ép JSON theo schema.
Nhưng DeepSeek có bug đã biết: khi dùng strict mode cho function-calling, JSON trả
về trong function.arguments đôi khi bị lỗi cú pháp (thiếu dấu ngoặc kép quanh string,
literal kiểu Python Null/True/False thay vì null/true/false...).
→ Đổi sang JSON mode (response_format={"type": "json_object"}) — theo đúng khuyến
nghị của DeepSeek, đảm bảo output LUÔN là JSON hợp lệ về mặt cú pháp. Việc khớp đúng
schema (field name, type) được đảm bảo bằng cách nhúng schema vào system prompt +
Pydantic validate sau khi parse, KHÔNG dựa vào cơ chế ép cứng nào của API.
"""

import os
import re
import json
import logging
from typing import TypeVar, Type

from fastapi import HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)

T = TypeVar("T", bound=BaseModel)

# Lớp phòng thủ thêm: nếu model vẫn lỡ trả literal kiểu Python (Null/True/False viết
# hoa) thay vì null/true/false chuẩn JSON, tự sửa trước khi parse. Chỉ thay khi các
# từ này đứng ở VỊ TRÍ VALUE (ngay sau : , [ ) để không đụng vào text bên trong string.
_PY_LITERAL_FIX_RE = re.compile(r'([:,\[]\s*)(Null|None|True|False)\b')
_PY_LITERAL_FIX_MAP = {"Null": "null", "None": "null", "True": "true", "False": "false"}


def _fix_python_style_literals(raw: str) -> str:
    return _PY_LITERAL_FIX_RE.sub(
        lambda m: m.group(1) + _PY_LITERAL_FIX_MAP[m.group(2)], raw
    )


def _parse_json_with_fallback(raw: str) -> dict:
    """Parse JSON, thử tự sửa literal kiểu Python nếu lần parse đầu fail."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        fixed = json.loads(_fix_python_style_literals(raw))
        logger.warning("LLM trả JSON lẫn literal kiểu Python, đã tự sửa. raw=%s", raw)
        return fixed
    except json.JSONDecodeError as exc:
        logger.error("Không parse được JSON từ LLM: %s | raw=%s", exc, raw)
        raise HTTPException(status_code=502, detail="LLM trả về JSON không hợp lệ.")


async def generate_structured(prompt: str, schema: Type[T]) -> T:
    """Gọi DeepSeek ở JSON mode, ép trả JSON đúng `schema`, trả về object đã validate.
    Thử tối đa 2 lần nếu lần đầu content rỗng (lỗi thỉnh thoảng gặp của DeepSeek JSON mode)."""
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
    json_schema = schema.model_json_schema()

    system_prompt = (
        "Bạn là hệ thống trích xuất dữ liệu có cấu trúc. Luôn trả lời bằng ĐÚNG MỘT object JSON "
        "hợp lệ — không kèm giải thích, không markdown code fence, không có bất kỳ ký tự nào "
        "ngoài JSON. Mọi giá trị kiểu chuỗi PHẢI nằm trong dấu ngoặc kép. Field không có dữ liệu "
        "thì để giá trị null (chữ thường), KHÔNG viết văn bản giải thích thay cho null. "
        "JSON trả về phải khớp đúng JSON Schema sau:\n\n"
        f"{json.dumps(json_schema, ensure_ascii=False)}"
    )

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = await _client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception as exc:
            logger.error("Lỗi khi gọi DeepSeek API (lần %d): %s", attempt + 1, exc)
            last_error = exc
            continue

        content = response.choices[0].message.content
        if not content:
            logger.warning("DeepSeek trả content rỗng (lần %d), thử lại...", attempt + 1)
            last_error = RuntimeError("DeepSeek trả content rỗng")
            continue

        parsed_args = _parse_json_with_fallback(content)
        try:
            return schema.model_validate(parsed_args)
        except Exception as exc:
            logger.error(
                "Kết quả DeepSeek không khớp schema %s: %s | parsed=%s",
                schema.__name__, exc, parsed_args,
            )
            raise HTTPException(status_code=502, detail="LLM trả về JSON không đúng schema.")

    logger.error("DeepSeek API thất bại sau 2 lần thử: %s", last_error)
    raise HTTPException(status_code=502, detail="Không gọi được DeepSeek API.")