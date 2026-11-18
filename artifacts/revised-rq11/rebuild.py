"""Rebuild the user-supplied manuscript with clearer, reproducible descriptive results."""
import asyncio
import json
import re
import os
from pathlib import Path
import pymupdf
os.environ.setdefault("DEEPSEEK_API_KEY", "unused-local-rendering-key")  # This script makes no LLM calls.
from app.data_research.repositories import download
from app.data_research.service import analyze_dataframe
from app.routers.discussion import _compact_analysis
from app.routers.export import _build_paper_pdf

SOURCE = Path(r"C:\Users\Lenovo\Downloads\research_paper_rq_11.pdf")
OUT = Path(__file__).parent
TOPIC = "Tác động của AI tới kết quả học tập của sinh viên đại học Việt Nam"


def section(text, start, end):
    return text.split(start, 1)[1].split(end, 1)[0].strip()


async def main():
    document = pymupdf.open(SOURCE)
    blocks = []
    for page in document:
        for block in page.get_text("blocks"):
            text = re.sub(r"\s+", " ", block[4]).strip()
            if text.isdigit() or ("Bài báo nghiên cứu" in text and len(text) < 120):
                continue
            blocks.append(text)
    text = "\n\n".join(blocks)
    intro = section(text, "1. Mở đầu", "2. Tổng quan nghiên cứu và cơ sở lí luận")
    related = section(text, "2. Tổng quan nghiên cứu và cơ sở lí luận", "3. Câu hỏi nghiên cứu")
    theme_chunks = re.split(r"(?:^|\n\n)2\.\d+\.\s+", related)[1:]
    titles = ["Việc sử dụng AI và các kết quả học tập tích cực",
        "Cơ chế trung gian: sự gắn kết, niềm tin vào năng lực bản thân, tự điều chỉnh và quản lý thời gian",
        "Vai trò của niềm tin vào năng lực bản thân và hiểu biết AI đối với thành tích học tập",
        "Điều kiện điều tiết: cảm xúc đối với AI, hỗ trợ giảng viên, cường độ sử dụng, năm học và liêm chính học thuật",
        "Mặt trái của AI: sử dụng ChatGPT cưỡng chế, đau khổ tâm lý và căng thẳng công nghệ",
        "Thách thức và bất bình đẳng: quyền riêng tư, năng lực số, phụ thuộc quá mức và chênh lệch giữa gắn kết và kết quả học tập"]
    themes = []
    for title, chunk in zip(titles, theme_chunks):
        body = chunk.split("\n\n", 1)[1]
        if "Kết quả giữa các nghiên cứu trước" in body:
            body = body.split("Kết quả giữa các nghiên cứu trước", 1)[0].strip()
        themes.append({"theme_name": title, "paragraph": body})
    assert len(themes) == 6
    contrast = "Kết quả giữa các nghiên cứu trước" + section(related, "Kết quả giữa các nghiên cứu trước", "Tổng quan cho thấy")
    objective = "Tổng quan cho thấy" + related.split("Tổng quan cho thấy", 1)[1]
    findings = section(text, "6.1. Diễn giải kết quả và đối chiếu nghiên cứu trước", "6.2. Ý nghĩa của nghiên cứu")
    split_at = findings.index("Mức tự đánh giá tích cực")
    key_findings, comparison = findings[:split_at], findings[split_at:]
    key_findings = re.sub(r"Đáng chú ý,.*?Tuy vậy,", "Danh sách tương quan mạnh nhất là danh sách chọn lọc; việc một cặp không xuất hiện không cho phép kết luận rằng cặp đó không liên hệ hoặc liên hệ yếu. Cần đọc hệ số của chính cặp biến và mô hình phù hợp. Tuy vậy,", key_findings, flags=re.S)
    comparison = re.sub(r"Dữ liệu ở đây,.*?Bên cạnh đó,", "Chưa thể so sánh độ lớn liên hệ giữa các khái niệm dựa vào danh sách các cặp chỉ báo mạnh nhất. Bộ dữ liệu đo cảm nhận về tiến bộ, không phải thành tích khách quan. Bên cạnh đó,", comparison, flags=re.S)
    implications = section(text, "6.2. Ý nghĩa của nghiên cứu", "6.3. Hạn chế và hướng nghiên cứu tiếp theo")
    limitations = section(text, "6.3. Hạn chế và hướng nghiên cứu tiếp theo", "Hướng tiếp theo nên")
    limitations = limitations.split("•", 1)[0].strip()
    future = "Hướng tiếp theo nên" + section(text, "Hướng tiếp theo nên", "7. Kết luận")
    conclusion = section(text, "7. Kết luận", "TÀI LIỆU THAM KHẢO")
    conclusion = conclusion.replace("và chưa có mối liên kết trực tiếp nổi bật giữa nhận thức về giá trị AI và tiến bộ học tập/nghề nghiệp tự báo cáo", "và danh sách tương quan chọn lọc chưa đủ để đánh giá đầy đủ liên hệ giữa nhận thức về giá trị AI và tiến bộ tự báo cáo")
    references = []
    raw_refs = text.split("TÀI LIỆU THAM KHẢO", 1)[1]
    for match in re.finditer(r"(?:^|\n\n)\[(\d+)\]\s*(.*?)(?=\n\n\[\d+\]|\Z)", raw_refs, re.S):
        ref = re.sub(r"\s+", " ", match[2]).strip()
        author_year = re.match(r"(.*?)\s*\((\d{4})\)\.\s*(.*)", ref)
        assert author_year
        doi = re.search(r"https://doi.org/(\S+)", ref)
        title = re.sub(r"https://doi.org/\S+", "", author_year[3]).strip().rstrip(".")
        references.append({"id": int(match[1]), "authors": [author_year[1]], "year": int(author_year[2]),
                           "title": title, "venue": None, "doi": doi[1] if doi else None})
    assert len(references) == 9
    frame, meta = await download("mendeley:9krwng5dm7:1")
    groups = [("PEVAI", "Giá trị giáo dục cảm nhận của AI", "Các mục đo nhận thức về tính hữu ích và giá trị giáo dục của hệ thống AI."),
              ("SL", "Học tập thông minh", "Các mục thuộc nhóm học tập thông minh; chưa có nội dung từng câu hỏi trong bản xuất này."),
              ("PACP", "Tiến bộ học tập và nghề nghiệp tự cảm nhận", "Tiến bộ do sinh viên tự cảm nhận, không phải điểm học tập khách quan."),
              ("MWB", "Trạng thái tinh thần", "Các mục về trạng thái tinh thần; không dùng để chẩn đoán sức khỏe tâm thần.")]
    guide = {"overview_vi": "Bộ dữ liệu công khai gồm 1.274 sinh viên đại học Việt Nam đã có kinh nghiệm dùng AI trong học tập. Các mục thang đo sử dụng mức trả lời 1–5 theo mô tả nguồn. Dữ liệu lấy mẫu thuận tiện, không đại diện cho toàn bộ sinh viên Việt Nam. Nhóm PACP đo tiến bộ học tập/nghề nghiệp tự cảm nhận, không phải năng suất khách quan hay điểm trung bình được xác minh.",
             "groups": [], "variables": [], "source": "Public Mendeley metadata; construct meanings only, item wording unavailable"}
    for prefix, title, explanation in groups:
        columns = [c for c in frame if re.fullmatch(prefix + r"\d+", c)]
        guide["groups"].append({"label_vi": title, "columns": columns, "description_vi": explanation})
        guide["variables"] += [{"name": c, "label_vi": title + " – mục " + c[len(prefix):], "kind": "continuous"} for c in columns]
    categorical = {"GENDER": "Giới tính", "YEAR_STUDY": "Năm học", "UNIV_TYPE": "Loại trường", "MAJOR": "Nhóm ngành", "AI_USE_DURATION": "Thời gian sử dụng AI (nhóm mã hóa)"}
    guide["variables"] += [{"name": c, "label_vi": title, "kind": "categorical"} for c, title in categorical.items()]
    codes = {"GENDER": {"0": "Nam", "1": "Nữ"}, "YEAR_STUDY": {"2": "Năm thứ hai", "3": "Năm thứ ba", "4": "Năm thứ tư", "5": "Năm khác"},
        "UNIV_TYPE": {"0": "Trường tư", "1": "Trường công"}, "MAJOR": {"1": "Giáo dục", "2": "Kinh tế", "3": "Ngôn ngữ", "4": "Kỹ thuật và công nghệ", "5": "Ngành khác"},
        "AI_USE_DURATION": {"1": "Dưới một năm", "2": "Từ một đến dưới hai năm", "3": "Từ hai năm trở lên"}}
    for variable in guide["variables"]:
        if variable["name"] in codes:
            variable["value_labels"] = codes[variable["name"]]
    meta.update(research_topic=TOPIC, variable_guide=guide)
    result = analyze_dataframe(frame, meta, column_roles={c: "categorical" for c in categorical})
    snapshot = _compact_analysis(result) | {"methods_draft": result["methods_draft"], "results_draft": result["results_draft"]}
    intro_keys = ["background", "problem_statement", "existing_knowledge", "literature_limitation", "research_gap_statement", "research_question_statement", "contribution_statement"]
    paragraphs = intro.split("\n\n")
    introduction = {k: paragraphs[i] if i < len(paragraphs) else "" for i, k in enumerate(intro_keys)}
    if len(paragraphs) > len(intro_keys):
        introduction[intro_keys[-1]] = "\n\n".join(paragraphs[len(intro_keys)-1:])
    rq_text = "Việc sử dụng công cụ AI có liên quan như thế nào đến kết quả học tập tự báo cáo của sinh viên đại học Việt Nam, và mối liên hệ này thay đổi ra sao khi kiểm soát đặc điểm nhân khẩu học, năm học và mức độ sử dụng AI?"
    author = [line.strip() for line in document[0].get_text().split("TÓM TẮT", 1)[0].splitlines() if line.strip()][-1]
    data = {"project_topic": TOPIC, "article_authors": [{"name": author}], "keywords": ["Sinh viên", "AI", "Học tập", "Kết quả tự báo cáo"],
        "rq": {"rq_text": rq_text, "is_quantitative": True, "variables": [], "hypotheses": []},
        "introduction": introduction, "related_work": {"theme_paragraphs": themes, "contradictions_paragraph": contrast, "gap_paragraph": objective},
        "discussion": {"key_findings_paragraph": key_findings, "literature_comparison_paragraph": comparison,
            "implications_paragraph": implications, "limitations_paragraph": limitations, "future_work_paragraph": future,
            "conclusion_paragraph": conclusion, "hypothesis_assessments": [], "analysis_snapshot": snapshot}, "references": references}
    _build_paper_pdf(data, OUT / "research_paper_rq_11_da_sua.pdf")
    (OUT / "reproducible-input.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    pdf = pymupdf.open(OUT / "research_paper_rq_11_da_sua.pdf")
    for i, page in enumerate(pdf):
        if "5. Kết quả nghiên cứu" in page.get_text() or "Hình 1." in page.get_text():
            page.get_pixmap(matrix=pymupdf.Matrix(1.1, 1.1)).save(OUT / f"page-{i+1}.png")
    print("Saved corrected PDF:", len(pdf), "pages; original references:", len(references), "; recomputed observations:", len(frame))


if __name__ == "__main__":
    asyncio.run(main())
