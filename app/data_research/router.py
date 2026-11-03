"""FastAPI endpoints for the human-in-the-loop Data Research workflow."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .planner_integration import resolve_project_topic
from .service import (
    DataResearchError,
    analyze_dataframe,
    discover_datasets,
    download_dataset_dataframe,
    inspect_dataset,
)

router = APIRouter(prefix="/data-research", tags=["Data Research"])


class DiscoverRequest(BaseModel):
    # Required workflow: discover is always tied to a Research Planner project.
    project_id: str | int = Field(..., description="ID project trong Research Planner; topic được lấy tự động")
    limit: int = Field(default=10, ge=1, le=20)

    # Reject the old manual-topic request instead of silently ignoring it.
    model_config = ConfigDict(extra="forbid")


class InspectRequest(BaseModel):
    dataset_id: str = Field(..., min_length=3, max_length=200)
    # Optional: carry the active Planner project through the workflow without retyping its topic.
    project_id: str | int | None = Field(default=None, description="Tùy chọn; topic được tự lấy từ Research Planner và gắn vào context")
    config: str | None = Field(default=None, max_length=200)
    split: str | None = Field(default=None, max_length=100)


class AnalyzeRequest(BaseModel):
    dataset_id: str = Field(..., min_length=3, max_length=200)
    # If supplied, the topic is automatically loaded from the Planner and injected into Methods/Results metadata.
    project_id: str | int | None = Field(default=None, description="Tùy chọn; tự lấy topic để gắn vào bản nháp Methods/Results")
    config: str = Field(..., min_length=1, max_length=200)
    split: str = Field(..., min_length=1, max_length=100)
    # This explicit confirmation is the human-in-the-loop gate after inspect.
    confirm_dataset: bool = Field(default=False, description="Chỉ true sau khi người dùng đã kiểm tra và chọn dataset")
    max_rows: int = Field(default=50_000, ge=100, le=100_000)
    target_column: str | None = Field(default=None, max_length=200)
    group_column: str | None = Field(default=None, max_length=200)
    predictor_columns: list[str] = Field(default_factory=list, max_length=10)


def _raise_api_error(exc: DataResearchError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/project-topic/{project_id}", summary="Lấy topic nghiên cứu từ Research Planner theo project_id")
async def get_project_topic(project_id: str) -> dict[str, str]:
    """Allow clients to verify the project/topic mapping before discovering datasets."""
    try:
        return await resolve_project_topic(project_id)
    except DataResearchError as exc:
        _raise_api_error(exc)


@router.post("/discover", summary="Tự tìm dataset theo topic của Research Planner")
async def discover(request: DiscoverRequest) -> dict[str, Any]:
    """Always resolve the topic from Research Planner before searching.

    Manual topic input is deliberately unsupported so results cannot drift away
    from the active project selected in the Planner.
    """
    try:
        project_context = await resolve_project_topic(request.project_id)
        topic = project_context["topic"]
        result = await discover_datasets(topic=topic, limit=request.limit)
        result["topic_source"] = "research_planner"
        result["project"] = project_context
        return result
    except DataResearchError as exc:
        _raise_api_error(exc)


@router.post("/inspect", summary="Xem trước dataset, cột, split và các dòng mẫu")
async def inspect(request: InspectRequest) -> dict[str, Any]:
    """Return metadata and a sample so the user can decide whether to approve it."""
    try:
        result = await inspect_dataset(
            dataset_id=request.dataset_id,
            config=request.config,
            split=request.split,
        )
        if request.project_id is not None:
            result["project"] = await resolve_project_topic(request.project_id)
        return result
    except DataResearchError as exc:
        _raise_api_error(exc)


@router.post("/analyze", summary="Thống kê dataset đã được người dùng xác nhận")
async def analyze(request: AnalyzeRequest) -> dict[str, Any]:
    """Download a bounded Parquet split and return statistics plus Methods/Results drafts."""
    if not request.confirm_dataset:
        raise HTTPException(
            status_code=400,
            detail="Chưa được phân tích: hãy gọi /inspect, kiểm tra dataset, rồi gửi lại confirm_dataset=true nếu đồng ý chọn dataset này.",
        )
    try:
        frame, metadata = await download_dataset_dataframe(
            dataset_id=request.dataset_id,
            config=request.config,
            split=request.split,
            max_rows=request.max_rows,
        )
        if request.project_id is not None:
            project_context = await resolve_project_topic(request.project_id)
            metadata["project_id"] = project_context["project_id"]
            metadata["research_topic"] = project_context["topic"]
            metadata["topic_source"] = project_context["topic_source"]
            metadata["project_title"] = project_context.get("project_title", "")
        return analyze_dataframe(
            frame=frame,
            dataset_meta=metadata,
            target_column=request.target_column,
            group_column=request.group_column,
            predictor_columns=request.predictor_columns,
        )
    except DataResearchError as exc:
        _raise_api_error(exc)
