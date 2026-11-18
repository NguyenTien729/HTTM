"""PDF and editable OOXML exports of validated narrative-review versions."""
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from app.routers.export import _paper_styles, _CitationNumbering, _format_reference, _table_block, _PAPER_FONT


def prepared(manuscript):
    numbering = _CitationNumbering({p["id"] for p in manuscript["papers"]})
    sections = []
    n = 0
    for section in manuscript["sections"]:
        if section["key"] != "abstract":
            n += 1
        title = "TÓM TẮT" if section["key"] == "abstract" else f"{n}. {section['title']}"
        sections.append({"key": section["key"], "title": title,
                         "paragraphs": [numbering.apply(p["text"]) for p in section["paragraphs"]]})
    matrix = []
    for paper in manuscript["papers"]:
        kinds = {source["kind"] for source in manuscript["evidence_sources"] if source["paper_id"] == paper["id"]}
        matrix.append([numbering.apply(f"[{paper['id']}]"), str(paper.get("year") or "—"), paper["title"],
                       "Trích đoạn toàn văn" if "fulltext_chunk" in kinds else "Tóm tắt"])
    by_id = {p["id"]: p for p in manuscript["papers"]}
    references = [_format_reference(by_id[pid], label=label) for pid, label in sorted(numbering.mapping.items(), key=lambda pair: pair[1])]
    return sections, matrix, references


def author_lines(options):
    if not options:
        return []
    authors = options.authors
    lines = [", ".join(a.name + ("*" if a.corresponding else "") for a in authors)] if authors else []
    lines += list(dict.fromkeys(a.affiliation for a in authors if a.affiliation))
    lines += [f"*Tác giả liên hệ: {a.name} – Email: {a.email}" for a in authors if a.corresponding and a.email]
    return lines


def build_review_pdf(manuscript, path, options=None):
    styles = _paper_styles()
    sections, matrix, references = prepared(manuscript)
    title = manuscript["title"]
    doc = SimpleDocTemplate(str(path), pagesize=A4, title=title, author=", ".join(a.name for a in options.authors) if options else "",
                            topMargin=2.5*cm, bottomMargin=2.2*cm, leftMargin=2.2*cm, rightMargin=2.2*cm)
    story = [Paragraph("Bài tổng quan tường thuật", styles["ArticleType"]), Paragraph(escape(title.upper()), styles["Title"])]
    story += [Paragraph(escape(line), styles["Subtitle"]) for line in author_lines(options)]
    story.append(Spacer(1, 8))
    for section in sections:
        abstract = section["key"] == "abstract"
        story.append(Paragraph(escape(section["title"]), styles["AbstractHead" if abstract else "H1"]))
        story += [Paragraph(escape(text), styles["Abstract" if abstract else "Body"]) for text in section["paragraphs"]]
        if abstract:
            keywords = options.keywords if options and options.keywords is not None else manuscript.get("keywords", [])
            if keywords:
                story.append(Paragraph("<b>Từ khóa:</b> " + escape("; ".join(keywords)), styles["Abstract"]))
        if section["key"] == "methodology":
            story += _table_block("Bảng 1. Tập tài liệu và phạm vi bằng chứng được đọc",
                                  ["Nguồn", "Năm", "Công trình", "Cơ sở đọc"], matrix, [1, 1, 6, 2], styles,
                                  "Có trích đoạn toàn văn không có nghĩa là đã đọc toàn bộ bài hoặc đánh giá đầy đủ chất lượng nghiên cứu.")
    story.append(Paragraph("TÀI LIỆU THAM KHẢO", styles["H1"]))
    story += [Paragraph(escape(ref), styles["Reference"]) for ref in references]
    if options and options.include_author_notes:
        story.append(Paragraph("Ghi chú dành cho tác giả", styles["H1"]))
        story.append(Paragraph("Bản thảo tổng quan có hỗ trợ tự động; kiểm chứng máy không thay thế rà soát học thuật. Liên kết bằng chứng và nhật ký được lưu cùng phiên bản bản thảo.", styles["Body"]))
    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(_PAPER_FONT, 9)
        canvas.drawCentredString(A4[0]/2, 1.2*cm, str(document.page))
        if document.page > 1:
            canvas.drawString(document.leftMargin, A4[1]-1.6*cm, "Bài tổng quan tường thuật")
            canvas.line(document.leftMargin, A4[1]-1.8*cm, A4[0]-document.rightMargin, A4[1]-1.8*cm)
        canvas.restoreState()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def build_review_docx(manuscript, path, options=None):
    """Standard DOCX package; Word/LibreOffice can edit text, tables and headings."""
    sections, matrix, references = prepared(manuscript)
    ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    def paragraph(text, style="Normal"):
        return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
    body = paragraph(manuscript["title"].upper(), "Title")
    body += "".join(paragraph(line, "Author") for line in author_lines(options))
    body += paragraph("Bài tổng quan tường thuật", "Author")
    for section in sections:
        abstract = section["key"] == "abstract"
        body += paragraph(section["title"], "Heading1")
        body += "".join(paragraph(text, "Abstract" if abstract else "Normal") for text in section["paragraphs"])
        if abstract:
            keywords = options.keywords if options and options.keywords is not None else manuscript.get("keywords", [])
            if keywords:
                body += paragraph("Từ khóa: " + "; ".join(keywords), "Abstract")
        if section["key"] == "methodology":
            body += paragraph("Bảng 1. Tập tài liệu và phạm vi bằng chứng được đọc", "Caption")
            rows = [["Nguồn", "Năm", "Công trình", "Cơ sở đọc"]] + matrix
            body += '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders><w:top w:val="single" w:sz="6"/><w:bottom w:val="single" w:sz="6"/><w:insideH w:val="single" w:sz="2"/></w:tblBorders></w:tblPr><w:tblGrid>' + ''.join(f'<w:gridCol w:w="{width}"/>' for width in [900,900,5400,2100]) + '</w:tblGrid>'
            for i, row in enumerate(rows):
                body += '<w:tr>' + ('<w:trPr><w:tblHeader/></w:trPr>' if i == 0 else '')
                body += ''.join('<w:tc><w:tcPr/>' + paragraph(str(cell), 'TableText') + '</w:tc>' for cell in row) + '</w:tr>'
            body += '</w:tbl>'
    body += paragraph("TÀI LIỆU THAM KHẢO", "Heading1")
    body += ''.join(paragraph(ref, "Reference") for ref in references)
    if options and options.include_author_notes:
        body += paragraph("Ghi chú dành cho tác giả", "Heading1")
        body += paragraph("Bản thảo tổng quan có hỗ trợ tự động; kiểm chứng máy không thay thế rà soát học thuật. Liên kết bằng chứng và nhật ký được lưu cùng phiên bản bản thảo.")
    body += '<w:sectPr><w:footerReference w:type="default" r:id="footer"/><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1417" w:bottom="1247" w:left="1247" w:right="1247" w:footer="600"/></w:sectPr>'
    styles = f'<w:styles xmlns:w="{ns}"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"/><w:sz w:val="24"/></w:rPr></w:rPrDefault></w:docDefaults>'
    for name, properties, run in [
        ("Normal", '<w:jc w:val="both"/><w:ind w:firstLine="450"/><w:spacing w:after="80" w:line="320" w:lineRule="auto"/>', ''),
        ("Title", '<w:jc w:val="center"/><w:keepNext/>', '<w:b/><w:sz w:val="28"/>'),
        ("Author", '<w:jc w:val="center"/><w:keepNext/>', '<w:i/><w:sz w:val="22"/>'),
        ("Heading1", '<w:keepNext/><w:spacing w:before="200" w:after="80"/><w:outlineLvl w:val="0"/>', '<w:b/>'),
        ("Abstract", '<w:jc w:val="both"/><w:spacing w:after="80"/>', '<w:i/><w:sz w:val="22"/>'),
        ("Caption", '<w:keepNext/><w:jc w:val="center"/>', '<w:i/><w:sz w:val="20"/>'),
        ("TableText", '<w:spacing w:after="40"/>', '<w:sz w:val="20"/>'),
        ("Reference", '<w:ind w:left="360" w:hanging="360"/><w:spacing w:after="80"/>', '<w:sz w:val="22"/>'),
    ]:
        styles += f'<w:style w:type="paragraph" w:styleId="{name}"><w:name w:val="{name}"/><w:pPr>{properties}</w:pPr><w:rPr>{run}</w:rPr></w:style>'
    styles += '</w:styles>'
    relns = 'http://schemas.openxmlformats.org/package/2006/relationships'
    with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/></Types>')
        archive.writestr('_rels/.rels', f'<Relationships xmlns="{relns}"><Relationship Id="main" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/_rels/document.xml.rels', f'<Relationships xmlns="{relns}"><Relationship Id="styles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="footer" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/></Relationships>')
        archive.writestr('word/document.xml', f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>{body}</w:body></w:document>')
        archive.writestr('word/styles.xml', styles)
        archive.writestr('word/footer1.xml', f'<w:ftr xmlns:w="{ns}"><w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>PAGE</w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p></w:ftr>')
