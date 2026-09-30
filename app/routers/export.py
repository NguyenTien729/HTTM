"""
Module Export: xuất "Research Proposal" ra PDF.

CHỦ ĐỊNH THIẾT KẾ QUAN TRỌNG: đây là bản PDF dạng "Research Proposal" (đề xuất
nghiên cứu), KHÔNG PHẢI "bài báo hoàn chỉnh". Lý do: Methods trong bản này là
phương pháp DỰ KIẾN sẽ dùng (rút ra từ variables/hypotheses đã sinh), không phải
đã chạy thật. KHÔNG có Results — vì hệ thống chưa có module Data Research (chưa
có dataset/thí nghiệm thật). Nếu ép sinh Results lúc này sẽ là AI tự bịa số liệu —
đúng thứ toàn bộ hệ thống được thiết kế để tránh (xem nguyên tắc ở module 9 trong
kế hoạch gốc: "Không để LLM tự quyết định và tự chạy code").

Ráp nối dữ liệu ĐÃ CÓ SẴN trong DB (không gọi LLM thêm ở bước này):
    Research Question + Hypotheses (module RQ Generator)
    Introduction (module Scientific Writer)
    Related Work (module Scientific Writer)
    Danh sách Reference (paper đã cite, lấy DOI thật từ bảng papers)
        ↓
    reportlab dựng PDF, dùng font NotoSans (Unicode, hỗ trợ dấu tiếng Việt)
"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.colors import HexColor
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem
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


def _format_reference(paper: dict) -> str:
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
    return f"[{paper['id']}] {author_str} ({year}). {title}.{venue_part}{doi_part}"


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