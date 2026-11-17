from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import Hypothesis, Variable
from app.routers.export import PaperAuthor


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartRun(StrictModel):
    topic: str | None = Field(default=None, min_length=5, max_length=1000)
    project_id: int | None = Field(default=None, gt=0)
    region: str | None = Field(default=None, max_length=200)
    target_paper_count: int = Field(default=15, ge=3, le=50)
    max_sources: int = Field(default=6, ge=1, le=10)
    max_rows: int = Field(default=10000, ge=100, le=50000)
    collection_seconds: int = Field(default=180, ge=30, le=600)
    authors: list[PaperAuthor] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def one_input(self):
        if (self.topic is None) == (self.project_id is None):
            raise ValueError("Cung cấp topic hoặc project_id, không đồng thời cả hai.")
        return self


class DataNeed(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,49}$")
    concept: str
    role: Literal["outcome", "exposure", "control", "join_key"]
    unit: str
    resolution: str
    search_terms: list[str] = Field(min_length=1, max_length=8)
    required: bool = True


class ResearchBrief(StrictModel):
    question: str = Field(min_length=10)
    gap: str
    evidence_paper_ids: list[int] = Field(min_length=1)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=6)
    variables: list[Variable] = Field(min_length=1, max_length=15)
    geography: str
    geography_terms: list[str] = Field(default_factory=list, max_length=12)
    time_scope: str
    data_needs: list[DataNeed] = Field(min_length=2, max_length=15)
    feasibility_notes: str

    @model_validator(mode="after")
    def unique_keys(self):
        keys = [n.key for n in self.data_needs]
        if len(keys) != len(set(keys)):
            raise ValueError("Các yêu cầu dữ liệu phải có key riêng.")
        if not any(n.role == "outcome" for n in self.data_needs):
            raise ValueError("Cần có biến kết quả.")
        return self


class Binding(StrictModel):
    variable: str
    column: str
    source_unit: str
    target_unit: str
    transform: Literal["identity", "numeric", "year", "strip", "casefold"] = "identity"
    scale: float = Field(default=1.0, allow_inf_nan=False)
    offset: float = Field(default=0.0, allow_inf_nan=False)
    rationale: str


class SourceFilter(StrictModel):
    column: str
    operator: Literal["eq", "in", "between"]
    values: list[str | float | int] = Field(min_length=1, max_length=30)
    rationale: str


class SourceSelection(StrictModel):
    source_key: str
    bindings: list[Binding] = Field(min_length=1, max_length=20)
    filters: list[SourceFilter] = Field(default_factory=list, max_length=8)


class JoinPlan(StrictModel):
    right_source: str
    keys: list[str] = Field(min_length=1, max_length=4)
    cardinality: Literal["one_to_one", "many_to_one"] = "one_to_one"


class AnalysisDesign(StrictModel):
    adequate: bool
    rationale: str
    missing_requirements: list[str] = Field(default_factory=list)
    sources: list[SourceSelection] = Field(default_factory=list, max_length=3)
    joins: list[JoinPlan] = Field(default_factory=list, max_length=2)
    method: Literal["descriptive", "correlation", "ols", "unsupported"]
    target: str | None = None
    predictors: list[str] = Field(default_factory=list, max_length=10)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    causal_claims_allowed: Literal[False] = False


class Decision(StrictModel):
    expected_version: int = Field(ge=1)
    action: Literal["approve", "revise"]
    notes: str = Field(default="", max_length=4000)
    restart_from: Literal["research_plan", "data_collection", "design", "introduction"] | None = None


class VersionRequest(StrictModel):
    expected_version: int = Field(ge=1)


class FrontMatter(StrictModel):
    title: str
    abstract: str
    keywords: list[str] = Field(min_length=3, max_length=8)


class EvidenceIssue(StrictModel):
    section: str
    claim: str
    reason: str
    severity: Literal["warning", "error"]


class ScientificAudit(StrictModel):
    issues: list[EvidenceIssue] = Field(default_factory=list, max_length=50)
    limitations: list[str] = Field(default_factory=list)
