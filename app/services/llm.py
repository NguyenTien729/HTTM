"""
Helper dùng chung để gọi LLM với structured output.

LỊCH SỬ THIẾT KẾ (quan trọng để hiểu tại sao code như hiện tại):
1. Ban đầu dùng function-calling (tool_choice ép gọi 1 tool). DeepSeek có bug đã biết:
   strict mode cho function-calling đôi khi trả JSON lỗi cú pháp.
   → Đổi sang JSON mode (response_format={"type": "json_object"}).
2. Với output dài (nhiều đoạn văn, nhiều theme — như Related Work), nếu không giới hạn
   max_tokens rõ ràng, response dễ bị cắt cụt giữa chừng → JSON không đóng ngoặc → parse
   lỗi. → Set max_tokens rộng rãi + phát hiện finish_reason="length" để biết chắc là do
   bị cắt, không phải model tự sinh sai.
3. Retry nhiều lần cho MỌI loại lỗi tạm thời (content rỗng, JSON hỏng, bị cắt cụt) —
   không raise ngay ở lần thử đầu tiên.
"""

import os
import re
import json
import logging
from typing import TypeVar, Type, Optional

from fastapi import HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)

T = TypeVar("T", bound=BaseModel)

_MAX_ATTEMPTS = 3
_MAX_TOKENS = 8000  # rộng rãi cho output dài kiểu Related Work (nhiều theme/đoạn văn)

# Lớp phòng thủ thêm: nếu model vẫn lỡ trả literal kiểu Python (Null/True/False viết
# hoa) thay vì null/true/false chuẩn JSON, tự sửa trước khi parse. Chỉ thay khi các
# từ này đứng ở VỊ TRÍ VALUE (ngay sau : , [ ) để không đụng vào text bên trong string.
_PY_LITERAL_FIX_RE = re.compile(r'([:,\[]\s*)(Null|None|True|False)\b')
_PY_LITERAL_FIX_MAP = {"Null": "null", "None": "null", "True": "true", "False": "false"}


def _fix_python_style_literals(raw: str) -> str:
    return _PY_LITERAL_FIX_RE.sub(
        lambda m: m.group(1) + _PY_LITERAL_FIX_MAP[m.group(2)], raw
    )


def _try_parse_json(raw: str) -> Optional[dict]:
    """Thử parse JSON, có fallback tự sửa literal kiểu Python. Trả None nếu vẫn fail
    (KHÔNG raise ở đây — để generate_structured tự quyết định retry hay bỏ cuộc)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        fixed = json.loads(_fix_python_style_literals(raw))
        logger.warning("LLM trả JSON lẫn literal kiểu Python, đã tự sửa. raw=%s", raw[:2000])
        return fixed
    except json.JSONDecodeError as exc:
        logger.warning("Parse JSON thất bại: %s | raw=%s", exc, raw[:2000])
        return None


async def generate_structured(prompt: str, schema: Type[T]) -> T:
    """Gọi DeepSeek ở JSON mode, ép trả JSON đúng `schema`, trả về object đã validate.
    Retry tối đa _MAX_ATTEMPTS lần cho MỌI lỗi tạm thời (content rỗng, bị cắt cụt do
    hết max_tokens, JSON hỏng cú pháp, không khớp schema) — chỉ raise sau khi hết lượt thử."""
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
    json_schema = schema.model_json_schema()

    system_prompt = (
        "Bạn là hệ thống trích xuất dữ liệu có cấu trúc. Luôn trả lời bằng ĐÚNG MỘT object JSON "
        "hợp lệ — không kèm giải thích, không markdown code fence, không có bất kỳ ký tự nào "
        "ngoài JSON. Mọi giá trị kiểu chuỗi PHẢI nằm trong dấu ngoặc kép. Field không có dữ liệu "
        "thì để giá trị null (chữ thường), KHÔNG viết văn bản giải thích thay cho null. "
        "Viết NGẮN GỌN, súc tích — ưu tiên trả JSON đầy đủ và ĐÓNG NGOẶC ĐÚNG hơn là dài dòng. "
        "JSON trả về phải khớp đúng JSON Schema sau:\n\n"
        f"{json.dumps(json_schema, ensure_ascii=False)}"
    )

    last_error_detail = "Không rõ nguyên nhân."

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = await _client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                max_tokens=_MAX_TOKENS,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception as exc:
            logger.error("Lỗi khi gọi DeepSeek API (lần %d/%d): %s", attempt, _MAX_ATTEMPTS, exc)
            last_error_detail = f"Lỗi gọi API: {exc}"
            continue

        choice = response.choices[0]
        content = choice.message.content

        if choice.finish_reason == "length":
            logger.warning(
                "DeepSeek bị cắt cụt output do hết max_tokens=%d (lần %d/%d). "
                "Nội dung có thể không đủ để tạo JSON hợp lệ.",
                _MAX_TOKENS, attempt, _MAX_ATTEMPTS,
            )
            last_error_detail = "Output bị cắt cụt do vượt quá max_tokens."
            continue

        if not content:
            logger.warning("DeepSeek trả content rỗng (lần %d/%d), thử lại...", attempt, _MAX_ATTEMPTS)
            last_error_detail = "DeepSeek trả về nội dung rỗng."
            continue

        parsed_args = _try_parse_json(content)
        if parsed_args is None:
            last_error_detail = "JSON trả về không hợp lệ về cú pháp."
            continue

        try:
            return schema.model_validate(parsed_args)
        except Exception as exc:
            logger.warning(
                "Kết quả DeepSeek không khớp schema %s (lần %d/%d): %s | parsed=%s",
                schema.__name__, attempt, _MAX_ATTEMPTS, exc, str(parsed_args)[:1000],
            )
            last_error_detail = f"JSON không khớp schema {schema.__name__}: {exc}"
            continue

    logger.error("DeepSeek thất bại sau %d lần thử. Lỗi cuối: %s", _MAX_ATTEMPTS, last_error_detail)
    raise HTTPException(
        status_code=502,
        detail=f"Không lấy được kết quả hợp lệ từ DeepSeek sau {_MAX_ATTEMPTS} lần thử. "
               f"Lỗi cuối: {last_error_detail}",
    )