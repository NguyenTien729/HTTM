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
from pydantic import BaseModel
from typing import Literal

from app.models import ResearchPlanRequest, ResearchPlanResponse, ResearchPlan
from app.db import get_pool
from app.services.llm import generate_structured

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/plan", tags=["planner"])


class PaperTypeChange(BaseModel):
    paper_type: Literal["topic_research", "review", "original_research"]


@router.patch("/{project_id}/paper-type")
async def change_paper_type(project_id: int, request: PaperTypeChange):
    """Switch workflow without deleting source papers or existing manuscripts."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("UPDATE research_projects SET paper_type=$2 WHERE id=$1 RETURNING id, paper_type", project_id, request.paper_type)
    if not row:
        raise HTTPException(404, "Không tìm thấy research project.")
    return dict(row)


async def generate_research_plan(payload: ResearchPlanRequest) -> ResearchPlan:
    """Gọi LLM, ép trả JSON đúng schema ResearchPlan, trả về object đã validate."""
    prompt = (
        f"Chủ đề nghiên cứu: {payload.topic}\n"
        f"Ngôn ngữ mong muốn cho tài liệu: {payload.language}\n"
        f"Loại nghiên cứu mong muốn: {payload.study_type or 'không chỉ định'}\n"
        f"Loại bản thảo: {payload.paper_type}. Nếu review: lập kế hoạch tổng quan bằng chứng, "
        "không yêu cầu khảo sát/dataset mới; các study_types là loại tài liệu được tìm, không giới hạn chỉ tìm bài review.\n"
        "Nếu topic_research: nghiên cứu dữ liệu thứ cấp bám đúng topic, không phát hiện gap, "
        "không chuyển sang câu hỏi từ khoảng trống, không khẳng định đã tạo kết quả hoặc giả thuyết mới.\n"
        f"Số lượng paper mục tiêu: {payload.target_paper_count}\n"
        f"Journal/conference mục tiêu: {payload.target_venue or 'không chỉ định'}\n"
        f"Quốc gia/khu vực bắt buộc: {payload.region or 'không chỉ định (suy ra từ topic nếu có)'}\n\n"
        "Hãy phân tích chủ đề này thành một research plan có cấu trúc: "
        "xác định lĩnh vực nghiên cứu, từ khoá tìm kiếm học thuật (tiếng Anh), "
        "từ đồng nghĩa cho mỗi từ khoá, các câu hỏi nghiên cứu khả thi, "
        "và chiến lược tìm kiếm (nguồn dữ liệu, khoảng thời gian, loại nghiên cứu). "
        "Nếu topic hoặc trường region nêu quốc gia/khu vực, hãy điền region bằng tên tiếng Anh, "
        "region_terms bằng tên nước, biến thể tên, tính từ quốc tịch và thành phố/vùng liên quan, "
        "country_codes bằng mã ISO alpha-2. Nếu không có giới hạn khu vực thì để region=null, "
        "region_terms=[], country_codes=[]."
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
            INSERT INTO research_projects (topic, research_plan, paper_type)
            VALUES ($1, $2::jsonb, $3)
            RETURNING id;
            """,
            payload.topic,
            json.dumps(plan.model_dump()),
            payload.paper_type,
        )

    return ResearchPlanResponse(id=row["id"], plan=plan, paper_type=payload.paper_type)


@router.get("/{project_id}", response_model=ResearchPlanResponse)
async def get_plan(project_id: int) -> ResearchPlanResponse:
    """Lấy lại research plan đã tạo trước đó theo id."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, research_plan, paper_type FROM research_projects WHERE id = $1;",
            project_id,
        )

    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy research project.")

    value = row["research_plan"]
    plan = ResearchPlan.model_validate(json.loads(value) if isinstance(value, str) else value)
    return ResearchPlanResponse(id=row["id"], plan=plan, paper_type=row["paper_type"])
