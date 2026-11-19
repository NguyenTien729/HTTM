from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import Hypothesis, Variable
from app.routers.export import PaperAuthor


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartRun(StrictModel):
    paper_type: Literal["review", "original_research"] = "review"
    topic: str | None = Field(default=None, min_length=5, max_length=1000)
    project_id: int | None = Field(default=None, gt=0)
    region: str | None = Field(default=None, max_length=200)
    target_paper_count: int = Field(default=15, ge=3, le=50)
    max_sources: int = Field(default=8, ge=1, le=10)
    max_rows: int = Field(default=10000, ge=100, le=50000)
    collection_seconds: int = Field(default=240, ge=30, le=600)
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
        outcomes = [n for n in self.data_needs if n.role == "outcome"]
        if not outcomes:
            raise ValueError("Cần có biến kết quả.")
        # The outcome and join keys define whether the approved question is
        # answerable. Controls are desirable covariates, not hard gates, unless
        # the user explicitly revises the plan to make one indispensable.
        # Keep the first exposure as the primary exposure; additional exposure
        # measures are alternatives and should not block an otherwise valid plan.
        primary_outcome = outcomes[0].key
        primary_exposure = next((n.key for n in self.data_needs if n.role == "exposure"), None)
        for need in self.data_needs:
            if need.role == "join_key":
                need.required = True
            elif need.role == "outcome":
                # This analysis pipeline supports one dependent variable.
                # Additional outcomes are alternatives and cannot be hard gates.
                need.required = need.key == primary_outcome
            elif need.role == "control":
                need.required = False
            elif need.role == "exposure" and need.key != primary_exposure:
                need.required = False
        return self


class Binding(StrictModel):
    variable: str
    column: str
    source_unit: str
    target_unit: str
    transform: Literal["identity", "numeric", "log", "year", "strip", "casefold"] = "identity"
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
    restart_from: Literal["research_plan", "data_collection", "design", "introduction", "manuscript", "verification"] | None = None
    # Re-plan the question but keep the already downloaded sources (data-grounded alternatives).
    keep_sources: bool = False


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


class PaperStory(StrictModel):
    thesis: str
    research_gap: str
    contribution_boundary: list[str] = Field(default_factory=list, max_length=10)
    claims_to_make: list[str] = Field(default_factory=list, max_length=15)
    claims_to_avoid: list[str] = Field(default_factory=list, max_length=15)


class ClaimEvidence(StrictModel):
    claim_id: str
    section: str
    claim: str
    status: Literal["supported_literature", "supported_data", "proposed", "unsupported"]
    evidence_paper_ids: list[int] = Field(default_factory=list, max_length=20)
    source_keys: list[str] = Field(default_factory=list, max_length=10)
    rationale: str


class ClaimEvidenceMap(StrictModel):
    claims: list[ClaimEvidence] = Field(default_factory=list, max_length=100)


class ProposalSections(StrictModel):
    introduction: str
    related_work: str
    proposed_methods: str
    data_and_feasibility: str
    limitations: str
    next_steps: str


class ProposalDraft(StrictModel):
    title: str
    abstract: str
    keywords: list[str] = Field(min_length=3, max_length=8)
    sections: ProposalSections
    claim_evidence_map: ClaimEvidenceMap


class ProposalRevision(StrictModel):
    expected_version: int = Field(ge=1)
    instruction: str = Field(min_length=3, max_length=4000)


class ColumnRef(StrictModel):
    source_key: str
    column: str
    concept: str


class AlternativeQuestion(StrictModel):
    question: str = Field(min_length=10)
    rationale: str
    outcome: ColumnRef
    exposures: list[ColumnRef] = Field(min_length=1, max_length=4)
    join_columns: list[str] = Field(default_factory=list, max_length=4)
    scope_change: str


class AlternativeQuestions(StrictModel):
    alternatives: list[AlternativeQuestion] = Field(default_factory=list, max_length=3)
