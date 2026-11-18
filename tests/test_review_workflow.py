"""Review branch tests without external LLMs or a shared PostgreSQL database."""
import copy
import json
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET
import httpx
import pytest
from fastapi import HTTPException
from app.main import app
from app.models import ResearchPlanRequest
from app.routers import review
from app.services import review_manuscript as engine
from app.services.review_export import build_review_pdf, build_review_docx
from app.routers.export import PaperExportOptions


def corpus():
    return {"topic": "Trí tuệ nhân tạo trong giáo dục", "keywords": ["trí tuệ nhân tạo", "giáo dục"],
        "papers": [{"id": i, "title": f"Study {i}", "authors": ["Author"], "year": 2024,
                    "doi": None, "venue": "Journal"} for i in (10, 20)],
        "evidence_sources": [{"paper_id": i, "source_id": f"paper:{i}:abstract", "kind": "abstract",
            "text": "Students reported improved learning engagement when using AI tools, but causal effects were not established."} for i in (10, 20)],
        "search_audit": {"query": "AI education", "search_date": "2026-11-11", "sources_requested": ["OpenAlex"], "selected": 2}}


def manuscript():
    c = corpus()
    p = {"text": "Các nghiên cứu ghi nhận sự tham gia học tập được cải thiện; chưa đủ bằng chứng nhân quả [10][20].",
         "evidence": [{"paper_id": s["paper_id"], "source_id": s["source_id"], "quote": s["text"]} for s in c["evidence_sources"]]}
    return {"paper_type": "review", "title": c["topic"], "keywords": c["keywords"], "papers": c["papers"],
        "evidence_sources": c["evidence_sources"], "search_audit": c["search_audit"],
        "sections": [engine.methodology(c) if key == "methodology" else
                     {"key": key, "title": title, "paragraphs": [copy.deepcopy(p)]} for key, title in engine.SECTION_TITLES.items()]}


def test_paper_types_are_explicit_and_legacy_default_is_preserved():
    assert ResearchPlanRequest(topic="Topic", paper_type="review").paper_type == "review"
    assert ResearchPlanRequest(topic="Topic").paper_type == "original_research"


def test_source_quotes_and_citations_must_match():
    m = manuscript()
    assert engine.check_manuscript(m)["structural_ok"]
    p = m["sections"][0]["paragraphs"][0]
    p["evidence"][0]["quote"] = "This invented sentence does not appear in the source."
    assert not engine.check_manuscript(m)["structural_ok"]
    p["text"] += " [999]"
    report = engine.check_manuscript(m)
    assert any("Trích dẫn" in e["reason"] for e in report["errors"])


def test_cross_paper_quote_and_unsupported_numbers_are_rejected():
    m = manuscript()
    p = m["sections"][0]["paragraphs"][0]
    p["evidence"][0]["paper_id"] = 20
    p["text"] += " Mức tăng là 37.4%."
    report = engine.check_manuscript(m)
    assert not report["structural_ok"]
    assert any("37.4%" in e["reason"] for e in report["errors"])


def test_numeric_substrings_cannot_authorize_a_different_statistic():
    m = manuscript()
    for source in m["evidence_sources"]:
        source["text"] += " Total sample: 137.4."
    p = m["sections"][0]["paragraphs"][0]
    for link, source in zip(p["evidence"], m["evidence_sources"]):
        link["quote"] = source["text"]
    p["text"] += " Giá trị là 37.4."
    report = engine.check_manuscript(m)
    assert any("37.4" in e["reason"] for e in report["errors"])


def test_methodology_reports_limits_instead_of_inventing_prisma_counts():
    c = corpus()
    c["search_audit"] = None
    text = engine.methodology(c)["paragraphs"][0]["text"]
    assert "Không có nhật ký tìm kiếm" in text
    assert "chưa đáp ứng quy trình tổng quan hệ thống" in text
    assert "không phải nghiên cứu thực nghiệm mới" in text


async def test_semantic_validation_fails_closed_for_missing_decisions(monkeypatch):
    async def incomplete(prompt, schema):
        return schema(decisions=[])
    monkeypatch.setattr(engine, "generate_structured", incomplete)
    m = manuscript()
    with pytest.raises(HTTPException) as exc:
        await engine.semantic_validation(m, engine.check_manuscript(m))
    assert exc.value.status_code == 502


async def test_semantic_validation_flags_unsubstantiated_claims(monkeypatch):
    async def validator(prompt, schema):
        batch = json.loads(prompt[prompt.index('[{'):])
        return schema(decisions=[{"paragraph_id": p["paragraph_id"], "supported": p["paragraph_id"] != "gaps:0", "reason": "Check source"} for p in batch])
    monkeypatch.setattr(engine, "generate_structured", validator)
    m = manuscript()
    report = await engine.semantic_validation(m, engine.check_manuscript(m))
    assert not report["ok"]
    assert report["semantic_checked"]
    assert report["human_review_required"]


async def test_review_writer_has_all_sections_and_never_calls_dataset_analysis(monkeypatch):
    prompts = []
    async def writer(prompt, schema):
        prompts.append(prompt)
        return schema(paragraphs=manuscript()["sections"][0]["paragraphs"])
    monkeypatch.setattr(engine, "generate_structured", writer)
    result = await engine.write_review(corpus())
    assert [s["key"] for s in result["sections"]] == list(engine.SECTION_TITLES)
    assert result["paper_type"] == "review"
    assert len(prompts) == 6
    assert "Phương pháp tổng quan" in result["sections"][2]["title"]
    assert engine.check_manuscript(result)["structural_ok"]


async def test_editing_creates_an_unvalidated_version_and_cannot_change_evidence_sources(monkeypatch):
    original = manuscript()
    async def load(_):
        return {"id": 1, "project_id": 3, "status": "validated", "manuscript": copy.deepcopy(original), "validation": {"ok": True}}
    captured = {}
    async def save(project_id, draft, validation):
        captured.update(draft=draft, validation=validation)
        return {"id": 2, "status": "draft", "manuscript": draft}
    monkeypatch.setattr(review, "load_review", load)
    monkeypatch.setattr(review, "store", save)
    edits = {s["key"]: {"paragraphs": s["paragraphs"]} for s in original["sections"] if s["key"] != "methodology"}
    result = await review.edit_review(1, review.ReviewEdit(title="Một tiêu đề chỉnh sửa", sections=edits))
    assert result["id"] == 2 and result["status"] == "draft"
    assert result["manuscript"]["previous_version_id"] == 1
    assert result["manuscript"]["evidence_sources"] == original["evidence_sources"]
    assert "ok" not in captured["validation"]


async def test_export_is_blocked_until_validation_passes(monkeypatch):
    async def load(_):
        return {"status": "needs_revision", "validation": {"ok": False}}
    monkeypatch.setattr(review, "load_review", load)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/reviews/1/export/pdf")
    assert response.status_code == 409


def test_review_pdf_and_docx_are_editable_unicode_documents(tmp_path):
    import pymupdf
    m = manuscript()
    options = PaperExportOptions(authors=[{"name": "Tác giả minh họa", "affiliation": "Đơn vị nghiên cứu"}])
    pdf = tmp_path / "review.pdf"
    docx = tmp_path / "review.docx"
    build_review_pdf(m, pdf, options)
    build_review_docx(m, docx, options)
    with pymupdf.open(pdf) as document:
        text = "\n".join(page.get_text() for page in document)
        assert "Bài tổng quan tường thuật" in text
        assert "TÓM TẮT" in text and "TÀI LIỆU THAM KHẢO" in text
        assert "Tác giả minh họa" in text
        assert "[10]" not in text and "[20]" not in text
    with ZipFile(docx) as archive:
        for name in archive.namelist():
            if name.endswith((".xml", ".rels")):
                ET.fromstring(archive.read(name))
        document = ET.fromstring(archive.read("word/document.xml"))
        text = " ".join(document.itertext())
        assert "Tác giả minh họa" in text
        assert "Trí tuệ nhân tạo" in text or "TRÍ TUỆ NHÂN TẠO" in text
        assert "TÀI LIỆU THAM KHẢO" in text
        assert archive.read("word/styles.xml").find(b'Times New Roman') > 0


async def test_auto_repair_targets_bad_paragraph_and_revalidates(monkeypatch):
    m = manuscript()
    original = copy.deepcopy(m)
    m["sections"][0]["paragraphs"][0]["text"] += " Giá trị bịa là 9876."
    calls = []
    async def generate(prompt, schema):
        calls.append(schema)
        if schema is engine.ParagraphRepairs:
            return schema(repairs=[{"paragraph_id": "abstract:0", **original["sections"][0]["paragraphs"][0]}])
        batch = json.loads(prompt[prompt.index('[{'):])
        return schema(decisions=[{"paragraph_id": p["paragraph_id"], "supported": True, "reason": "Supported by quote"} for p in batch])
    monkeypatch.setattr(engine, "generate_structured", generate)
    repaired, report = await engine.repair_and_validate(m)
    assert report["ok"]
    assert report["auto_repair"]["rounds"] == 1
    assert "9876" not in repaired["sections"][0]["paragraphs"][0]["text"]
    assert "9876" in m["sections"][0]["paragraphs"][0]["text"]  # Input version retained.
    assert repaired["evidence_sources"] == m["evidence_sources"]
    assert repaired["sections"][1:] == m["sections"][1:]
    assert engine.SupportReport in calls


async def test_auto_repair_cannot_bypass_semantic_validation(monkeypatch):
    m = manuscript()
    attempts = []
    async def generate(prompt, schema):
        if schema is engine.ParagraphRepairs:
            attempts.append(1)
            return schema(repairs=[{"paragraph_id": "abstract:0", **m["sections"][0]["paragraphs"][0]}])
        batch = json.loads(prompt[prompt.index('[{'):])
        return schema(decisions=[{"paragraph_id": p["paragraph_id"], "supported": p["paragraph_id"] != "abstract:0", "reason": "Not supported"} for p in batch])
    monkeypatch.setattr(engine, "generate_structured", generate)
    _, report = await engine.repair_and_validate(m, max_rounds=2)
    assert len(attempts) == 2
    assert not report["ok"]
    assert report["auto_repair"]["exhausted"]


async def test_incomplete_auto_repair_response_is_not_applied(monkeypatch):
    m = manuscript()
    m["sections"][0]["paragraphs"][0]["text"] += " 9876."
    async def generate(prompt, schema):
        return schema(repairs=[{"paragraph_id": "introduction:0", **m["sections"][1]["paragraphs"][0]}])
    monkeypatch.setattr(engine, "generate_structured", generate)
    with pytest.raises(HTTPException) as exc:
        await engine.repair_and_validate(m)
    assert exc.value.status_code == 502


async def test_validation_returns_new_automatically_repaired_version(monkeypatch):
    from contextlib import asynccontextmanager
    before = manuscript()
    after = copy.deepcopy(before)
    after["sections"][0]["paragraphs"][0]["text"] += " Đã điều chỉnh."
    saved_versions, updates = [], []
    async def load(_):
        return {"id": 1, "project_id": 3, "status": "draft", "validation": {}, "manuscript": before}
    async def repair(draft):
        return after, {"ok": True, "auto_repair": {"rounds": 1}}
    async def store(project_id, draft, report):
        saved_versions.append(copy.deepcopy(draft))
        return {"id": 2}
    class Connection:
        async def execute(self, query, *args):
            updates.append(args)
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()
    monkeypatch.setattr(review, "load_review", load)
    monkeypatch.setattr(review, "repair_and_validate", repair)
    monkeypatch.setattr(review, "store", store)
    monkeypatch.setattr(review, "get_pool", lambda: Pool())
    result = await review.validate_review(1)
    assert result["id"] == 2 and result["status"] == "validated"
    assert result["manuscript"]["previous_version_id"] == 1
    assert updates[0][0] == 2
    assert saved_versions[0]["evidence_sources"] == before["evidence_sources"]
    assert "Đã điều chỉnh" not in before["sections"][0]["paragraphs"][0]["text"]
