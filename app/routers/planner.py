"""
Module 2 trong kế hoạch: Research Planner.

Luồng:
    Topic (+ tuỳ chọn) → LLM (ép trả JSON qua generate_structured) → ResearchPlan
                        → lưu PostgreSQL → trả về cho client

Điểm quan trọng: KHÔNG prompt LLM trả text tự do rồi tự parse JSON bằng regex.
Dùng chung helper generate_structured() (app/services/llm.py) để ép đúng schema —
đổi model provider (Claude/Gemini/DeepSeek...) chỉ cần sửa 1 chỗ duy nhất ở đó.
"""

import json
import logging

from fastapi import APIRouter, HTTPException

from app.models import ResearchPlanRequest, ResearchPlanResponse, ResearchPlan
from app.db import get_pool
from app.services.llm import generate_structured

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/plan", tags=["planner"])


async def generate_research_plan(payload: ResearchPlanRequest) -> ResearchPlan:
    """Gọi LLM, ép trả JSON đúng schema ResearchPlan, trả về object đã validate."""
    prompt = (
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
    return await generate_structured(prompt, ResearchPlan)


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