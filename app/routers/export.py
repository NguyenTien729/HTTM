"""
Module Export: xuất PDF.

Hai loại tài liệu:
  POST /export/paper/{rq_id}     — BÀI BÁO HOÀN CHỈNH theo cấu trúc IMRaD: Abstract, Introduction,
                                   Related Work, RQ/giả thuyết, Methods, Results (kèm bảng số liệu),
                                   Discussion, Conclusion, References. Cần đã chạy:
                                   /write/introduction, /write/related-work, /write/discussion
                                   (Discussion lưu kèm kết quả /data-research/analyze).
  POST /export/proposal/{rq_id}  — (giữ nguyên) Research Proposal: chưa có Methods/Results thật.

Nguyên tắc: bước export KHÔNG gọi LLM và không tự sinh số liệu — chỉ ráp dữ liệu đã lưu trong DB
(văn bản đã được verify citation/số liệu ở bước viết). Citation được đánh số lại [1], [2]... theo
thứ tự xuất hiện; citation không tồn tại bị xoá khỏi bài và ghi vào "Ghi chú dành cho tác giả".
Font NotoSans (Unicode, hỗ trợ dấu tiếng Việt).
"""

import re
import json
import logging
from pathlib import Path
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem, Table, TableStyle, PageBreak,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from app.db import get_pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/export", tags=["export"])

_FONT_PATH = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSans-VF.ttf"
_FONT_NAME = "VNFont"
_FONT_REGISTERED = False


def _ensure_font_registered() -> None:
    """Đăng ký font Unicode 1 lần duy nhất (reportlab raise lỗi nếu đăng ký trùng tên)."""
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return
    if not _FONT_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Thiếu font PDF tại {_FONT_PATH}. Không thể xuất PDF có dấu tiếng Việt.",
        )
    pdfmetrics.registerFont(TTFont(_FONT_NAME, str(_FONT_PATH)))
    _FONT_REGISTERED = True


def _build_styles() -> dict:
    """Định nghĩa style riêng dùng font Unicode — KHÔNG dùng style mặc định của
    reportlab vì chúng trỏ tới font Helvetica/Times không có dấu tiếng Việt.
    Font variable chỉ có 1 weight thật, nên phân biệt heading bằng cỡ chữ + màu,
    không dùng bold thật (xem ghi chú lúc tải font)."""
    base = getSampleStyleSheet()
    styles = {
        "Title": ParagraphStyle(
            "VNTitle", parent=base["Title"], fontName=_FONT_NAME,
            fontSize=18, leading=22, alignment=TA_CENTER, spaceAfter=6,
        ),
        "Subtitle": ParagraphStyle(
            "VNSubtitle", parent=base["Normal"], fontName=_FONT_NAME,
            fontSize=11, leading=14, alignment=TA_CENTER,
            textColor=HexColor("#555555"), spaceAfter=18,
        ),
        "H1": ParagraphStyle(
            "VNH1", parent=base["Heading1"], fontName=_FONT_NAME,
            fontSize=14, leading=18, spaceBefore=16, spaceAfter=8,
            textColor=HexColor("#1a1a1a"),
        ),
        "H2": ParagraphStyle(
            "VNH2", parent=base["Heading2"], fontName=_FONT_NAME,
            fontSize=12, leading=16, spaceBefore=10, spaceAfter=6,
            textColor=HexColor("#333333"),
        ),
        "Body": ParagraphStyle(
            "VNBody", parent=base["Normal"], fontName=_FONT_NAME,
            fontSize=10.5, leading=15, alignment=TA_JUSTIFY, spaceAfter=8,
        ),
        "Meta": ParagraphStyle(
            "VNMeta", parent=base["Normal"], fontName=_FONT_NAME,
            fontSize=9.5, leading=13, textColor=HexColor("#555555"), spaceAfter=4,
        ),
        "Reference": ParagraphStyle(
            "VNReference", parent=base["Normal"], fontName=_FONT_NAME,
            fontSize=9.5, leading=13, leftIndent=14, spaceAfter=4,
        ),
        "Abstract": ParagraphStyle(
            "VNAbstract", parent=base["Normal"], fontName=_FONT_NAME,
            fontSize=10, leading=14, alignment=TA_JUSTIFY, leftIndent=18, rightIndent=18, spaceAfter=6,
        ),
        "AbstractHead": ParagraphStyle(
            "VNAbstractHead", parent=base["Normal"], fontName=_FONT_NAME,
            fontSize=11, leading=14, alignment=TA_CENTER, spaceBefore=4, spaceAfter=4,
            textColor=HexColor("#333333"),
        ),
        "Caption": ParagraphStyle(
            "VNCaption", parent=base["Normal"], fontName=_FONT_NAME,
            fontSize=9.5, leading=13, spaceBefore=6, spaceAfter=4, textColor=HexColor("#222222"),
        ),
        "TableHead": ParagraphStyle(
            "VNTableHead", parent=base["Normal"], fontName=_FONT_NAME, fontSize=8.5, leading=11,
            textColor=HexColor("#111111"),
        ),
        "TableCell": ParagraphStyle(
            "VNTableCell", parent=base["Normal"], fontName=_FONT_NAME, fontSize=8.5, leading=11,
        ),
        "TableNote": ParagraphStyle(
            "VNTableNote", parent=base["Normal"], fontName=_FONT_NAME, fontSize=8.5, leading=11,
            textColor=HexColor("#555555"), spaceBefore=2,
        ),
    }
    return styles


async def _load_proposal_data(research_question_id: int) -> dict:
    """Gom toàn bộ dữ liệu cần thiết cho 1 bản proposal — chỉ đọc DB, không gọi LLM."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rq_row = await conn.fetchrow(
            "SELECT * FROM research_questions WHERE id = $1;", research_question_id
        )
        if rq_row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy research question.")

        gap_row = await conn.fetchrow("SELECT * FROM gaps WHERE id = $1;", rq_row["gap_id"])
        project_row = await conn.fetchrow(
            "SELECT topic FROM research_projects WHERE id = $1;", rq_row["project_id"]
        )
        intro_row = await conn.fetchrow(
            """SELECT * FROM introductions WHERE research_question_id = $1
               ORDER BY id DESC LIMIT 1;""",
            research_question_id,
        )
        related_row = await conn.fetchrow(
            """SELECT * FROM related_works WHERE research_question_id = $1
               ORDER BY id DESC LIMIT 1;""",
            research_question_id,
        )

        if intro_row is None:
            raise HTTPException(
                status_code=400,
                detail=f"Chưa có Introduction cho research question này. "
                       f"Gọi POST /write/introduction/{research_question_id} trước.",
            )
        if related_row is None:
            raise HTTPException(
                status_code=400,
                detail=f"Chưa có Related Work cho research question này. "
                       f"Gọi POST /write/related-work/{research_question_id} trước.",
            )

        # Gom toàn bộ citation hợp lệ đã dùng ở cả 2 phần để build reference list —
        # chỉ lấy citations_used (đã verify là paper_id CÓ THẬT), bỏ qua citations_invalid.
        intro_citations = json.loads(intro_row["citations_used"]) if isinstance(intro_row["citations_used"], str) else intro_row["citations_used"]
        related_citations = json.loads(related_row["citations_used"]) if isinstance(related_row["citations_used"], str) else related_row["citations_used"]
        all_cited_ids = sorted(set(intro_citations) | set(related_citations))

        reference_rows = []
        if all_cited_ids:
            reference_rows = await conn.fetch(
                "SELECT id, title, authors, year, doi, venue FROM papers WHERE id = ANY($1::int[]) ORDER BY id;",
                all_cited_ids,
            )

    def _jsonb(value):
        return json.loads(value) if isinstance(value, str) else value

    return {
        "project_topic": project_row["topic"] if project_row else "",
        "rq": dict(rq_row) | {
            "variables": _jsonb(rq_row["variables"]),
            "hypotheses": _jsonb(rq_row["hypotheses"]),
        },
        "gap": dict(gap_row) if gap_row else None,
        "introduction": dict(intro_row),
        "related_work": dict(related_row) | {
            "theme_paragraphs": _jsonb(related_row["theme_paragraphs"]),
        },
        "references": [dict(r) for r in reference_rows],
    }


def _format_reference(paper: dict, label=None) -> str:
    """Format kiểu APA rút gọn. authors là JSONB list tên tác giả."""
    authors = paper["authors"]
    authors = json.loads(authors) if isinstance(authors, str) else authors
    if not authors:
        author_str = "(Không rõ tác giả)"
    elif len(authors) == 1:
        author_str = authors[0]
    elif len(authors) <= 3:
        author_str = ", ".join(authors[:-1]) + f" & {authors[-1]}"
    else:
        author_str = f"{authors[0]} et al."

    year = paper["year"] or "n.d."
    title = paper["title"]
    venue = paper["venue"] or ""
    doi_part = f" https://doi.org/{paper['doi']}" if paper["doi"] else ""
    venue_part = f" {venue}." if venue else ""
    return f"[{label if label is not None else paper['id']}] {author_str} ({year}). {title}.{venue_part}{doi_part}"


def _build_pdf(data: dict, output_path: Path) -> None:
    _ensure_font_registered()
    styles = _build_styles()

    doc = SimpleDocTemplate(
        str(output_path), pagesize=A4,
        topMargin=2.2 * cm, bottomMargin=2.2 * cm, leftMargin=2.2 * cm, rightMargin=2.2 * cm,
    )
    story = []

    rq = data["rq"]
    intro = data["introduction"]
    related = data["related_work"]

    # ---- Trang bìa / tiêu đề ----
    story.append(Paragraph(rq["rq_text"], styles["Title"]))
    story.append(Paragraph(
        f"Research Proposal — Đề tài: {data['project_topic']}", styles["Subtitle"]
    ))
    story.append(Paragraph(
        "<i>Tài liệu này là bản ĐỀ XUẤT NGHIÊN CỨU (proposal), chưa bao gồm Kết quả (Results) "
        "vì nghiên cứu chưa được tiến hành trên dữ liệu thật. Methods bên dưới là phương pháp "
        "DỰ KIẾN sẽ áp dụng.</i>",
        styles["Meta"],
    ))
    story.append(Spacer(1, 10))

    # ---- 1. Introduction ----
    story.append(Paragraph("1. Giới thiệu (Introduction)", styles["H1"]))
    for text in [
        intro["background"], intro["problem_statement"], intro["existing_knowledge"],
        intro["literature_limitation"], intro["research_gap_statement"],
        intro["research_question_statement"], intro["contribution_statement"],
    ]:
        story.append(Paragraph(text, styles["Body"]))

    # ---- 2. Related Work ----
    story.append(Paragraph("2. Tổng quan tài liệu (Related Work)", styles["H1"]))
    for tp in related["theme_paragraphs"]:
        story.append(Paragraph(tp["theme_name"], styles["H2"]))
        story.append(Paragraph(tp["paragraph"], styles["Body"]))
    story.append(Paragraph("Mâu thuẫn giữa các nghiên cứu", styles["H2"]))
    story.append(Paragraph(related["contradictions_paragraph"], styles["Body"]))
    story.append(Paragraph("Khoảng trống nghiên cứu (Research Gap)", styles["H2"]))
    story.append(Paragraph(related["gap_paragraph"], styles["Body"]))

    # ---- 3. Research Question & Hypotheses ----
    story.append(Paragraph("3. Câu hỏi nghiên cứu", styles["H1"]))
    story.append(Paragraph(f"<b>RQ:</b> {rq['rq_text']}", styles["Body"]))
    story.append(Paragraph(
        f"<b>Tính khả thi:</b> {rq['feasibility_score']}/5 — {rq['feasibility_notes']}",
        styles["Body"],
    ))
    story.append(Paragraph(
        f"<b>Tính mới:</b> {rq['novelty_score']}/5 — {rq['novelty_notes']}",
        styles["Body"],
    ))

    if rq["is_quantitative"] and rq["variables"]:
        story.append(Paragraph("Biến số (Variables)", styles["H2"]))
        var_items = [
            ListItem(Paragraph(f"<b>{v['name']}</b> ({v['role']}) — {v['description']}", styles["Body"]))
            for v in rq["variables"]
        ]
        story.append(ListFlowable(var_items, bulletType="bullet"))

    if rq["is_quantitative"] and rq["hypotheses"]:
        story.append(Paragraph("Giả thuyết (Hypotheses)", styles["H2"]))
        for h in rq["hypotheses"]:
            story.append(Paragraph(f"<b>{h['label']}:</b> {h['statement']}", styles["Body"]))

    # ---- 4. Methods (dự kiến) ----
    story.append(Paragraph("4. Phương pháp dự kiến (Proposed Methods)", styles["H1"]))
    if rq["is_quantitative"]:
        story.append(Paragraph(
            "Nghiên cứu này dự kiến sử dụng thiết kế ĐỊNH LƯỢNG để kiểm định các giả thuyết "
            "đã nêu ở trên. Dữ liệu sẽ được thu thập thông qua khảo sát/thực nghiệm phù hợp với "
            "các biến số đã xác định, sau đó phân tích thống kê (ví dụ: hồi quy, kiểm định "
            "giả thuyết) để trả lời research question. <b>Lưu ý: đây là kế hoạch dự kiến, "
            "chưa có dữ liệu/kết quả thật.</b>",
            styles["Body"],
        ))
    else:
        story.append(Paragraph(
            "Nghiên cứu này dự kiến sử dụng thiết kế ĐỊNH TÍNH (ví dụ: phỏng vấn sâu, "
            "phân tích nội dung) phù hợp với bản chất khám phá của research question. "
            "<b>Lưu ý: đây là kế hoạch dự kiến, chưa có dữ liệu/kết quả thật.</b>",
            styles["Body"],
        ))

    # ---- 5. References ----
    story.append(Paragraph("5. Tài liệu tham khảo (References)", styles["H1"]))
    if data["references"]:
        for paper in data["references"]:
            story.append(Paragraph(_format_reference(paper), styles["Reference"]))
    else:
        story.append(Paragraph("(Không có citation nào được verify từ các phần trên.)", styles["Body"]))

    doc.build(story)


# =========================================================================== #
# BÀI BÁO HOÀN CHỈNH (IMRaD):  Abstract → Introduction → Related Work → RQ/giả thuyết
#   → Methods → Results → Discussion → Conclusion → References
# Mọi nội dung lấy từ DB / kết quả đã tính; module này KHÔNG gọi LLM và không tự viết số liệu.
# =========================================================================== #

_CITE_RE = re.compile(r"\[(\d+)\]")
_VERDICT_VI = {
    "supported": "Được ủng hộ",
    "partially_supported": "Được ủng hộ một phần",
    "not_supported": "Không được ủng hộ",
    "not_tested": "Chưa kiểm định được",
}
_MAX_TABLE_ROWS = 15


def _jsonb(value):
    return json.loads(value) if isinstance(value, str) else value


def _esc(text) -> str:
    """Escape ký tự đặc biệt trước khi đưa vào reportlab Paragraph (đọc như mini-XML:
    một dấu '<' hoặc '&' trong text LLM sẽ làm hỏng cả file PDF nếu không escape)."""
    return escape(str(text if text is not None else ""))


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "–"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return str(value)


def _fmt_p(value) -> str:
    if value is None:
        return "–"
    return "< 0.001" if value < 0.001 else f"{value:.3f}"


def _p_clause(value) -> str:
    """'p < 0.001' hoặc 'p = 0.123' (không viết 'p = < 0.001')."""
    if value is None:
        return "p = –"
    return "p < 0.001" if value < 0.001 else f"p = {value:.3f}"


def _clean_draft_text(text: str | None) -> str:
    return (text or "").replace("`", "").strip()


# ---- Đánh số lại citation theo thứ tự xuất hiện (chuẩn bài báo) ------------ #

class _CitationNumbering:
    """[paper_id] (id nội bộ DB) → [1], [2], ... theo thứ tự xuất hiện đầu tiên trong bài.
    paper_id không nằm trong `valid_ids` (LLM bịa) bị XOÁ khỏi văn bản, không đưa vào bài."""

    def __init__(self, valid_ids: set[int]):
        self.valid_ids = valid_ids
        self.mapping: dict[int, int] = {}
        self.removed: list[int] = []

    def apply(self, text: str) -> str:
        had_removal = False

        def repl(match: re.Match) -> str:
            nonlocal had_removal
            pid = int(match.group(1))
            if pid not in self.valid_ids:
                if pid not in self.removed:
                    self.removed.append(pid)
                had_removal = True
                return ""
            if pid not in self.mapping:
                self.mapping[pid] = len(self.mapping) + 1
            return f"[{self.mapping[pid]}]"

        out = _CITE_RE.sub(repl, text or "")
        if had_removal:
            out = re.sub(r"\s+([.,;:])", r"\1", out)
            out = re.sub(r"[ \t]{2,}", " ", out).strip()
        return out


def _strip_citations(text: str) -> str:
    out = _CITE_RE.sub("", text or "")
    out = re.sub(r"\s+([.,;:])", r"\1", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _first_sentences(text: str, max_sentences: int = 2, max_chars: int = 450) -> str:
    parts = re.split(r"(?<=[.!?])\s+", _strip_citations(text))
    out = " ".join(parts[:max_sentences]).strip()
    return out if len(out) <= max_chars else out[: max_chars - 1].rstrip() + "…"


# ---- Nạp dữ liệu ------------------------------------------------------------ #

async def _load_paper_data(research_question_id: int) -> dict:
    """Mở rộng dữ liệu proposal: thêm Discussion/Conclusion + snapshot phân tích dữ liệu,
    keywords, và reference của MỌI phần (không chỉ Introduction/Related Work)."""
    data = await _load_proposal_data(research_question_id)  # raise 404/400 nếu thiếu Intro/Related

    pool = get_pool()
    async with pool.acquire() as conn:
        disc_row = await conn.fetchrow(
            "SELECT * FROM discussions WHERE research_question_id = $1 ORDER BY id DESC LIMIT 1;",
            research_question_id,
        )
        if disc_row is None:
            raise HTTPException(
                status_code=400,
                detail=f"Chưa có Discussion/Conclusion cho research question này. "
                       f"Gọi POST /write/discussion/{research_question_id} (kèm kết quả "
                       f"/data-research/analyze) trước.",
            )
        project_row = await conn.fetchrow(
            "SELECT research_plan FROM research_projects WHERE id = $1;", data["rq"]["project_id"]
        )
        cited = set(_jsonb(data["introduction"]["citations_used"]) or [])
        cited |= set(_jsonb(data["related_work"]["citations_used"]) or [])
        cited |= set(_jsonb(disc_row["citations_used"]) or [])
        refs = []
        if cited:
            refs = await conn.fetch(
                "SELECT id, title, authors, year, doi, venue FROM papers WHERE id = ANY($1::int[]) ORDER BY id;",
                sorted(cited),
            )

    plan = _jsonb(project_row["research_plan"]) if project_row else {}
    disc = dict(disc_row)
    for key in ("hypothesis_assessments", "citations_used", "citations_invalid",
                "numbers_unverified", "analysis_snapshot"):
        disc[key] = _jsonb(disc[key])
    data["discussion"] = disc
    data["keywords"] = (plan or {}).get("keywords", [])[:6]
    data["references"] = [dict(r) for r in refs]
    return data


# ---- Dựng nội dung bài báo (thuần Python, test được không cần DB) ------------ #

def _table_block(caption: str, header: list[str], rows: list[list[str]], widths: list[float],
                 styles: dict, note: str | None = None) -> list:
    """Bảng có caption phía trên (chuẩn bài báo). `widths` là tỷ lệ, tự co theo bề ngang trang."""
    usable = A4[0] - 4.4 * cm
    total = sum(widths)
    col_widths = [usable * w / total for w in widths]
    data = [[Paragraph(_esc(h), styles["TableHead"]) for h in header]]
    for row in rows:
        data.append([Paragraph(_esc(c), styles["TableCell"]) for c in row])
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#EDEDED")),
        ("LINEABOVE", (0, 0), (-1, 0), 0.8, HexColor("#333333")),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, HexColor("#333333")),
        ("LINEBELOW", (0, -1), (-1, -1), 0.8, HexColor("#333333")),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    block = [Paragraph(_esc(caption), styles["Caption"]), table]
    if note:
        block.append(Paragraph(_esc(note), styles["TableNote"]))
    block.append(Spacer(1, 10))
    return block


def _results_tables(snap: dict, styles: dict) -> list:
    """Dựng các bảng Results trực tiếp từ số liệu đã tính — không qua LLM."""
    flow: list = []
    n_tab = 0

    summaries = snap.get("column_summaries") or {}
    numeric = [(c, s) for c, s in summaries.items() if s.get("kind") == "numeric" and s.get("n")]
    if numeric:
        n_tab += 1
        rows = [[c, _fmt(s.get("n")), _fmt(s.get("mean")), _fmt(s.get("std")),
                 _fmt(s.get("median")), _fmt(s.get("min")), _fmt(s.get("max"))]
                for c, s in numeric[:_MAX_TABLE_ROWS]]
        note = None
        if len(numeric) > _MAX_TABLE_ROWS:
            note = f"Chỉ hiển thị {_MAX_TABLE_ROWS}/{len(numeric)} biến số đầu tiên."
        flow += _table_block(f"Bảng {n_tab}. Thống kê mô tả các biến số",
                             ["Biến", "N", "Mean", "SD", "Median", "Min", "Max"], rows,
                             [3, 1, 1.2, 1.2, 1.2, 1.2, 1.2], styles, note)

    categorical = [(c, s) for c, s in summaries.items() if s.get("kind") == "categorical" and s.get("top_values")]
    if categorical:
        n_tab += 1
        rows = []
        for col, s in categorical[:6]:
            for tv in s["top_values"][:3]:
                rows.append([col, tv["value"], _fmt(tv["count"]), f"{_fmt(tv.get('percent_of_non_missing'), 1)}%"])
        flow += _table_block(f"Bảng {n_tab}. Phân phối biến phân loại (tối đa 3 giá trị phổ biến nhất mỗi biến)",
                             ["Biến", "Giá trị", "Tần số", "Tỷ lệ"], rows, [2.5, 3.5, 1.2, 1.2], styles)

    corr = snap.get("top_correlations_pearson") or []
    if corr:
        n_tab += 1
        rows = [[c["var_1"], c["var_2"], _fmt(c["pearson_r"])] for c in corr]
        flow += _table_block(f"Bảng {n_tab}. Các cặp biến có tương quan Pearson mạnh nhất",
                             ["Biến 1", "Biến 2", "r"], rows, [3, 3, 1.2], styles,
                             "Tương quan không hàm ý quan hệ nhân quả.")

    gt = snap.get("group_test")
    if gt and gt.get("status") == "ok":
        n_tab += 1
        if gt.get("group_summary"):
            rows = [[g["group"], _fmt(g["n"]), _fmt(g.get("mean")), _fmt(g.get("std"))] for g in gt["group_summary"]]
            flow += _table_block(
                f"Bảng {n_tab}. {gt.get('target_column')} theo nhóm {gt.get('group_column')}",
                ["Nhóm", "N", "Mean", "SD"], rows, [3, 1, 1.2, 1.2], styles)
            n_tab += 1
        tests = gt.get("tests") or ([gt["test"]] if gt.get("test") else [])
        if tests:
            rows = [[t["name"], _fmt(t.get("statistic")), _fmt_p(t.get("p_value"))] for t in tests]
            flow += _table_block(f"Bảng {n_tab}. Kết quả kiểm định giữa các nhóm",
                                 ["Phép kiểm định", "Thống kê", "p"], rows, [4, 1.5, 1.2], styles,
                                 "p-value chưa hiệu chỉnh cho đa kiểm định.")

    ols = snap.get("ols_regression")
    if ols and ols.get("status") == "ok":
        n_tab += 1
        rows = [[c["term"], _fmt(c.get("coefficient")), _fmt(c.get("std_error")), _fmt(c.get("t_statistic")),
                 _fmt_p(c.get("p_value")), f"[{_fmt(c.get('ci_95_lower'))}; {_fmt(c.get('ci_95_upper'))}]"]
                for c in ols.get("coefficients", [])]
        note = (f"Biến phụ thuộc: {ols.get('target')}; N = {_fmt(ols.get('n'))}; R² = {_fmt(ols.get('r_squared'))}; "
                f"R² hiệu chỉnh = {_fmt(ols.get('adjusted_r_squared'))}; mô hình tổng thể: {_p_clause(ols.get('model_p_value'))}.")
        flow += _table_block(f"Bảng {n_tab}. Hồi quy tuyến tính OLS",
                             ["Hạng tử", "B", "SE", "t", "p", "CI 95%"], rows,
                             [2.2, 1.2, 1.2, 1.1, 1.1, 2.6], styles, note)
    return flow


def _build_abstract(rq: dict, snap: dict, disc: dict, conclusion_text: str, assessments: list[dict]) -> str:
    """Abstract có cấu trúc, ráp bằng CODE từ các phần đã có (không gọi LLM)."""
    ds = snap.get("dataset") or {}
    parts = [f"<b>Mục tiêu.</b> {_esc(rq['rq_text'])}"]

    methods = []
    if ds.get("dataset_id"):
        n = ds.get("rows_analyzed")
        methods.append(f"phân tích thứ cấp trên bộ dữ liệu công khai {ds['dataset_id']}"
                       + (f" (N = {n:,})" if isinstance(n, int) else ""))
    gt, ols = snap.get("group_test"), snap.get("ols_regression")
    methods.append("thống kê mô tả và tương quan Pearson")
    if gt and gt.get("status") == "ok":
        methods.append("kiểm định so sánh nhóm")
    if ols and ols.get("status") == "ok":
        methods.append("hồi quy tuyến tính OLS")
    parts.append("<b>Phương pháp.</b> " + _esc("Nghiên cứu thực hiện " + ", ".join(methods) + "."))

    if assessments:
        verdicts = "; ".join(f"{a['label']}: {_VERDICT_VI.get(a['verdict'], a['verdict']).lower()}" for a in assessments)
        parts.append("<b>Kết quả.</b> " + _esc(verdicts.capitalize() + ". ") + _esc(_first_sentences(disc["key_findings_paragraph"], 1)))
    else:
        parts.append("<b>Kết quả.</b> " + _esc(_first_sentences(disc["key_findings_paragraph"], 2)))
    parts.append("<b>Kết luận.</b> " + _esc(_first_sentences(conclusion_text, 2, 500)))
    return "<br/>".join(parts)


def _prepare_paper(data: dict) -> dict:
    """Chuẩn hoá toàn bộ nội dung bài báo: đánh số citation theo thứ tự xuất hiện, xoá citation
    không có thật, gom ghi chú rà soát. Thuần Python — test được không cần DB."""
    intro, related, disc = data["introduction"], data["related_work"], data["discussion"]
    valid_ids = {p["id"] for p in data["references"]}
    num = _CitationNumbering(valid_ids)
    snap = disc.get("analysis_snapshot") or {}

    intro_paras = [num.apply(intro[k]) for k in (
        "background", "problem_statement", "existing_knowledge", "literature_limitation",
        "research_gap_statement", "research_question_statement", "contribution_statement")]
    related_themes = [(tp["theme_name"], num.apply(tp["paragraph"])) for tp in related["theme_paragraphs"]]
    related_contra = num.apply(related["contradictions_paragraph"])
    related_gap = num.apply(related["gap_paragraph"])

    methods_text = _clean_draft_text(snap.get("methods_draft"))
    results_paras = [p.strip() for p in _clean_draft_text(snap.get("results_draft")).split("\n\n") if p.strip()]
    # Câu cuối của results_draft là lời nhắc nội bộ cho người dùng, không phải văn bản bài báo.
    results_paras = [p for p in results_paras if not p.startswith("Không nên diễn giải p-value")]

    d = {k: num.apply(disc[k]) for k in (
        "key_findings_paragraph", "literature_comparison_paragraph", "implications_paragraph",
        "limitations_paragraph", "future_work_paragraph", "conclusion_paragraph")}
    assessments = [{**a, "explanation": num.apply(a["explanation"])} for a in disc["hypothesis_assessments"]]

    ordered_refs = []
    by_id = {p["id"]: p for p in data["references"]}
    for pid, n in sorted(num.mapping.items(), key=lambda kv: kv[1]):
        ordered_refs.append((n, by_id[pid]))

    notes = [
        "Bản thảo do hệ thống sinh tự động từ literature đã thu thập và kết quả phân tích dữ liệu; "
        "tác giả cần đọc lại toàn bộ trước khi sử dụng.",
        "Kiểm tra giấy phép sử dụng và các vấn đề đạo đức của dataset công khai đã chọn.",
        "Các p-value là phân tích khám phá, chưa hiệu chỉnh đa kiểm định; không diễn giải là quan hệ nhân quả.",
    ]
    if num.removed:
        notes.append(f"Đã tự động xoá {len(num.removed)} citation không tồn tại trong cơ sở dữ liệu bài báo "
                     f"(id: {', '.join(map(str, num.removed))}); hãy rà soát câu liên quan.")
    unverified = disc.get("numbers_unverified") or []
    if unverified:
        notes.append("Các số sau xuất hiện trong Discussion/Conclusion nhưng KHÔNG khớp số liệu phân tích — "
                     "cần kiểm tra thủ công: " + ", ".join(unverified) + ".")
    if not methods_text or not snap.get("column_summaries"):
        notes.append("Discussion này được lưu từ phiên bản cũ nên thiếu bản Methods/bảng số liệu; "
                     "hãy chạy lại POST /write/discussion để có đủ phần Methods và Results.")

    return {
        "intro": intro_paras, "related_themes": related_themes,
        "related_contra": related_contra, "related_gap": related_gap,
        "methods": methods_text, "results_paras": results_paras, "snap": snap,
        "discussion": d, "assessments": assessments, "references": ordered_refs, "notes": notes,
        "abstract": _build_abstract(data["rq"], snap, d, d["conclusion_paragraph"], assessments),
    }


def _page_decorations(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont(_FONT_NAME, 8.5)
    canvas.setFillColor(HexColor("#777777"))
    canvas.drawCentredString(A4[0] / 2, 1.2 * cm, f"{doc.page}")
    canvas.restoreState()


def _build_paper_pdf(data: dict, output_path: Path) -> None:
    _ensure_font_registered()
    styles = _build_styles()
    paper = _prepare_paper(data)
    rq, disc = data["rq"], data["discussion"]
    snap = paper["snap"]

    doc = SimpleDocTemplate(
        str(output_path), pagesize=A4, title=rq["rq_text"][:200],
        topMargin=2.2 * cm, bottomMargin=2.2 * cm, leftMargin=2.2 * cm, rightMargin=2.2 * cm,
    )
    story: list = []
    P = lambda text, style="Body": Paragraph(_esc(text), styles[style])  # noqa: E731

    # ---- Tiêu đề + Abstract + Keywords ----
    story.append(P(rq["rq_text"], "Title"))
    story.append(P(f"Đề tài: {data['project_topic']}", "Subtitle"))
    story.append(Paragraph("Abstract (Tóm tắt)", styles["AbstractHead"]))
    story.append(Paragraph(paper["abstract"], styles["Abstract"]))
    if data.get("keywords"):
        story.append(Paragraph("<b>Từ khoá:</b> " + _esc(", ".join(data["keywords"])), styles["Abstract"]))

    # ---- 1. Introduction ----
    story.append(P("1. Giới thiệu (Introduction)", "H1"))
    for text in paper["intro"]:
        story.append(P(text))

    # ---- 2. Related Work ----
    story.append(P("2. Tổng quan tài liệu (Related Work)", "H1"))
    for name, text in paper["related_themes"]:
        story.append(P(name, "H2"))
        story.append(P(text))
    story.append(P("Mâu thuẫn giữa các nghiên cứu", "H2"))
    story.append(P(paper["related_contra"]))
    story.append(P("Khoảng trống nghiên cứu", "H2"))
    story.append(P(paper["related_gap"]))

    # ---- 3. RQ & giả thuyết ----
    story.append(P("3. Câu hỏi nghiên cứu và giả thuyết", "H1"))
    story.append(Paragraph("<b>RQ:</b> " + _esc(rq["rq_text"]), styles["Body"]))
    if rq["is_quantitative"] and rq["variables"]:
        story.append(P("Biến số", "H2"))
        story.append(ListFlowable(
            [ListItem(Paragraph(f"<b>{_esc(v['name'])}</b> ({_esc(v['role'])}) — {_esc(v['description'])}", styles["Body"]))
             for v in rq["variables"]], bulletType="bullet"))
    if rq["is_quantitative"] and rq["hypotheses"]:
        story.append(P("Giả thuyết", "H2"))
        for h in rq["hypotheses"]:
            story.append(Paragraph(f"<b>{_esc(h['label'])}:</b> {_esc(h['statement'])}", styles["Body"]))

    # ---- 4. Methods ----
    story.append(P("4. Phương pháp (Methods)", "H1"))
    ds = snap.get("dataset") or {}
    if paper["methods"]:
        story.append(P("Dữ liệu và quy trình phân tích", "H2"))
        story.append(P(paper["methods"]))
    if ds.get("source_url"):
        story.append(P(f"Nguồn dữ liệu: {ds['source_url']}"))
    if ds.get("rows_in_selected_split") is not None:
        story.append(P(f"Số dòng của split đã chọn: {ds['rows_in_selected_split']:,}; "
                       f"số dòng được phân tích: {ds.get('rows_analyzed', 0):,}."))

    # ---- 5. Results ----
    story.append(P("5. Kết quả (Results)", "H1"))
    for text in paper["results_paras"]:
        story.append(P(text))
    story.extend(_results_tables(snap, styles))

    # ---- 6. Discussion ----
    d = paper["discussion"]
    story.append(P("6. Thảo luận (Discussion)", "H1"))
    story.append(P("Diễn giải kết quả chính", "H2"))
    story.append(P(d["key_findings_paragraph"]))
    story.append(P("So sánh với các nghiên cứu trước", "H2"))
    story.append(P(d["literature_comparison_paragraph"]))
    if paper["assessments"]:
        story.append(P("Đánh giá các giả thuyết", "H2"))
        stmt = {h["label"]: h["statement"] for h in rq["hypotheses"]}
        rows = [[a["label"], stmt.get(a["label"], ""), _VERDICT_VI.get(a["verdict"], a["verdict"]), a["explanation"]]
                for a in paper["assessments"]]
        story.extend(_table_block("Bảng tổng hợp đánh giá giả thuyết",
                                  ["Giả thuyết", "Phát biểu", "Kết luận", "Giải thích"], rows, [1, 3, 1.6, 4], styles))
    story.append(P("Ý nghĩa của nghiên cứu", "H2"))
    story.append(P(d["implications_paragraph"]))
    story.append(P("Hạn chế", "H2"))
    story.append(P(d["limitations_paragraph"]))
    tech_limits = snap.get("limitations_from_analysis") or []
    if tech_limits:
        story.append(ListFlowable(
            [ListItem(Paragraph(_esc(t), styles["Body"])) for t in tech_limits], bulletType="bullet"))
    story.append(P("Hướng nghiên cứu tiếp theo", "H2"))
    story.append(P(d["future_work_paragraph"]))

    # ---- 7. Conclusion ----
    story.append(P("7. Kết luận (Conclusion)", "H1"))
    story.append(P(d["conclusion_paragraph"]))

    # ---- References (đánh số theo thứ tự xuất hiện) ----
    story.append(P("Tài liệu tham khảo (References)", "H1"))
    if paper["references"]:
        for n, ref in paper["references"]:
            story.append(P(_format_reference(ref, label=n), "Reference"))
    else:
        story.append(P("(Bài không có citation hợp lệ.)"))

    # ---- Ghi chú dành cho tác giả (trang cuối) ----
    story.append(PageBreak())
    story.append(P("Ghi chú dành cho tác giả (xoá trang này trước khi nộp)", "H1"))
    story.append(ListFlowable(
        [ListItem(Paragraph(_esc(n), styles["Body"])) for n in paper["notes"]], bulletType="bullet"))

    doc.build(story, onFirstPage=_page_decorations, onLaterPages=_page_decorations)


@router.post("/proposal/{research_question_id}")
async def export_proposal_pdf(research_question_id: int):
    """Xuất PDF Research Proposal cho 1 research question đã có đủ Introduction + Related Work."""
    data = await _load_proposal_data(research_question_id)

    output_dir = Path("/tmp/research_copilot_exports")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"proposal_rq_{research_question_id}.pdf"

    try:
        _build_pdf(data, output_path)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Lỗi khi build PDF: %s", exc)
        raise HTTPException(status_code=500, detail=f"Lỗi khi tạo PDF: {exc}")

    return FileResponse(
        path=str(output_path),
        media_type="application/pdf",
        filename=f"research_proposal_rq_{research_question_id}.pdf",
    )


@router.post("/paper/{research_question_id}")
async def export_paper_pdf(research_question_id: int):
    """Xuất bài báo hoàn chỉnh (IMRaD) cho 1 research question đã có Introduction, Related Work
    và Discussion/Conclusion (kèm kết quả phân tích dữ liệu)."""
    data = await _load_paper_data(research_question_id)

    output_dir = Path("/tmp/research_copilot_exports")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"paper_rq_{research_question_id}.pdf"

    try:
        _build_paper_pdf(data, output_path)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Lỗi khi build PDF bài báo: %s", exc)
        raise HTTPException(status_code=500, detail=f"Lỗi khi tạo PDF: {exc}")

    return FileResponse(
        path=str(output_path),
        media_type="application/pdf",
        filename=f"research_paper_rq_{research_question_id}.pdf",
    )