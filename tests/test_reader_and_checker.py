import pymupdf
import pytest

from app import db
from app.services import pdf_reader, chunk_store
from app.services.citation_checker import check_text, extract_cited_ids
from app.services.paper_merge import merge_papers
from app.models import Paper
from tests.helpers import insert_project, insert_paper


def make_pdf(text: str) -> bytes:
    doc = pymupdf.open()
    for i in range(0, len(text), 1500):
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 550, 800), text[i:i + 1500], fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


LONG = ("Introduction. " + "filler " * 400 + "Method: we surveyed participants, N = 523 students. "
        + "filler " * 400 + "Limitations: self-reported data. " + "filler " * 200)


def test_extract_and_chunk():
    text = pdf_reader.extract_text(make_pdf(LONG))
    assert "N = 523" in text
    chunks = pdf_reader.chunk_text(text, max_words=200, overlap=20)
    assert len(chunks) > 3 and all(len(c.split()) <= 200 for c in chunks)
    assert pdf_reader.chunk_text("") == []


def test_extract_text_bad_pdf_returns_empty():
    assert pdf_reader.extract_text(b"not a pdf") == ""


async def test_download_rejects_html(monkeypatch):
    class R:
        status_code = 200
        content = b"<html>paywall</html>"

    class C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return R()
    monkeypatch.setattr(pdf_reader.httpx, "AsyncClient", C)
    assert await pdf_reader.download_pdf("http://x") is None
    assert await pdf_reader.download_pdf(None) is None


async def test_ingest_and_select_chunks_keyword_mode(client, monkeypatch):
    async def fake_dl(url):
        return make_pdf(LONG)
    monkeypatch.setattr(chunk_store, "download_pdf", fake_dl)
    async with db.get_pool().acquire() as conn:
        pid = await insert_project(conn)
        paper = await insert_paper(conn, pid, pdf_url="http://x/a.pdf")
        n = await chunk_store.ingest_paper(conn, paper, "http://x/a.pdf")
        assert n >= 2
        assert await chunk_store.ingest_paper(conn, paper, "http://x/a.pdf") == 0   # idempotent
        picked = await chunk_store.select_chunks(conn, paper, k=2)
    assert 1 <= len(picked) <= 2
    assert any("N = 523" in c or "Limitations" in c for c in picked)


async def test_ingest_without_pdf_returns_zero(client):
    async with db.get_pool().acquire() as conn:
        pid = await insert_project(conn)
        paper = await insert_paper(conn, pid)
        assert await chunk_store.ingest_paper(conn, paper, None) == 0
        assert await chunk_store.select_chunks(conn, paper) == []


async def test_vector_mode_with_fake_embeddings(client, monkeypatch):
    async with db.get_pool().acquire() as conn:
        if not await chunk_store.has_embedding_column(conn):
            pytest.skip("DB không có pgvector")
    dim = 1536

    async def fake_embed(texts):
        # vector one-hot theo độ dài text -> deterministic
        return [[1.0 if i == len(t) % dim else 0.0 for i in range(dim)] for t in texts]

    async def fake_dl(url):
        return make_pdf(LONG)
    monkeypatch.setattr(chunk_store, "embed", fake_embed)
    monkeypatch.setattr(chunk_store, "download_pdf", fake_dl)
    async with db.get_pool().acquire() as conn:
        pid = await insert_project(conn)
        paper = await insert_paper(conn, pid)
        assert await chunk_store.ingest_paper(conn, paper, "http://x") >= 2
        assert await conn.fetchval("SELECT count(*) FROM paper_chunks WHERE embedding IS NOT NULL") >= 2
        assert len(await chunk_store.select_chunks(conn, paper, k=2)) == 2


def test_citation_checker():
    r = check_text("AI giúp sinh viên học tập hiệu quả hơn rất nhiều [1, 99]. Câu này không có nguồn nào cả nhé bạn.", {1, 2})
    assert r["invalid_ids"] == [99] and not r["ok"]
    assert len(r["uncited_sentences"]) == 1
    assert extract_cited_ids("a [3] b [3, 4] c [2024x]") == [3, 4]
    assert check_text("AI giúp sinh viên học tập hiệu quả hơn rất nhiều [1].", {1})["ok"]


def test_merge_papers():
    a = Paper(project_id=1, title="X", doi="10.1/x", citation_count=1)
    b = Paper(project_id=1, title="x", doi="10.1/X", abstract="abs", pdf_url="u", citation_count=5)
    c = Paper(project_id=1, title="No DOI!", source="s2")
    d = Paper(project_id=1, title="no doi", source="openalex")
    out = merge_papers([a, c], [b, d])
    assert len(out) == 2
    assert out[0].abstract == "abs" and out[0].pdf_url == "u" and out[0].citation_count == 5


async def test_reviewer_endpoints(client):
    async with db.get_pool().acquire() as conn:
        pid = await insert_project(conn)
        p1 = await insert_paper(conn, pid)
        rq = await conn.fetchval(
            "INSERT INTO gaps (project_id, gap_type, description, proposed_research_question) "
            "VALUES ($1,'x','d','q') RETURNING id;", pid)
        rq = await conn.fetchval(
            "INSERT INTO research_questions (project_id, gap_id, rq_text, feasibility_score, feasibility_notes,"
            " novelty_score, novelty_notes, is_quantitative) VALUES ($1,$2,'q',3,'n',3,'n',true) RETURNING id;", pid, rq)
        await conn.execute(
            "INSERT INTO introductions (project_id, research_question_id, background, problem_statement,"
            " existing_knowledge, literature_limitation, research_gap_statement, research_question_statement,"
            " contribution_statement) VALUES ($1,$2,$3,'p','e','l','g','r','c');",
            pid, rq, f"AI tăng năng suất học tập của sinh viên đại học [{p1}] và cả [777].")

    r = await client.post(f"/review/citations/{pid}", json={"text": f"Một nhận định có nguồn rõ ràng [{p1}]."})
    assert r.json()["ok"] is True
    s = (await client.get(f"/review/citations/{pid}/stored")).json()
    assert s["all_ok"] is False and s["reports"][0]["invalid_ids"] == [777]
