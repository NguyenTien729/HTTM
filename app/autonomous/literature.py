"""Narrative-review branch using a frozen corpus and paragraph evidence links."""
import json

from pydantic import BaseModel

from app.routers.review import load_corpus
from app.services.llm import generate_structured
from app.services.review_manuscript import write_review, repair_and_validate, methodology
from app.services.vietnamese_writing import VIETNAMESE_WRITING_RULE


class ReviewScope(BaseModel):
    question: str
    gap: str
    geography: str
    time_scope: str
    feasibility_notes: str


async def plan(run):
    a = run["artifacts"]
    corpus = a.get("review_corpus") or await load_corpus(run["project_id"])
    a["review_corpus"] = corpus
    result = await generate_structured(
        VIETNAMESE_WRITING_RULE +
        "Lập phạm vi bài tổng quan tường thuật từ tập tài liệu được cung cấp. "
        "Giữ chủ đề và địa bàn người dùng; câu hỏi trả lời bằng tổng hợp nghiên cứu đã công bố. "
        "Không yêu cầu dataset mới, hồi quy mới hoặc giả thuyết thực nghiệm. "
        "Gap chỉ là giới hạn trong tập nguồn hiện có, không khẳng định thiếu hụt toàn lĩnh vực. "
        "Nêu rõ giới hạn độ phủ, thời gian và việc đọc tóm tắt/trích đoạn.\n" +
        json.dumps({"topic": a["topic"], "region": a["settings"].get("region"),
                    "papers": corpus["papers"], "feedback": a.get("feedback", "")},
                   ensure_ascii=False, default=str), ReviewScope)
    return result.model_dump() | {"paper_type": "review", "hypotheses": [], "data_needs": [],
        "evidence_paper_ids": [p["id"] for p in corpus["papers"]]}


def presentation(document, authors):
    sections = {s["title"]: "\n\n".join(p["text"] for p in s["paragraphs"])
                for s in document["sections"] if s["key"] != "abstract"}
    abstract = next("\n\n".join(p["text"] for p in s["paragraphs"])
                    for s in document["sections"] if s["key"] == "abstract")
    references = []
    for paper in document["papers"]:
        ref = dict(paper)
        if isinstance(ref.get("authors"), str):
            ref["authors"] = json.loads(ref["authors"])
        ref["authors"] = ref.get("authors") or []
        references.append(ref)
    return {"title": document["title"], "abstract": abstract, "keywords": document["keywords"],
            "section_order": list(sections),
            "authors": authors, "sections": sections, "references": references,
            "paper_type": "review"}


async def manuscript(run):
    from .pipeline import approved
    approved(run, "research_plan")
    a = run["artifacts"]
    corpus = dict(a["review_corpus"])
    corpus["writing_scope"] = {"approved_plan": a["research_plan"], "revision_notes": a.get("feedback", "")}
    document = await write_review(corpus)
    a["review_document"] = document
    a["paper_state"] = {"mode": "narrative-review", "stage": "verification_pending",
                        "blockers": ["Chờ kiểm chứng từng đoạn và trích dẫn."]}
    return presentation(document, a["settings"].get("authors", []))


async def verify(run):
    a = run["artifacts"]
    document = a["review_document"]
    if a.get("review_corpus"):
        document = document | {"sections": [methodology(a["review_corpus"]) if s["key"] == "methodology" else s
                                             for s in document["sections"]]}
    document, report = await repair_and_validate(document)
    a["review_document"] = document
    a["manuscript"] = presentation(document, a["settings"].get("authors", []))
    issues = [{"section": item["paragraph_id"], "claim": "", "reason": item["reason"], "severity": "error"}
              for item in report.get("errors", [])]
    issues += [{"section": item["paragraph_id"], "claim": "", "reason": item["reason"], "severity": "error"}
               for item in report.get("semantic_decisions", []) if not item["supported"]]
    a["paper_state"] = {"mode": "narrative-review",
        "stage": "verification_passed_pending_author" if report["ok"] else "blocked",
        "blockers": [item["reason"] for item in issues]}
    return report | {"issues": issues}
