"""Strict, evidence-grounded topic screening for literature search.

LLM first decomposes the user's topic into required concepts. Each paper must
have verifiable title/abstract evidence for EVERY required concept and their
relationship. Any missing/invalid decision is rejected (fail closed).
"""
import logging
import os
import re
from pydantic import BaseModel, Field
from app.models import Paper, ResearchPlan
from app.services.llm import generate_structured

logger = logging.getLogger(__name__)


class TopicConcepts(BaseModel):
    concepts: list[str] = Field(description="2-4 mandatory concepts, not synonyms; preserve the topic's actual scope")
    scope_rule: str = Field(description="One sentence specifying what direct relationship between concepts is required")


class ConceptEvidence(BaseModel):
    concept: str
    quote: str = Field(description="Exact contiguous excerpt from candidate title or abstract; empty if unsupported")
    supported: bool


class RelevanceDecision(BaseModel):
    candidate_id: int
    concept_evidence: list[ConceptEvidence]
    direct_relationship: bool
    relationship_quote: str = Field(description="Exact title/abstract excerpt demonstrating the relationship, empty if unsupported")
    score: int = Field(ge=0, le=100)
    reason: str


class RelevanceBatch(BaseModel):
    decisions: list[RelevanceDecision]


def _normalized(s: str) -> str:
    return re.sub(r"\s+", " ", s).casefold().strip()


def _valid_quote(quote: str, paper: Paper) -> bool:
    q = _normalized(quote)
    if len(q) < 12:
        return False
    return any(q in _normalized(t) for t in (paper.title or "", paper.abstract or ""))


async def extract_topic_concepts(plan: ResearchPlan) -> TopicConcepts:
    """Use original topic, not broad LLM-generated planner questions, as authority."""
    prompt = (
        "Decompose the research topic into 2-4 DISTINCT mandatory semantic concepts. "
        "Do not add constraints absent from the original topic. Do not turn synonyms into separate concepts. "
        "For an application-of-X-in-Y topic, require BOTH X and Y and evidence that X is APPLIED TO Y. "
        "Related background work about only X or only Y is NOT relevant. "
        "If the topic has a specified geography, that geography may be an additional concept. "
        "Return concise concepts and a strict scope_rule.\n"
        f"TOPIC: {plan.topic}\n"
    )
    result = await generate_structured(prompt, TopicConcepts)
    concepts = [c.strip() for c in result.concepts if c.strip()]
    if not 2 <= len(concepts) <= 4 or len({_normalized(c) for c in concepts}) != len(concepts):
        raise ValueError("Topic concept extraction did not produce 2-4 distinct concepts")
    return TopicConcepts(concepts=concepts, scope_rule=result.scope_rule)


def _approve(d: RelevanceDecision, paper: Paper, concepts: TopicConcepts, threshold: int) -> bool:
    if d.score < threshold or not d.direct_relationship or not _valid_quote(d.relationship_quote, paper):
        return False
    expected = {_normalized(c) for c in concepts.concepts}
    actual = [_normalized(e.concept) for e in d.concept_evidence]
    if len(actual) != len(expected) or set(actual) != expected or len(set(actual)) != len(actual):
        return False
    return all(e.supported and _valid_quote(e.quote, paper) for e in d.concept_evidence)


async def screen_papers(
    papers: list[Paper], plan: ResearchPlan, concepts: TopicConcepts | None = None,
    threshold: int | None = None, batch_size: int = 10,
) -> list[tuple[Paper, int]]:
    if not papers:
        return []
    concepts = concepts or await extract_topic_concepts(plan)
    threshold = int(os.getenv("SEARCH_RELEVANCE_THRESHOLD", "80")) if threshold is None else threshold
    threshold = max(0, min(100, threshold))
    accepted = []
    for offset in range(0, len(papers), batch_size):
        batch = papers[offset:offset + batch_size]
        entries = "\n\n".join(
            f"CANDIDATE_ID={offset+i}\nTITLE: {p.title}\nABSTRACT: {(p.abstract or '[missing]')[:3500]}"
            for i, p in enumerate(batch)
        )
        prompt = (
            "You are a STRICT scientific literature screener. Assess the ORIGINAL research topic, "
            "not broad background relevance. A candidate must explicitly address ALL mandatory concepts "
            "AND their DIRECT relationship. For each concept, provide an EXACT contiguous quote from "
            "the supplied title or abstract that supports it. Also provide an EXACT quote demonstrating "
            "the direct relationship. Quotes must be copied verbatim, not paraphrased or invented. "
            "If the evidence is missing, set supported=false or direct_relationship=false and use an empty quote. "
            "For application-of-X-in-Y topics, a study about X alone or Y alone MUST be rejected. "
            "No generic background studies. If abstract is missing, use title only. "
            "Score 0-100 heuristically; use the same candidate IDs; one decision per candidate. "
            "Never approve merely because of broad keyword overlap.\n\n"
            f"TOPIC: {plan.topic}\nMANDATORY CONCEPTS: {concepts.concepts}\n"
            f"DIRECT RELATIONSHIP REQUIRED: {concepts.scope_rule}\n\nCANDIDATES:\n{entries}"
        )
        result = await generate_structured(prompt, RelevanceBatch)
        seen = set()
        for d in result.decisions:
            i = d.candidate_id - offset
            if d.candidate_id in seen or not 0 <= i < len(batch):
                continue
            seen.add(d.candidate_id)
            p = batch[i]
            if _approve(d, p, concepts, threshold):
                accepted.append((p, d.score))
            else:
                logger.info("REJECT id=%d score=%d title=%r reason=%s", d.candidate_id, d.score, p.title, d.reason)
        if len(seen) < len(batch):
            logger.warning("REJECT missing relevance decisions: %d", len(batch) - len(seen))
    return accepted
