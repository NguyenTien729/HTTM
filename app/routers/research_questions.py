"""
Module 8 trong kế hoạch: Research Question Generator.

Luồng:
    Gap (BẮT BUỘC đã 👤 confirmed=true — không sinh RQ từ gap chưa duyệt)
        ↓
    Candidate RQ + Feasibility check + Novelty check (1 lần gọi LLM)
        ↓
    Nếu is_quantitative=true → Variables → Hypotheses (LLM sinh cùng lúc, trong 1 schema)
        ↓
    Lưu bảng `research_questions`

Không tự động chạy cho mọi gap — chỉ chạy khi được gọi tường minh cho 1 gap_id cụ thể,
và gap đó phải confirmed=true. Đây là bước tiếp nối trực tiếp checkpoint human-in-the-loop
đã có ở module Gap Detector.
"""

import json
import logging

from fastapi import APIRouter, HTTPException

from app.models import (
    ResearchQuestion, ResearchQuestionExtraction, GenerateRQResponse,
    Variable, Hypothesis,
)
from app.db import get_pool
from app.services.llm import generate_structured

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research-questions", tags=["rq-generator"])


async def _load_confirmed_gap(gap_id: int) -> dict:
    """Lấy gap theo id — raise 400 nếu chưa confirmed, 404 nếu không tồn tại."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM gaps WHERE id = $1;", gap_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy gap.")
        if not row["confirmed"]:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Gap này chưa được duyệt. Gọi POST /synthesize/{project_id}/gaps/"
                    f"{gap_id}/approve trước khi sinh research question."
                ),
            )
        # Lấy thêm topic/research_domain của project để LLM có bối cảnh khi sinh RQ
        project_row = await conn.fetchrow(
            "SELECT topic, research_plan FROM research_projects WHERE id = $1;", row["project_id"]
        )
    gap = dict(row)
    gap["project_topic"] = project_row["topic"] if project_row else ""
    return gap


def _row_to_research_question(row: dict) -> ResearchQuestion:
    return ResearchQuestion(
        id=row["id"],
        project_id=row["project_id"],
        gap_id=row["gap_id"],
        rq_text=row["rq_text"],
        feasibility_score=row["feasibility_score"],
        feasibility_notes=row["feasibility_notes"],
        novelty_score=row["novelty_score"],
        novelty_notes=row["novelty_notes"],
        is_quantitative=row["is_quantitative"],
        variables=[
            Variable.model_validate(v)
            for v in (json.loads(row["variables"]) if isinstance(row["variables"], str) else row["variables"])
        ],
        hypotheses=[
            Hypothesis.model_validate(h)
            for h in (json.loads(row["hypotheses"]) if isinstance(row["hypotheses"], str) else row["hypotheses"])
        ],
    )


@router.post("/gap/{gap_id}", response_model=GenerateRQResponse)
async def generate_research_question(gap_id: int) -> GenerateRQResponse:
    """Sinh research question từ 1 gap đã được người dùng duyệt (confirmed=true)."""
    gap = await _load_confirmed_gap(gap_id)

    prompt = (
        f"Đề tài nghiên cứu tổng thể: {gap['project_topic']}\n\n"
        f"Research gap đã xác nhận:\n"
        f"- Loại gap: {gap['gap_type']}\n"
        f"- Mô tả: {gap['description']}\n"
        f"- Câu hỏi nghiên cứu gợi ý ban đầu (chỉ tham khảo, không bắt buộc dùng nguyên văn): "
        f"{gap['proposed_research_question']}\n\n"
        "Hãy phát triển gap này thành 1 candidate research question cụ thể, khả thi. "
        "Đánh giá feasibility (có dữ liệu/phương pháp thực hiện được không) và novelty "
        "(có thực sự mới so với gap đã nêu không) theo thang 1-5, kèm giải thích ngắn. "
        "Xác định RQ này có phù hợp thiết kế định lượng không — nếu có, sinh thêm danh sách "
        "biến số (independent/dependent/moderator/mediator/control) và các giả thuyết (H1, H2...) "
        "tương ứng. Nếu là nghiên cứu định tính, để variables và hypotheses rỗng."
    )

    extracted = await generate_structured(prompt, ResearchQuestionExtraction)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO research_questions
                (project_id, gap_id, rq_text, feasibility_score, feasibility_notes,
                 novelty_score, novelty_notes, is_quantitative, variables, hypotheses)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb)
            RETURNING *;
            """,
            gap["project_id"],
            gap_id,
            extracted.candidate_rq,
            extracted.feasibility_score,
            extracted.feasibility_notes,
            extracted.novelty_score,
            extracted.novelty_notes,
            extracted.is_quantitative,
            json.dumps([v.model_dump() for v in extracted.variables]),
            json.dumps([h.model_dump() for h in extracted.hypotheses]),
        )

    rq = _row_to_research_question(dict(row))
    return GenerateRQResponse(project_id=gap["project_id"], gap_id=gap_id, research_question=rq)


@router.get("/project/{project_id}", response_model=list[ResearchQuestion])
async def list_research_questions(project_id: int) -> list[ResearchQuestion]:
    """Lấy toàn bộ research question đã sinh cho 1 project (từ mọi gap đã duyệt)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM research_questions WHERE project_id = $1 ORDER BY id;", project_id
        )
    return [_row_to_research_question(dict(r)) for r in rows]