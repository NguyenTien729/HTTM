"""
Schema cho module Research Planner (V1).
LLM luôn phải trả về đúng ResearchPlan (ép bằng tool-calling ở planner.py),
không được trả văn bản tự do.
"""

from typing import List, Optional
from pydantic import BaseModel, Field


class ResearchPlanRequest(BaseModel):
    topic: str = Field(..., description="Chủ đề nghiên cứu, ví dụ: 'Tác động của AI đến năng suất sinh viên'")
    language: str = Field(default="vi", description="Ngôn ngữ mong muốn cho papers: vi/en/...")
    study_type: Optional[str] = Field(
        default=None, description="Loại nghiên cứu mong muốn, ví dụ: 'survey', 'experiment', 'review'"
    )
    target_paper_count: int = Field(default=50, ge=1, le=500)
    target_venue: Optional[str] = Field(
        default=None, description="Journal/conference mục tiêu, ví dụ: 'Computers & Education'"
    )


class SearchStrategy(BaseModel):
    databases: List[str]
    date_range: str
    language: List[str]
    study_types: List[str]
    target_paper_count: int


class KeywordSynonyms(BaseModel):
    """Dùng list-of-object thay vì dict với key tự do, vì Gemini structured
    output (response_schema) hỗ trợ object/array ổn định hơn dict linh hoạt."""
    keyword: str
    synonyms: List[str]


class ResearchPlan(BaseModel):
    topic: str
    research_domain: str
    keywords: List[str]
    synonyms: List[KeywordSynonyms]
    research_questions: List[str]
    search_strategy: SearchStrategy


class ResearchPlanResponse(BaseModel):
    id: int
    plan: ResearchPlan


# ---- Module Literature Search (V1→V2) ----

class Paper(BaseModel):
    """Metadata 1 paper — khớp cột trong bảng `papers`."""
    id: Optional[int] = None
    project_id: int
    title: str
    authors: List[str] = Field(default_factory=list)
    year: Optional[int] = None
    doi: Optional[str] = None
    abstract: Optional[str] = None
    venue: Optional[str] = None
    url: Optional[str] = None
    source: str = "openalex"
    citation_count: int = 0


class SearchResponse(BaseModel):
    project_id: int
    query_used: str
    found: int          # tổng số kết quả thô từ OpenAlex
    saved: int          # số paper mới thực sự lưu (sau dedup)
    papers: List[Paper]