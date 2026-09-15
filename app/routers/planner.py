"""
Module 2 trong kế hoạch: Research Planner.

Luồng:
    Topic (+ tuỳ chọn) → Gemini (ép trả JSON qua response_schema) → ResearchPlan
                        → lưu PostgreSQL → trả về cho client

Điểm quan trọng: KHÔNG prompt LLM trả text tự do rồi tự parse JSON bằng regex.
Dùng response_schema (Pydantic-native structured output) của Gemini SDK để ép
đúng schema — response.parsed trả về thẳng object ResearchPlan đã validate.
"""

import os
import json
import logging

from fastapi import APIRouter, HTTPException
from google import genai
from google.genai import types

from app.models import ResearchPlanRequest, ResearchPlanResponse, ResearchPlan
from app.db import get_pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/plan", tags=["planner"])

# Đọc GEMINI_API_KEY từ env tự động (client cũng nhận qua api_key=... nếu muốn set thủ công)
_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))


async def generate_research_plan(payload: ResearchPlanRequest) -> ResearchPlan:
    """Gọi Gemini Pro, ép trả JSON đúng schema ResearchPlan, trả về object đã validate."""
    model = os.environ.get("GEMINI_MODEL", "gemini-3-pro-preview")

    user_prompt = (
        f"Chủ đề nghiên cứu: {payload.topic}\n"
        f"Ngôn ngữ mong muốn cho tài liệu: {payload.language}\n"
        f"Loại nghiên cứu mong muốn: {payload.study_type or 'không chỉ định'}\n"
        f"Số lượng paper mục tiêu: {payload.target_paper_count}\n"
        f"Journal/conference mục tiêu: {payload.target_venue or 'không chỉ định'}\n\n"
        "Hãy phân tích chủ đề này thành một research plan có cấu trúc: "
        "xác định lĩnh vực nghiên cứu, từ khoá tìm kiếm học thuật (tiếng Anh), "
        "từ đồng nghĩa cho mỗi từ khoá, các câu hỏi nghiên cứu khả thi, "
        "và chiến lược tìm kiếm (nguồn dữ liệu, khoảng thời gian, loại nghiên cứu)."
    )

    try:
        response = await _client.aio.models.generate_content(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ResearchPlan,
            ),
        )
    except Exception as exc:
        logger.error("Lỗi khi gọi Gemini API: %s", exc)
        raise HTTPException(status_code=502, detail="Không gọi được Gemini API.")

    if response.parsed is None:
        logger.error("Gemini không trả JSON đúng schema. Raw text: %s", response.text)
        raise HTTPException(status_code=502, detail="LLM không trả về research plan hợp lệ.")

    return response.parsed


@router.post("", response_model=ResearchPlanResponse)
async def create_plan(payload: ResearchPlanRequest) -> ResearchPlanResponse:
    """Tạo research plan mới từ 1 topic và lưu vào DB."""
    plan = await generate_research_plan(payload)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO research_projects (topic, research_plan)
            VALUES ($1, $2::jsonb)
            RETURNING id;
            """,
            payload.topic,
            json.dumps(plan.model_dump()),
        )

    return ResearchPlanResponse(id=row["id"], plan=plan)


@router.get("/{project_id}", response_model=ResearchPlanResponse)
async def get_plan(project_id: int) -> ResearchPlanResponse:
    """Lấy lại research plan đã tạo trước đó theo id."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, research_plan FROM research_projects WHERE id = $1;",
            project_id,
        )

    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy research project.")

    plan = ResearchPlan.model_validate(json.loads(row["research_plan"]))
    return ResearchPlanResponse(id=row["id"], plan=plan)