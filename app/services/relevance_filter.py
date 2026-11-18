"""Evidence-grounded relevance screening for literature search.

The screen favors recall while still requiring an exact excerpt that supports
the paper's direct relevance. Planner concept labels are guidance, not literal
phrases every abstract must contain.
"""
import asyncio
import logging
import os
import re
from pydantic import BaseModel, Field, field_validator
from app.models import Paper, ResearchPlan
from app.services.llm import generate_structured

logger = logging.getLogger(__name__)


class TopicConcepts(BaseModel):
    concepts: list[str] = Field(description="2-4 central concepts, not synonyms; preserve the topic's actual scope")
    scope_rule: str = Field(description="One sentence explaining the direct relevance expected, without adding constraints")


def _none_as_empty(value):
    # Models sometimes return null for "no supporting excerpt". Treat it as the
    # documented empty string, which _valid_quote rejects, so screening stays strict.
    return "" if value is None else value


class ConceptEvidence(BaseModel):
    concept: str
    quote: str = Field(description="Exact contiguous excerpt from candidate title or abstract; empty if unsupported")
    supported: bool

    _quote = field_validator("quote", mode="before")(_none_as_empty)


class RelevanceDecision(BaseModel):
    candidate_id: int
    concept_evidence: list[ConceptEvidence]
    direct_relationship: bool
    relationship_quote: str = Field(description="Exact title/abstract excerpt demonstrating the relationship, empty if unsupported")
    score: int = Field(ge=0, le=100)
    reason: str

    _empty = field_validator("relationship_quote", "reason", mode="before")(_none_as_empty)


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
        f"TOPIC: {plan.topic}\nUSER-SPECIFIED GEOGRAPHY: {plan.region or 'none'}\n"
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
    if len(set(actual)) != len(actual) or not set(actual).issubset(expected):
        return False
    # Do not reject an otherwise well-grounded paper because an abstract uses a
    # field-specific synonym for one planner label. The exact relationship
    # excerpt remains mandatory, plus at least one independently supported core
    # concept excerpt.
    return any(e.supported and _valid_quote(e.quote, paper) for e in d.concept_evidence)


async def screen_papers(
    papers: list[Paper], plan: ResearchPlan, concepts: TopicConcepts | None = None,
    threshold: int | None = None, batch_size: int = 10,
) -> list[tuple[Paper, int]]:
    if not papers:
        return []
    concepts = concepts or await extract_topic_concepts(plan)
    threshold = int(os.getenv("SEARCH_RELEVANCE_THRESHOLD", "65")) if threshold is None else threshold
    threshold = max(0, min(100, threshold))
    concurrency = max(1, int(os.getenv("SEARCH_SCREEN_CONCURRENCY", "4")))
    slots = asyncio.Semaphore(concurrency)

    async def screen_batch(offset):
        batch = papers[offset:offset + batch_size]
        entries = "\n\n".join(
            f"CANDIDATE_ID={offset+i}\nTITLE: {p.title}\nABSTRACT: {(p.abstract or '[missing]')[:3500]}"
            for i, p in enumerate(batch)
        )
        prompt = (
            "You are screening candidate academic papers for a literature search. Prefer relevant candidates "
            "over false negatives, but reject papers that only share generic keywords. A candidate is relevant "
            "when its title/abstract directly studies the topic or provides a useful, clearly related empirical "
            "or theoretical result. Interpret discipline-specific synonyms and equivalent wording semantically; "
            "do not require every planner concept label to appear verbatim. For each concept, report a short "
            "exact excerpt only when one supports it. The relationship_quote MUST be copied exactly from the "
            "title/abstract and show the connection to the topic; never invent or paraphrase a quote. If only "
            "the title is available, lower the score and say why. A study about one component alone is relevant "
            "only when it clearly informs the stated relationship; mark generic background as irrelevant. "
            "Score 0-100 for topical relevance, not paper quality. Return one decision for each supplied ID.\n\n"
            f"TOPIC: {plan.topic}\nGEOGRAPHIC SCOPE: {plan.region or 'none specified'}\n"
            "When a region is specified, prioritize studies set in that region. Metadata may omit the study site; "
            "do not reject a directly relevant candidate for omission alone, but note the uncertainty in reason and score.\n"
            f"CENTRAL CONCEPTS: {concepts.concepts}\nRELEVANCE GUIDE: {concepts.scope_rule}\n\nCANDIDATES:\n{entries}"
        )
        async with slots:
            result = await generate_structured(prompt, RelevanceBatch)
        found, seen = [], set()
        for d in result.decisions:
            i = d.candidate_id - offset
            if d.candidate_id in seen or not 0 <= i < len(batch):
                continue
            seen.add(d.candidate_id)
            p = batch[i]
            if _approve(d, p, concepts, threshold):
                found.append((p, d.score))
            else:
                logger.info("REJECT id=%d score=%d title=%r reason=%s", d.candidate_id, d.score, p.title, d.reason)
        if len(seen) < len(batch):
            logger.warning("REJECT missing relevance decisions: %d", len(batch) - len(seen))
        return found

    # Batches are independent LLM calls: run a few at once, keep input order.
    results = await asyncio.gather(*(screen_batch(offset) for offset in range(0, len(papers), batch_size)))
    return [item for batch_result in results for item in batch_result]


_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def prescreen_rank(papers: list[Paper], plan: ResearchPlan, concepts: TopicConcepts) -> list[Paper]:
    """Cheap lexical ordering so the LLM screens the most promising candidates first.

    It only decides the ORDER of screening; papers not screened in this window
    stay available for the next one and are never rejected by this score.
    """
    vocabulary = {w.casefold() for text in [plan.topic, *concepts.concepts, *plan.keywords]
                  for w in _WORD.findall(text or "")}
    def score(paper):
        title = {w.casefold() for w in _WORD.findall(paper.title or "")}
        abstract = {w.casefold() for w in _WORD.findall(paper.abstract or "")}
        return (2 * len(vocabulary & title) + len(vocabulary & abstract), bool(paper.abstract),
                paper.citation_count or 0)
    return sorted(papers, key=score, reverse=True)
