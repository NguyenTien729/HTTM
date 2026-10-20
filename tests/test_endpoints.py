"""Happy-path cho /plan, /search, /analyze, /synthesize. LLM và API ngoài đều được mock
-> không tốn tiền, không cần mạng."""

from app import db
from app.models import (
    Paper, PaperAnalysisExtraction, ThemesExtraction, ThemeExtraction,
    GapsExtraction, GapExtraction,
)
from tests.helpers import sample_plan, insert_project, insert_paper


async def test_plan_create_and_get(client, monkeypatch):
    async def fake_llm(prompt, schema):
        return sample_plan()
    monkeypatch.setattr("app.routers.planner.generate_structured", fake_llm)

    r = await client.post("/plan", json={
        "topic": "AI và năng suất sinh viên", "language": "vi",
        "study_type": "survey", "target_paper_count": 10, "target_venue": None,
    })
    assert r.status_code == 200
    pid = r.json()["id"]
    assert r.json()["plan"]["keywords"][0] == "ai in education"

    r2 = await client.get(f"/plan/{pid}")
    assert r2.status_code == 200 and r2.json()["id"] == pid
    assert (await client.get("/plan/9999")).status_code == 404


async def test_search_merges_sources_and_dedupes(client, monkeypatch):
    async with db.get_pool().acquire() as conn:
        pid = await insert_project(conn)

    async def fake_openalex(**kw):
        return [
            Paper(project_id=pid, title="A", doi="10.1/a", abstract=None, source="openalex"),
            Paper(project_id=pid, title="B", doi="10.1/b", abstract="b", source="openalex"),
        ]

    async def fake_s2(**kw):
        return [
            Paper(project_id=pid, title="A", doi="10.1/a", abstract="abs A", pdf_url="http://x/a.pdf",
                  source="semantic_scholar"),
            Paper(project_id=pid, title="C", doi="10.1/c", abstract="c", source="semantic_scholar"),
        ]

    monkeypatch.setenv("ENABLE_SEMANTIC_SCHOLAR", "1")
    monkeypatch.setattr("app.routers.search.search_openalex", fake_openalex)
    monkeypatch.setattr("app.routers.search.search_semantic_scholar", fake_s2)

    r = await client.post(f"/search/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["found"] == 3          # A (gộp), B, C
    assert body["saved"] == 3

    papers = (await client.get(f"/search/{pid}")).json()
    a = next(p for p in papers if p["doi"] == "10.1/a")
    assert a["abstract"] == "abs A" and a["pdf_url"] == "http://x/a.pdf"   # bổ sung từ nguồn 2

    # Chạy lại: không lưu trùng
    r2 = await client.post(f"/search/{pid}")
    assert r2.json()["saved"] == 0


async def test_search_openalex_failure_returns_502(client, monkeypatch):
    async with db.get_pool().acquire() as conn:
        pid = await insert_project(conn)

    async def boom(**kw):
        raise RuntimeError("down")
    monkeypatch.setattr("app.routers.search.search_openalex", boom)
    assert (await client.post(f"/search/{pid}")).status_code == 502


async def test_analyze_falls_back_to_abstract(client, monkeypatch):
    async with db.get_pool().acquire() as conn:
        pid = await insert_project(conn)
        p1 = await insert_paper(conn, pid, "P1", "abstract one")
        await insert_paper(conn, pid, "P2", None)   # không abstract, không pdf -> bị bỏ qua

    prompts = []

    async def fake_llm(prompt, schema):
        prompts.append(prompt)
        return PaperAnalysisExtraction(key_finding="AI giúp học tốt hơn", method="Survey")
    monkeypatch.setattr("app.routers.analysis.generate_structured", fake_llm)

    r = await client.post(f"/analyze/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["analyzed_now"] == 1 and body["fulltext_used"] == 0
    assert body["analyses"][0]["paper_id"] == p1
    assert "full-text" not in prompts[0]

    # Chạy lại: không phân tích lại
    assert (await client.post(f"/analyze/{pid}")).json()["analyzed_now"] == 0
    assert len((await client.get(f"/analyze/{pid}")).json()) == 1


async def test_synthesize_themes_gaps_and_approve(client, monkeypatch):
    async with db.get_pool().acquire() as conn:
        pid = await insert_project(conn)
        p1 = await insert_paper(conn, pid, "P1")
        await conn.execute(
            "INSERT INTO paper_analyses (paper_id, project_id, key_finding) VALUES ($1,$2,'finding');", p1, pid)

    # chưa phân tích -> 400
    async with db.get_pool().acquire() as conn:
        pid2 = await insert_project(conn)
    assert (await client.post(f"/synthesize/{pid2}")).status_code == 400

    async def fake_llm(prompt, schema):
        if schema is ThemesExtraction:
            return ThemesExtraction(themes=[ThemeExtraction(name="T1", description="d", paper_ids=[p1])])
        return GapsExtraction(gaps=[GapExtraction(
            gap_type="population", description="gap", evidence_paper_ids=[p1],
            proposed_research_question="RQ?")])
    monkeypatch.setattr("app.routers.synthesis.generate_structured", fake_llm)

    r = await client.post(f"/synthesize/{pid}")
    assert r.status_code == 200 and r.json()["themes"][0]["paper_ids"] == [p1]
    assert len((await client.get(f"/synthesize/{pid}")).json()) == 1

    g = await client.post(f"/synthesize/{pid}/gaps")
    assert g.status_code == 200
    gap_id = g.json()["gaps"][0]["id"]
    assert g.json()["gaps"][0]["confirmed"] is False

    ap = await client.post(f"/synthesize/{pid}/gaps/{gap_id}/approve")
    assert ap.status_code == 200 and ap.json()["confirmed"] is True
    assert (await client.post(f"/synthesize/{pid}/gaps/9999/approve")).status_code == 404


async def test_analyze_uses_fulltext_when_pdf_available(client, monkeypatch):
    import pymupdf
    from app.services import chunk_store

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(40, 40, 550, 800),
                        "Method: survey of 523 students. Findings: AI improved grades. Limitations: self-report.",
                        fontsize=10)
    pdf = doc.tobytes(); doc.close()

    async def fake_dl(url):
        return pdf if url == "http://x/ok.pdf" else None
    monkeypatch.setattr(chunk_store, "download_pdf", fake_dl)

    async with db.get_pool().acquire() as conn:
        pid = await insert_project(conn)
        with_pdf = await insert_paper(conn, pid, "HasPDF", "abs", pdf_url="http://x/ok.pdf")
        await insert_paper(conn, pid, "BrokenPDF", "abs2", pdf_url="http://x/missing.pdf")

    prompts = []

    async def fake_llm(prompt, schema):
        prompts.append(prompt)
        return PaperAnalysisExtraction(key_finding="f")
    monkeypatch.setattr("app.routers.analysis.generate_structured", fake_llm)

    body = (await client.post(f"/analyze/{pid}")).json()
    assert body["analyzed_now"] == 2          # paper PDF hỏng vẫn được phân tích bằng abstract
    assert body["fulltext_used"] == 1
    assert sum("full-text" in p for p in prompts) == 1

    # use_fulltext=false: bỏ qua PDF hoàn toàn
    async with db.get_pool().acquire() as conn:
        pid2 = await insert_project(conn)
        await insert_paper(conn, pid2, "P", "abs", pdf_url="http://x/ok.pdf")
    body2 = (await client.post(f"/analyze/{pid2}?use_fulltext=false")).json()
    assert body2["fulltext_used"] == 0
