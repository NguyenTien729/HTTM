"""FastAPI endpoints for the human-in-the-loop Data Research workflow."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .service import (
    DataResearchError,
    analyze_dataframe,
    discover_datasets,
    download_dataset_dataframe,
    inspect_dataset,
)

router = APIRouter(prefix="/data-research", tags=["Data Research"])


class DiscoverRequest(BaseModel):
    topic: str = Field(..., min_length=3, max_length=300, description="Chủ đề nghiên cứu cần tìm dataset")
    limit: int = Field(default=10, ge=1, le=20)


class InspectRequest(BaseModel):
    dataset_id: str = Field(..., min_length=3, max_length=200)
    config: str | None = Field(default=None, max_length=200)
    split: str | None = Field(default=None, max_length=100)


class AnalyzeRequest(BaseModel):
    dataset_id: str = Field(..., min_length=3, max_length=200)
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


@router.post("/discover", summary="Tự tìm dataset công khai theo chủ đề nghiên cứu")
async def discover(request: DiscoverRequest) -> dict[str, Any]:
    """Search Hugging Face Hub and return candidate datasets for a human to review."""
    try:
        return await discover_datasets(topic=request.topic, limit=request.limit)
    except DataResearchError as exc:
        _raise_api_error(exc)


@router.post("/inspect", summary="Xem trước dataset, cột, split và các dòng mẫu")
async def inspect(request: InspectRequest) -> dict[str, Any]:
    """Return metadata and a sample so the user can decide whether to approve it."""
    try:
        return await inspect_dataset(
            dataset_id=request.dataset_id,
            config=request.config,
            split=request.split,
        )
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
        return analyze_dataframe(
            frame=frame,
            dataset_meta=metadata,
            target_column=request.target_column,
            group_column=request.group_column,
            predictor_columns=request.predictor_columns,
        )
    except DataResearchError as exc:
        _raise_api_error(exc)
