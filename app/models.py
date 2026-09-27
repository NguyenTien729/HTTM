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
    """Dùng list-of-object thay vì dict với key tự do, vì cấu trúc object/array
    được LLM tuân theo ổn định hơn dict với key tự do khi ép structured output."""
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


# ---- Module Paper Analyst rút gọn (chỉ dựa trên abstract, chưa đọc full-text PDF) ----

class PaperAnalysisExtraction(BaseModel):
    """Schema dùng riêng để ép LLM trả JSON khi phân tích 1 abstract.
    Không có id/paper_id/project_id — những field đó do code tự gắn sau,
    không để LLM tự sinh (tránh LLM bịa ID)."""
    method: Optional[str] = Field(default=None, description="Phương pháp nghiên cứu, ví dụ: 'Survey', 'Experiment'. Null nếu abstract không nêu rõ.")
    population: Optional[str] = Field(default=None, description="Đối tượng/mẫu nghiên cứu, ví dụ: 'University students, N=523'. Null nếu không rõ.")
    context: Optional[str] = Field(default=None, description="Bối cảnh/quốc gia/lĩnh vực nếu abstract có nêu. Null nếu không rõ.")
    key_finding: str = Field(description="Phát hiện chính của nghiên cứu, tóm tắt 1-2 câu.")
    limitation: Optional[str] = Field(default=None, description="Hạn chế của nghiên cứu nếu abstract có đề cập. Null nếu không rõ.")


class PaperAnalysis(BaseModel):
    """Kết quả phân tích 1 paper — khớp cột bảng `paper_analyses`."""
    id: Optional[int] = None
    paper_id: int
    project_id: int
    method: Optional[str] = None
    population: Optional[str] = None
    context: Optional[str] = None
    key_finding: str
    limitation: Optional[str] = None


class AnalyzeResponse(BaseModel):
    project_id: int
    analyzed_now: int          # số paper vừa phân tích trong lần gọi này
    total_analyzed: int        # tổng số paper đã có phân tích (kể cả trước đó)
    analyses: List[PaperAnalysis]


# ---- Module Literature Synthesis: gom cụm theme từ các paper đã phân tích ----

class ThemeExtraction(BaseModel):
    name: str = Field(description="Tên ngắn gọn của theme/hướng phát hiện, ví dụ: 'AI improves productivity'")
    description: str = Field(description="Mô tả 1-2 câu về theme này.")
    paper_ids: List[int] = Field(description="Danh sách paper_id thuộc theme này — PHẢI lấy đúng từ input, không được bịa số.")


class ThemesExtraction(BaseModel):
    themes: List[ThemeExtraction]


class Theme(BaseModel):
    id: Optional[int] = None
    project_id: int
    name: str
    description: str
    paper_ids: List[int]


class SynthesizeResponse(BaseModel):
    project_id: int
    themes: List[Theme]


# ---- Module Research Gap Detector ----

class GapExtraction(BaseModel):
    gap_type: str = Field(
        description="Một trong: population, method, context, theoretical, dataset, contradiction, temporal"
    )
    description: str = Field(description="Mô tả gap: bằng chứng quan sát được + khoảng trống tiềm năng.")
    evidence_paper_ids: List[int] = Field(description="paper_id làm bằng chứng — PHẢI lấy đúng từ input.")
    proposed_research_question: str


class GapsExtraction(BaseModel):
    gaps: List[GapExtraction]


class Gap(BaseModel):
    id: Optional[int] = None
    project_id: int
    gap_type: str
    description: str
    evidence_paper_ids: List[int]
    proposed_research_question: str
    confirmed: bool = False   # 👤 human approval checkpoint — mặc định chưa duyệt


class GapsResponse(BaseModel):
    project_id: int
    gaps: List[Gap]


# ---- Module Research Question Generator ----
# Chỉ chạy trên gap ĐÃ 👤 confirmed=true — không sinh RQ từ gap chưa được người duyệt.

class VariableExtraction(BaseModel):
    name: str
    role: str = Field(description="Một trong: independent, dependent, moderator, mediator, control")
    description: str


class HypothesisExtraction(BaseModel):
    label: str = Field(description="Ví dụ: 'H1', 'H2'")
    statement: str = Field(description="Phát biểu giả thuyết đầy đủ, ví dụ: 'AI literacy làm giảm tác động tiêu cực của AI usage lên academic performance.'")
    independent_variable: str
    dependent_variable: str


class ResearchQuestionExtraction(BaseModel):
    """Schema ép LLM trả JSON khi sinh RQ từ 1 gap đã duyệt."""
    candidate_rq: str = Field(description="Câu hỏi nghiên cứu cụ thể, khả thi, trả lời được bằng dữ liệu thực tế.")
    feasibility_score: int = Field(ge=1, le=5, description="1=rất khó thực hiện, 5=rất khả thi (dữ liệu/phương pháp sẵn có).")
    feasibility_notes: str = Field(description="Giải thích ngắn cho điểm feasibility — cần dataset gì, phương pháp gì.")
    novelty_score: int = Field(ge=1, le=5, description="1=trùng lặp nhiều nghiên cứu đã có, 5=rất mới so với literature đã thấy.")
    novelty_notes: str = Field(description="Giải thích ngắn cho điểm novelty, dựa trên gap đã cho.")
    is_quantitative: bool = Field(description="RQ này có phù hợp thiết kế định lượng (đo lường biến số, test giả thuyết) không.")
    variables: List[VariableExtraction] = Field(
        default_factory=list, description="Chỉ điền nếu is_quantitative=true. Để rỗng nếu định tính."
    )
    hypotheses: List[HypothesisExtraction] = Field(
        default_factory=list, description="Chỉ điền nếu is_quantitative=true. Để rỗng nếu định tính."
    )


class Variable(BaseModel):
    name: str
    role: str
    description: str


class Hypothesis(BaseModel):
    label: str
    statement: str
    independent_variable: str
    dependent_variable: str


class ResearchQuestion(BaseModel):
    """Kết quả sinh RQ — khớp cột bảng `research_questions`."""
    id: Optional[int] = None
    project_id: int
    gap_id: int
    rq_text: str
    feasibility_score: int
    feasibility_notes: str
    novelty_score: int
    novelty_notes: str
    is_quantitative: bool
    variables: List[Variable] = Field(default_factory=list)
    hypotheses: List[Hypothesis] = Field(default_factory=list)


class GenerateRQResponse(BaseModel):
    project_id: int
    gap_id: int
    research_question: ResearchQuestion