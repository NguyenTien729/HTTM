"""Six research roles, backed by existing HTTM services and recorded artifacts."""
import asyncio
import json

from fastapi import HTTPException

from app.db import get_pool
from app.services.llm import generate_structured
from app.services.vietnamese_writing import VIETNAMESE_WRITING_RULE
from . import data
from .models import ResearchBrief, AnalysisDesign, FrontMatter, ScientificAudit
from .state import fingerprint


def dumped(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def approved(run, stage):
    current = run["artifacts"].get(stage)
    if not any(a["stage"] == stage and a.get("fingerprint") == fingerprint(current)
               for a in run["artifacts"].get("approvals", [])):
        raise ValueError("Chưa có phê duyệt cho phiên bản " + stage + " hiện tại.")


async def evidence(project_id):
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("""SELECT p.id,p.title,p.year,p.doi,pa.method,pa.population,
            pa.context,pa.key_finding,pa.limitation,p.abstract FROM papers p
            JOIN paper_analyses pa ON pa.paper_id=p.id WHERE p.project_id=$1 ORDER BY p.id LIMIT 50""", project_id)
    return [dict(r) for r in rows]


async def research_plan(run):
    corpus = await evidence(run["project_id"])
    if not corpus:
        raise ValueError("Chưa có tài liệu đã đọc làm bằng chứng; không thể tự bịa khoảng trống.")
    brief = await generate_structured(
        VIETNAMESE_WRITING_RULE + "Lập hồ sơ nghiên cứu định lượng bằng dữ liệu thứ cấp. "
        "Đề tài bất biến: " + run["artifacts"]["topic"] + "\n"
        + "Địa bàn người dùng yêu cầu: " + str(run["artifacts"]["settings"].get("region") or "suy ra từ đề tài") + "\n" +
        "Giữ đúng địa bàn và đối tượng. Đề xuất một câu hỏi và khoảng trống thận trọng dựa trên tài liệu; "
        "không tuyên bố đã chứng minh tính mới hay đã tìm thấy dữ liệu. Giả thuyết là dự kiến, không phải kết quả. "
        "Tách data_needs theo biến kết quả, phơi nhiễm/giải thích, kiểm soát và khóa ghép. "
        "Mỗi biến có đơn vị, độ phân giải không gian/thời gian, key tiếng Anh snake_case, "
        "search_terms tiếng Anh ngắn để tìm dữ liệu riêng của biến đó. Không yêu cầu tên dataset giống toàn bộ đề tài. "
        "geography_terms chỉ gồm các cách gọi tương đương của địa bàn, không đưa khu vực khác hoặc ngôn ngữ thay địa bàn. "
        "evidence_paper_ids chỉ lấy ID trong bằng chứng bên dưới. Nếu cần dữ liệu bảng/chuỗi thời gian, nêu rõ "
        "trong feasibility_notes; hiện phương pháp tự động chỉ có mô tả, tương quan và OLS liên hệ, không nhận diện nhân quả.\n"
        + "Yêu cầu bổ sung của người dùng: " + run["artifacts"].get("feedback", "") + "\n"
        + json.dumps(corpus, ensure_ascii=False, default=str), ResearchBrief)
    valid = {p["id"] for p in corpus}
    if not set(brief.evidence_paper_ids).issubset(valid):
        raise ValueError("Kế hoạch dẫn tài liệu không thuộc bằng chứng đã đọc.")
    return brief.model_dump()


async def design(run):
    approved(run, "research_plan")
    collection = run["artifacts"]["data_collection"]
    if not collection["sources"]:
        proposed = AnalysisDesign(adequate=False, rationale="Chưa có nguồn chứa dữ liệu bảng có thể kiểm tra.",
            missing_requirements=["Cần dữ liệu đo biến theo câu hỏi và địa bàn đã duyệt."], method="unsupported")
    else:
        proposed = await generate_structured(
            VIETNAMESE_WRITING_RULE + "Đề xuất thiết kế phân tích từ dữ liệu THỰC TẾ dưới đây. "
            "Không bịa tên cột, nguồn, đơn vị hay độ bao phủ. adequate=true chỉ khi đủ biến bắt buộc, đúng địa bàn, "
            "đúng cách đo, cùng thời gian và độ phân giải, có thể trả lời câu hỏi ở mức liên hệ quan sát. "
            "Metadata có nhắc địa bàn chưa đủ chứng minh bản ghi thuộc địa bàn. Nếu dữ liệu giả lập, "
            "instruction tuning hoặc ảnh không có biến định lượng phù hợp thì adequate=false. "
            "Đọc ý nghĩa và giá trị mẫu để phân biệt mã số với đại lượng. "
            "Chọn tối đa 3 source_key có thật, ánh xạ bindings từ column sang key data_needs. "
            "Nếu nguồn chứa nhiều địa bàn hoặc thời kỳ, khai báo filters trước ánh xạ bằng cột gốc, "
            "operator eq/in/between và values chính xác có trong nguồn, kèm rationale. Không đoán mã quốc gia. "
            "Không có cách lọc đúng địa bàn/thời kỳ thì adequate=false. "
            "Khai báo source_unit và target_unit trùng đơn vị trong kế hoạch; mọi đổi đơn vị phải có lý do, scale/offset rõ. "
            "Chỉ dùng transform identity/numeric/year/strip/casefold. year chỉ cho cột ngày tháng, "
            "cột năm dạng số dùng numeric. Không ghép theo số thứ tự dòng. "
            "joins ghép lần lượt vào nguồn đầu tiên bằng các key chung. Chỉ one_to_one hoặc many_to_one, "
            "không ghép nhiều-nhiều, không lặp biến ngoài khóa ghép. "
            "method chỉ descriptive/correlation/ols; nếu câu hỏi bắt buộc mô hình panel, time-series, spatial "
            "hoặc thiết kế nhân quả mà OLS không phù hợp, chọn unsupported và ghi missing_requirements. "
            "Không coi các giả định là đã kiểm chứng. causal_claims_allowed=false.\n"
            + json.dumps(run["artifacts"]["research_plan"], ensure_ascii=False) + "\n"
            + "Phản hồi người dùng: " + run["artifacts"].get("feedback", "") + "\n"
            + data.design_context(collection), AnalysisDesign)
    brief = ResearchBrief.model_validate(run["artifacts"]["research_plan"])
    known = {s["source_key"]: s for s in collection["sources"]}
    columns = {key: {c["name"] for c in source.get("columns", [])} for key, source in known.items()}
    coverage = {need.key: {"key": need.key, "concept": need.concept, "role": need.role,
                           "unit": need.unit, "resolution": need.resolution,
                           "required": need.required, "sources": []}
                for need in brief.data_needs}
    candidate = proposed.model_dump()
    invalid_bindings = []
    for selection in proposed.sources:
        for binding in selection.bindings:
            if binding.variable not in coverage:
                invalid_bindings.append({"source_key": selection.source_key, "variable": binding.variable,
                                         "column": binding.column, "reason": "Biến không thuộc kế hoạch."})
            elif selection.source_key not in known:
                invalid_bindings.append({"source_key": selection.source_key, "variable": binding.variable,
                                         "column": binding.column, "reason": "Nguồn không tồn tại trong snapshot."})
            elif binding.column not in columns[selection.source_key]:
                invalid_bindings.append({"source_key": selection.source_key, "variable": binding.variable,
                                         "column": binding.column, "reason": "Cột không có trong snapshot."})
            else:
                coverage[binding.variable]["sources"].append({"source_key": selection.source_key,
                                                               "column": binding.column})
    uncovered = [row for row in coverage.values() if row["required"] and not row["sources"]]
    validation = {"ok": False, "errors": []}
    if uncovered:
        validation["errors"].append("Snapshot chưa phủ đủ biến bắt buộc ở đúng độ phân giải.")
    if invalid_bindings:
        validation["errors"].append("Có ánh xạ trỏ tới biến, nguồn hoặc cột không hợp lệ.")
    if not uncovered and not invalid_bindings and proposed.adequate and proposed.method != "unsupported":
        try:
            _, validation = await asyncio.to_thread(data.prepare_data, run["id"],
                run["artifacts"]["research_plan"], candidate, collection,
                run["artifacts"]["settings"]["max_rows"])
        except (ValueError, KeyError, TypeError) as exc:
            validation = {"ok": False, "errors": [str(exc)]}
    feasible = bool(validation.get("ok") and proposed.adequate and proposed.method != "unsupported")
    if not feasible:
        missing = [f"{row['concept']} ({row['key']}): cần {row['unit']}, độ phân giải {row['resolution']}"
                   for row in uncovered]
        missing += proposed.missing_requirements
        if not missing:
            missing = validation.get("errors") or ["Phương pháp hiện có không đáp ứng câu hỏi nghiên cứu đã duyệt."]
        safe = AnalysisDesign(adequate=False,
            rationale="Chưa có phương án phân tích khả thi: snapshot phải được đối chiếu với từng biến bắt buộc, đơn vị và độ phân giải trước khi phân tích. Các ánh xạ thử được lưu riêng để rà soát.",
            missing_requirements=list(dict.fromkeys(missing)), method="unsupported",
            limitations=proposed.limitations, assumptions=proposed.assumptions)
        validation.update({"ok": False, "coverage": list(coverage.values()),
                           "invalid_bindings": invalid_bindings,
                           "candidate_mappings": candidate.get("sources", []),
                           "next_actions": ["Tìm lại nguồn cho các biến còn thiếu; nguồn đã kiểm tra sẽ được loại khỏi lượt tìm lại.",
                                            "Đề xuất phương pháp phù hợp với dữ liệu hiện có.",
                                            "Điều chỉnh câu hỏi hoặc yêu cầu độ phân giải nếu bạn chấp thuận thay đổi phạm vi."]})
        result = safe.model_dump()
    else:
        validation["coverage"] = list(coverage.values())
        result = candidate
    run["artifacts"]["design_validation"] = validation
    return result


async def analyze(run):
    from app.data_research.service import analyze_dataframe
    approved(run, "research_plan")
    approved(run, "design")
    a = run["artifacts"]
    spec = AnalysisDesign.model_validate(a["design"])
    frame, report = await asyncio.to_thread(data.prepare_data, run["id"], a["research_plan"],
        a["design"], a["data_collection"], a["settings"]["max_rows"])
    roles = {n["key"]: "identifier" if n["role"] == "join_key" else "continuous"
             for n in a["research_plan"]["data_needs"] if n["key"] in frame}
    provenance = report["provenance"]
    metadata = {"dataset_id": " + ".join(s["dataset_id"] for s in provenance),
        "source": " + ".join(sorted({s["candidate"].get("source", "Public dataset") for s in a["data_collection"]["sources"]
                                      if s["source_key"] in {p["source_key"] for p in provenance}})),
        "research_topic": a["topic"], "research_question": a["research_plan"]["question"],
        "research_gap": {"description": a["research_plan"]["gap"]},
        "project_id": run["project_id"], "config": "approved-source-snapshots", "split": "approved-join",
        "sampled": any(s["sampled"] for s in provenance),
        "sampling_method": "Giới hạn mẫu theo từng nguồn; xem source_provenance.",
        "source_provenance": provenance, "join_report": report["joins"], "rows_in_selected_split": len(frame)}
    result = await asyncio.to_thread(analyze_dataframe, frame, metadata,
        target_column=spec.target, predictor_columns=spec.predictors if spec.method == "ols" else [],
        column_roles=roles, correlation_columns=[spec.target]+spec.predictors if spec.method in ("ols", "correlation") else [])
    if spec.method == "ols" and result.get("ols_regression", {}).get("status") != "ok":
        raise ValueError("Hồi quy không chạy thành công; không được viết kết quả thay thế.")
    result["approved_design"] = a["design"]
    import platform
    import importlib.metadata
    import hashlib
    from pathlib import Path
    code = [Path(__file__), Path(data.__file__), Path(__file__).parents[1] / "data_research" / "service.py"]
    result["reproducibility"] = {"python": platform.python_version(),
        "packages": {name: importlib.metadata.version(name) for name in ("pandas", "numpy", "scipy", "pyarrow")},
        "code_sha256": {str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(p.read_bytes()).hexdigest() for p in code},
        "research_plan_sha256": fingerprint(a["research_plan"]), "design_sha256": fingerprint(a["design"])}
    result["methods_draft"] += " Phương án chuẩn hóa và kết hợp nguồn đã được người dùng duyệt. "
    for source in provenance:
        result["methods_draft"] += (f"Nguồn {source['dataset_id']} ({source['dataset_url']}) cung cấp "
            f"{source['rows_before_filter']} dòng snapshot; {source['rows']} dòng đáp ứng điều kiện chọn mẫu. ")
        for rule in source["filters"]:
            result["methods_draft"] += rule["rationale"] + ". "
    for join in report["joins"]:
        result["methods_draft"] += ("Dữ liệu được ghép trái theo " + ", ".join(join["keys"]) +
            f"; {join['matched_rows']} dòng khớp và {join['unmatched_rows']} dòng không khớp. ")
    result["methods_draft"] += "Các phép phân tích chỉ đánh giá mối liên hệ quan sát, không xác định tác động nhân quả."
    return result


async def ensure_question(run):
    """Record the approved gap/question once; legacy writers use this stable ID."""
    a = run["artifacts"]
    if a.get("research_question_id"):
        return a["research_question_id"]
    approved(run, "research_plan")
    brief = ResearchBrief.model_validate(a["research_plan"])
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT version,status,artifacts FROM autonomous_runs WHERE id=$1 FOR UPDATE", run["id"])
            if not row or row["version"] != run["version"] or row["status"] != "running":
                raise ValueError("Tiến trình đã bị thay đổi hoặc dừng.")
            stored = row["artifacts"]
            stored = json.loads(stored) if isinstance(stored, str) else stored
            if stored.get("research_question_id"):
                a["research_question_id"] = stored["research_question_id"]
                return a["research_question_id"]
            gap_id = await conn.fetchval("""INSERT INTO gaps(project_id,gap_type,description,evidence_paper_ids,
                proposed_research_question,confirmed) VALUES($1,'dataset',$2,$3::jsonb,$4,true) RETURNING id""",
                run["project_id"], brief.gap, json.dumps(brief.evidence_paper_ids), brief.question)
            rq_id = await conn.fetchval("""INSERT INTO research_questions(project_id,gap_id,rq_text,
                feasibility_score,feasibility_notes,novelty_score,novelty_notes,is_quantitative,variables,hypotheses)
                VALUES($1,$2,$3,3,$4,1,$5,true,$6::jsonb,$7::jsonb) RETURNING id""",
                run["project_id"], gap_id, brief.question, brief.feasibility_notes,
                "Khoảng trống đề xuất từ tập tài liệu đã đọc; chưa đánh giá độc lập tính mới.",
                json.dumps([v.model_dump() for v in brief.variables], ensure_ascii=False),
                json.dumps([h.model_dump() for h in brief.hypotheses], ensure_ascii=False))
            a["research_question_id"] = rq_id
            await conn.execute("UPDATE autonomous_runs SET artifacts=$2::jsonb WHERE id=$1", run["id"],
                               json.dumps(a, ensure_ascii=False))
    return rq_id


async def manuscript(run):
    a = run["artifacts"]
    front = await generate_structured(VIETNAMESE_WRITING_RULE +
        "Viết title, abstract, keywords cho bản thảo dưới đây. Không tạo tác giả. "
        "Abstract chỉ dùng số liệu từ kết quả thực tế, không suy luận nhân quả, không thêm số từ literature. "
        "Không tự nhận là nghiên cứu đầu tiên. Nêu giới hạn quan trọng và phạm vi mẫu.\n" +
        json.dumps({"topic": a["topic"], "brief": a["research_plan"],
                    "results": a["analysis"]["results_draft"], "discussion": a["discussion"],
                    "feedback": a.get("feedback", "")}, ensure_ascii=False, default=str), FrontMatter)
    intro = a["introduction"]["introduction"]
    work = a["related_work"]["related_work"]
    disc = a["discussion"]["discussion"]
    refs = await evidence(run["project_id"])
    # Preserve canonical [paper_id] references; never fabricate author metadata.
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT id,title,authors,year,doi,venue FROM papers WHERE project_id=$1 ORDER BY id", run["project_id"])
    sections = {"Introduction": "\n\n".join(intro[k] for k in (
        "background", "problem_statement", "existing_knowledge", "literature_limitation",
        "research_gap_statement", "research_question_statement", "contribution_statement")),
        "Related Work": "\n\n".join([t["paragraph"] for t in work["theme_paragraphs"]] +
                                     [work["contradictions_paragraph"], work["gap_paragraph"]]),
        "Research Question & Hypotheses": a["research_plan"]["question"] + "\n\n" + "\n".join(
            h["label"] + ": " + h["statement"] for h in a["research_plan"]["hypotheses"]),
        "Methods": a["analysis"]["methods_draft"], "Results": a["analysis"]["results_draft"],
        "Discussion": "\n\n".join(disc[k] for k in ("key_findings_paragraph", "literature_comparison_paragraph",
            "implications_paragraph", "limitations_paragraph", "future_work_paragraph")),
        "Conclusion": disc["conclusion_paragraph"]}
    if disc["hypothesis_assessments"]:
        sections["Discussion"] += "\n\n" + "\n\n".join(
            assessment["label"] + ": " + assessment["verdict"] + ". " + assessment["explanation"]
            for assessment in disc["hypothesis_assessments"])
    from app.services.citation_checker import extract_cited_ids
    cited = set(extract_cited_ids("\n".join(sections.values()) + front.abstract))
    references = [dict(row) for row in rows if row["id"] in cited]
    for ref in references:
        if isinstance(ref["authors"], str):
            ref["authors"] = json.loads(ref["authors"])
    return front.model_dump() | {"authors": a["settings"]["authors"], "sections": sections,
        "references": references, "tables": a["analysis"].get("column_summaries", {}),
        "charts": a["analysis"].get("charts", {}), "evidence": refs,
        "note": "Bản thảo tự động, cần kiểm chứng và phê duyệt; thiếu tác giả nếu người dùng chưa cung cấp."}


async def verify(run):
    from app.services.citation_checker import check_text
    from app.routers.discussion import _find_unverified_numbers
    a = run["artifacts"]
    draft = a["manuscript"]
    valid = {p["id"] for p in draft["evidence"]}
    issues, citations = [], {}
    for name, text in {"Abstract": draft["abstract"], **draft["sections"]}.items():
        report = check_text(text, valid)
        citations[name] = report
        if report["invalid_ids"]:
            issues.append({"section": name, "claim": str(report["invalid_ids"]),
                           "reason": "Trích dẫn không thuộc tài liệu đã đọc.", "severity": "error"})
    for name, part in (("Introduction", a["introduction"]["introduction"]),
                       ("Related Work", a["related_work"]["related_work"]),
                       ("Discussion", a["discussion"]["discussion"])):
        if part.get("citations_invalid"):
            issues.append({"section": name, "claim": str(part["citations_invalid"]),
                           "reason": "Trích dẫn không được bằng chứng của bước viết hỗ trợ.", "severity": "error"})
    if a["discussion"]["discussion"].get("numbers_unverified"):
        issues.append({"section": "Discussion", "claim": str(a["discussion"]["discussion"]["numbers_unverified"]),
                       "reason": "Số liệu chưa đối chiếu được với phân tích.", "severity": "error"})
    unverified = _find_unverified_numbers([draft["abstract"]], json.dumps(a["analysis"], ensure_ascii=False))
    if unverified:
        issues.append({"section": "Abstract", "claim": str(unverified),
                       "reason": "Số trong abstract chưa đối chiếu được với kết quả.", "severity": "error"})
    # Re-run deterministic hash/join checks against the approved snapshots.
    _, source_report = await asyncio.to_thread(data.prepare_data, run["id"], a["research_plan"],
        a["design"], a["data_collection"], a["settings"]["max_rows"])
    audit = await generate_structured(VIETNAMESE_WRITING_RULE +
        "Kiểm chứng bản thảo với nguồn bằng chứng cung cấp. Đây là kiểm tra tự động, không phải bình duyệt độc lập. "
        "Phát hiện tuyên bố thiếu bằng chứng, trích dẫn có thật nhưng không hỗ trợ phát biểu, "
        "đánh tráo khái niệm/địa bàn, số liệu sai, khẳng định nhân quả từ liên hệ, "
        "khẳng định tính mới chưa được chứng minh. Ghi rõ section, claim, reason, severity error/warning. "
        "Không báo thiếu citation cho số liệu do chính phân tích tạo ra.\n" +
        json.dumps({"manuscript": draft, "design": a["design"], "analysis": a["analysis"]},
                   ensure_ascii=False, default=str), ScientificAudit)
    issues += [i.model_dump() for i in audit.issues]
    return {"ok": not any(i["severity"] == "error" for i in issues), "issues": issues,
        "citation_reports": citations, "source_checks": source_report,
        "limitations": audit.limitations + ["Kiểm chứng ngữ nghĩa dùng LLM; cần người nghiên cứu xem lại nguồn gốc."]}


async def execute_stage(run):
    from app.routers import search, analysis, synthesis, writer, related_work, discussion
    from app.models import WriteDiscussionRequest
    stage, pid = run["stage"], run["project_id"]
    if stage == "literature_search":
        result = await search.run_search(pid)
        if not result.found:
            raise ValueError("Chưa tìm được tài liệu phù hợp; cần điều chỉnh kế hoạch tìm tài liệu.")
        return dumped(result)
    if stage == "literature_read":
        result = await analysis.analyze_papers(pid, use_fulltext=True)
        if not result.total_analyzed:
            raise ValueError("Không đọc được bằng chứng từ tài liệu đã tìm.")
        return dumped(result)
    if stage == "literature_synthesis":
        return dumped(await synthesis.synthesize_themes(pid))
    if stage == "research_plan":
        return await research_plan(run)
    if stage == "data_collection":
        approved(run, "research_plan")
        from .store import collection_progress
        async def progress(value):
            if not await collection_progress(run, value):
                raise asyncio.CancelledError()
        return await data.collect(run, progress=progress)
    if stage == "design":
        return await design(run)
    if stage == "analysis":
        return await analyze(run)
    if stage in ("introduction", "related_work", "discussion"):
        approved(run, "design")
        rq_id = await ensure_question(run)
        if stage == "introduction":
            return dumped(await writer.write_introduction(rq_id, revision_notes=run["artifacts"].get("feedback", "")))
        if stage == "related_work":
            return dumped(await related_work.write_related_work(rq_id, revision_notes=run["artifacts"].get("feedback", "")))
        return dumped(await discussion.write_discussion(rq_id, WriteDiscussionRequest(
            analysis_result=run["artifacts"]["analysis"], revision_notes=run["artifacts"].get("feedback", ""))))
    if stage == "manuscript":
        return await manuscript(run)
    if stage == "verification":
        return await verify(run)
    raise ValueError("Bước nghiên cứu không được hỗ trợ.")
