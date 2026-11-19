"""Six research roles, backed by existing HTTM services and recorded artifacts."""
import asyncio
import json
import re

from fastapi import HTTPException

from app.db import get_pool
from app.services.llm import generate_structured
from app.services.vietnamese_writing import VIETNAMESE_WRITING_RULE
from . import data
from .models import (ResearchBrief, AnalysisDesign, FrontMatter, ScientificAudit,
                     PaperStory, ProposalDraft, ClaimEvidenceMap, AlternativeQuestions)
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


async def create_proposal_draft(run, blockers, coverage):
    """Create an explicitly non-empirical draft while carrying every blocker forward."""
    brief = ResearchBrief.model_validate(run["artifacts"]["research_plan"])
    papers = await evidence(run["project_id"])
    corpus = [paper for paper in papers if paper["id"] in set(brief.evidence_paper_ids)]
    allowed_ids = {paper["id"] for paper in corpus}
    story = await generate_structured(
        VIETNAMESE_WRITING_RULE +
        "Create the paper story before drafting. This is a research proposal because the approved question does not yet have a verified empirical dataset. "
        "Use only the research plan and read-paper evidence supplied. Do not claim novelty, causality, dataset availability, or findings as established. "
        "Separate the proposed contribution from what literature currently supports. List specific claims to avoid until data/analysis exist.\n" +
        json.dumps({"topic": run["artifacts"]["topic"], "plan": brief.model_dump(), "literature": corpus,
                    "blockers": blockers, "coverage": coverage}, ensure_ascii=False, default=str), PaperStory)
    package = await generate_structured(
        VIETNAMESE_WRITING_RULE +
        "Write a coherent Vietnamese research-proposal draft from the approved plan and paper story. "
        "This is NOT an empirical paper: do not include results, findings, coefficient estimates, or completed-analysis language. "
        "The abstract and methods must state that the study is proposed and not yet run. Keep research scope unchanged. "
        "Cite only paper IDs in the supplied corpus as [id]. Do not cite dataset candidates as evidence. "
        "Use the sections Introduction, Related Work, Proposed Methods, Data and Feasibility, Limitations, Next Steps. "
        "Data and Feasibility must explain the exact blockers without implying the missing data were found. "
        "Build claim_evidence_map for factual claims: supported_literature requires valid evidence_paper_ids; "
        "supported_data is forbidden because no analysis has run; proposed is for future plans; unsupported is for claims lacking evidence. "
        "Never upgrade a proposed claim to a finding.\n" +
        json.dumps({"topic": run["artifacts"]["topic"], "plan": brief.model_dump(), "paper_story": dumped(story),
                    "literature_corpus": corpus, "blockers": blockers, "data_coverage": coverage},
                   ensure_ascii=False, default=str), ProposalDraft)
    draft = dumped(package)
    draft["paper_story"] = dumped(story)
    draft["disclaimer"] = "Bản đề xuất nghiên cứu — chưa có kết quả thực nghiệm. Không dùng như bài báo thực nghiệm hoàn chỉnh."
    draft["blockers"] = list(dict.fromkeys(blockers))
    valid_keys = {source.get("source_key") for source in run["artifacts"].get("data_collection", {}).get("sources", [])}
    for claim in draft["claim_evidence_map"]["claims"]:
        bad_ids = set(claim.get("evidence_paper_ids", [])) - allowed_ids
        bad_keys = set(claim.get("source_keys", [])) - valid_keys
        claim["evidence_paper_ids"] = sorted(set(claim.get("evidence_paper_ids", [])) & allowed_ids)
        # A downloaded snapshot is not yet valid analytical evidence while this
        # design is blocked; only completed analysis can support data claims.
        claim["source_keys"] = []
        if claim["status"] == "supported_data":
            claim["status"] = "unsupported"
            claim["rationale"] += " Chưa có phân tích được duyệt để hỗ trợ luận điểm này."
        if bad_ids or bad_keys or (claim["status"] == "supported_literature" and not claim["evidence_paper_ids"]):
            claim["status"] = "unsupported"
            claim["rationale"] += " Hệ thống không xác nhận được toàn bộ nguồn bằng chứng đã khai báo."
        if claim["status"] == "unsupported":
            draft["blockers"].append("Có luận điểm trong claim-evidence map cần người dùng kiểm tra nguồn.")
    from app.services.citation_checker import extract_cited_ids
    cited = set(extract_cited_ids(draft["abstract"] + "\n" + "\n".join(draft["sections"].values())))
    invalid = set(cited) - allowed_ids
    if invalid:
        pattern = re.compile(r"\[(?:" + "|".join(map(str, sorted(invalid))) + r")\]")
        for key, value in list(draft["sections"].items()):
            draft["sections"][key] = pattern.sub("[citation cần xác minh]", value)
        draft["abstract"] = pattern.sub("[citation cần xác minh]", draft["abstract"])
        draft["blockers"].append("Có citation trong bản đề xuất không thuộc corpus đã đọc; đã đánh dấu để xác minh.")
    cited = set(extract_cited_ids(draft["abstract"] + "\n" + "\n".join(draft["sections"].values())))
    cited.update(paper_id for claim in draft["claim_evidence_map"]["claims"]
                 for paper_id in claim.get("evidence_paper_ids", []))
    draft["references"] = [paper for paper in corpus if paper["id"] in cited]
    run["artifacts"]["paper_story"] = draft["paper_story"]
    run["artifacts"]["claim_evidence_map"] = draft["claim_evidence_map"]
    run["artifacts"]["proposal_draft"] = draft
    run["artifacts"]["paper_state"] = {
        "schema_version": "httm/paper-state-v1", "mode": "exploratory-proposal",
        "stage": "draft_with_blockers", "required_artifacts": ["paper_story", "claim_evidence_map", "proposal_draft"],
        "blockers": draft["blockers"], "build": {"status": "not_run", "format": "Markdown"}}
    return draft


def available_data_context(artifacts):
    """Schema of team uploads and sources kept from a previous collection, for planning."""
    sources = list(artifacts.get("user_sources", []))
    known = {s.get("source_key") for s in sources}
    sources += [s for s in (artifacts.get("data_collection") or {}).get("sources", []) if s.get("source_key") not in known]
    return [{"source_key": s.get("source_key"), "title": s.get("candidate", {}).get("title"),
             "origin": "team_upload" if s.get("candidate", {}).get("user_provided") else s.get("candidate", {}).get("source"),
             "description": str(s.get("candidate", {}).get("description", ""))[:800], "rows": s.get("rows"),
             "columns": [{"name": c.get("name"), "dtype": c.get("dtype")} for c in s.get("columns", [])[:60]],
             "sample": (s.get("sample") or [])[:3]} for s in sources]


async def research_plan(run):
    corpus = await evidence(run["project_id"])
    if not corpus:
        raise ValueError("Chưa có tài liệu đã đọc làm bằng chứng; không thể tự bịa khoảng trống.")
    available = available_data_context(run["artifacts"])
    data_rule = ("Dữ liệu đã có sẵn cho tiến trình này (file nhóm tải lên hoặc nguồn đã kiểm tra) được liệt kê dưới đây. "
                 "Nếu phù hợp với đề tài, đặt câu hỏi và data_needs sao cho đo được bằng các cột có thật này: "
                 "search_terms và unit mô tả đúng cột, không đổi đề tài và không bịa cột. "
                 "Nếu dữ liệu không phù hợp đề tài thì bỏ qua và nêu rõ trong feasibility_notes.\n"
                 + json.dumps(available, ensure_ascii=False, default=str) + "\n") if available else ""
    brief = await generate_structured(
        VIETNAMESE_WRITING_RULE + "Lập hồ sơ nghiên cứu định lượng bằng dữ liệu thứ cấp. "
        "Đề tài bất biến: " + run["artifacts"]["topic"] + "\n"
        + "Địa bàn người dùng yêu cầu: " + str(run["artifacts"]["settings"].get("region") or "suy ra từ đề tài") + "\n" +
        "Giữ đúng địa bàn và đối tượng. Đề xuất một câu hỏi và khoảng trống thận trọng dựa trên tài liệu; "
        "không tuyên bố đã chứng minh tính mới hay đã tìm thấy dữ liệu. Giả thuyết là dự kiến, không phải kết quả. "
        "Trong gap, question và feasibility_notes chỉ nhắc tài liệu bằng [id] trong bằng chứng, không tự ghi tên tác giả hay năm "
        "(tên tác giả viết sai sẽ lan sang bản thảo); không ghi hệ số hay con số không có trong dữ liệu hoặc tài liệu. "
        "Tách data_needs theo biến kết quả, phơi nhiễm/giải thích, kiểm soát và khóa ghép. "
        "Chọn đúng MỘT outcome chính phù hợp nhất với câu hỏi vì bộ phân tích chỉ hỗ trợ một biến đích; không tạo hai outcome thay thế cùng bắt buộc. "
        "Nếu người dùng nêu nhiều outcome thay thế, chọn một outcome chính và ghi lựa chọn còn lại trong feasibility_notes. "
        "Mỗi join_key phải tương ứng một cột nguyên tử có thật (ví dụ province_code và year là hai key riêng); không gộp nhiều cột thành province_year_key. "
        "Mỗi biến có đơn vị, độ phân giải không gian/thời gian, key tiếng Anh snake_case, "
        "search_terms tiếng Anh ngắn để tìm dữ liệu riêng của biến đó. Không yêu cầu tên dataset giống toàn bộ đề tài. "
        "Phân cấp data_needs: outcome và join_key là required; exposure đầu tiên là phơi nhiễm chính và required; "
        "exposure bổ sung là lựa chọn thay thế (required=false); control mặc định required=false. "
        "Chỉ nâng control thành bắt buộc nếu câu hỏi người dùng nói rõ cần kiểm soát biến đó. "
        "geography_terms chỉ gồm các cách gọi tương đương của địa bàn, không đưa khu vực khác hoặc ngôn ngữ thay địa bàn. "
        "evidence_paper_ids chỉ lấy ID trong bằng chứng bên dưới. Nếu cần dữ liệu bảng/chuỗi thời gian, nêu rõ "
        "trong feasibility_notes; hiện phương pháp tự động chỉ có mô tả, tương quan và OLS liên hệ, không nhận diện nhân quả. "
        "GIỚI HẠN PHÂN TÍCH: chỉ có thống kê mô tả, tương quan Pearson và OLS tuyến tính trên biến SỐ; không có số hạng bình phương, "
        "biến tương tác, so sánh hay kiểm định khác biệt giữa các nhóm, mô hình bảng hay chuỗi thời gian. Vì vậy mỗi giả thuyết phải kiểm định "
        "được bằng dấu/độ lớn của một hệ số OLS hoặc một tương quan giữa biến số: không đặt giả thuyết phi tuyến, giảm dần, tương tác, "
        "điều tiết hay khác biệt giữa nam/nữ, vùng, nhóm. Biến dạng phân loại chữ (giới tính, vùng, nghề) chỉ đưa vào data_needs khi "
        "nguồn có sẵn dạng số 0/1; nếu không thì nêu trong feasibility_notes. Chỉ thêm join_key khi dự kiến phải ghép từ hai nguồn trở lên; "
        "dữ liệu một bảng cấp cá nhân không cần join_key.\n"
        + "Yêu cầu bổ sung của người dùng: " + run["artifacts"].get("feedback", "") + "\n"
        + data_rule
        + json.dumps(corpus, ensure_ascii=False, default=str), ResearchBrief)
    valid = {p["id"] for p in corpus}
    if not set(brief.evidence_paper_ids).issubset(valid):
        raise ValueError("Kế hoạch dẫn tài liệu không thuộc bằng chứng đã đọc.")
    # Tell the user, before approval, whether each variable has any candidate source.
    try:
        run["artifacts"]["data_feasibility"] = await data.probe_feasibility(
            brief, run["artifacts"].get("user_sources", []),
            seconds=float(run["artifacts"].get("settings", {}).get("feasibility_seconds", 75)))
    except Exception as exc:  # The probe is advisory; never block planning on it.
        run["artifacts"]["data_feasibility"] = {"schema_version": "httm/data-feasibility-v1", "verdict": "unknown",
                                                "needs": [], "errors": [{"error": type(exc).__name__}]}
    return brief.model_dump()


def _numeric(dtype):
    return str(dtype).lower().startswith(("int", "uint", "float", "double"))


def validate_alternatives(proposal, collection):
    """Keep only alternatives whose every column exists, is numeric and is joinable."""
    columns = {s["source_key"]: {c["name"]: c.get("dtype") for c in s.get("columns", [])}
               for s in collection.get("sources", [])}
    valid, rejected = [], []
    for item in proposal.alternatives:
        refs = [item.outcome, *item.exposures]
        used = sorted({ref.source_key for ref in refs})
        problem = None
        for ref in refs:
            if ref.source_key not in columns or ref.column not in columns[ref.source_key]:
                problem = f"Cột {ref.source_key}:{ref.column} không có trong snapshot."
            elif not _numeric(columns[ref.source_key][ref.column]):
                problem = f"Cột {ref.source_key}:{ref.column} không phải biến số."
            if problem:
                break
        if not problem and len(used) > 2:
            problem = "Dùng quá hai nguồn."
        if not problem and len(used) == 2 and (not item.join_columns or any(
                col not in columns[key] for key in used for col in item.join_columns)):
            problem = "Hai nguồn không có cột khóa ghép chung đã khai báo."
        if problem:
            rejected.append({"question": item.question, "reason": problem})
        else:
            valid.append(item.model_dump() | {"source_keys": used})
    return valid, rejected


async def suggest_alternatives(run, brief, collection, blockers):
    """Data-grounded questions on the same topic, when the approved one has no usable data."""
    papers = [p for p in await evidence(run["project_id"]) if p["id"] in set(brief.evidence_paper_ids)]
    proposal = await generate_structured(
        VIETNAMESE_WRITING_RULE +
        "Câu hỏi nghiên cứu đã duyệt chưa có dữ liệu phù hợp. Đề xuất tối đa 3 câu hỏi THAY THẾ cùng lĩnh vực với đề tài, "
        "mỗi câu trả lời được bằng các snapshot dữ liệu THỰC TẾ bên dưới ở mức liên hệ quan sát (mô tả, tương quan, OLS). "
        "outcome và exposures phải trỏ đúng source_key và tên cột có thật, là cột số đo đúng khái niệm; không dùng cột mã/ID. "
        "Nếu dùng hai nguồn thì join_columns phải là các cột có trong cả hai nguồn. "
        "scope_change nói thẳng điểm khác so với câu hỏi gốc (biến, địa bàn, đơn vị phân tích, thời gian). "
        "Không đề xuất câu hỏi nhân quả, không bịa cột hay nguồn. Nếu không có câu hỏi hợp lý, trả danh sách rỗng.\n"
        + json.dumps({"topic": run["artifacts"]["topic"], "approved_question": brief.question,
                      "blockers": blockers, "literature": [{k: p.get(k) for k in ("id", "title", "key_finding")} for p in papers][:20]},
                     ensure_ascii=False, default=str) + "\n" + data.design_context(collection), AlternativeQuestions)
    valid, rejected = validate_alternatives(proposal, collection)
    return {"alternatives": valid, "rejected": rejected,
            "note": "Câu hỏi thay thế dựa trên cột có thật trong dữ liệu đã tải; chọn một câu sẽ lập lại kế hoạch "
                    "và giữ nguyên các nguồn đã tải. Thiết kế và phân tích vẫn được kiểm tra lại đầy đủ."}


def align_units(proposed, brief):
    """Use the approved unit label for bindings that declare no conversion.

    The planner writes units in Vietnamese ("USD/giờ", "năm") while the design
    LLM may write the same unit in English ("USD/hour", "years"). A binding with
    scale 1 and offset 0 asserts the column is already in the approved unit, so
    its labels are set to that unit. Bindings that declare a real conversion
    keep their labels and remain subject to the strict unit checks.
    """
    units = {need.key: need.unit for need in brief.data_needs}
    value = proposed.model_dump()
    changed = []
    for selection in value["sources"]:
        for binding in selection["bindings"]:
            unit = units.get(binding["variable"])
            if unit is None or binding["target_unit"] == unit:
                continue
            if binding["scale"] == 1 and binding["offset"] == 0:
                changed.append(f"{binding['variable']}: '{binding['target_unit']}' → '{unit}'")
                binding["target_unit"] = binding["source_unit"] = unit
    if not changed:
        return proposed
    value["assumptions"] = list(value.get("assumptions", [])) + [
        "Cột được dùng đúng đơn vị đã duyệt, không đổi đơn vị (chuẩn hóa nhãn: " + "; ".join(changed) + ")."]
    return AnalysisDesign.model_validate(value)


def _words(text):
    import re
    import unicodedata
    text = unicodedata.normalize("NFC", str(text or "")).casefold()
    return {w for w in re.findall(r"\w+", text) if len(w) > 1 or w.isdigit()}


def repair_binding_variables(proposed, brief):
    """Map bindings that name a column or concept instead of a plan key.

    The design LLM sometimes writes the column label ("Lương giờ (USD)") in
    ``variable`` instead of the plan key ("hourly_wage"). Exact key, concept
    and column matches are repaired first; otherwise the need whose concept
    shares the most words with the column (and the same unit) is used, but
    only when that choice is unique, so an unclear binding stays invalid.
    """
    needs = list(brief.data_needs)
    keys = {need.key for need in needs}
    value = proposed.model_dump()
    taken = {b["variable"] for s in value["sources"] for b in s["bindings"] if b["variable"] in keys}
    changed = []
    for selection in value["sources"]:
        for binding in selection["bindings"]:
            if binding["variable"] in keys:
                continue
            label = binding["variable"].casefold().strip()
            match = [n.key for n in needs if label in (n.key.casefold(), n.concept.casefold().strip())]
            if len(match) != 1:
                words = _words(binding["variable"]) | _words(binding["column"])
                scored = []
                for need in needs:
                    score = len(words & _words(need.concept))
                    if score and _words(need.unit) and _words(need.unit) <= (_words(binding.get("target_unit")) | words):
                        score += 1
                    scored.append((score, need.key))
                scored.sort(reverse=True)
                best = [key for score, key in scored if scored and score == scored[0][0] and score > 0]
                # If the closest concept is already bound, this binding duplicates it:
                # never fall back to the next-closest (e.g. GDP → CO2 per capita).
                match = best if len(best) == 1 and best[0] not in taken else []
            if len(match) == 1 and match[0] not in taken:
                changed.append(f"{binding['column']} → {match[0]}")
                binding["variable"] = match[0]
                taken.add(match[0])
    if not changed:
        return proposed
    rename = {}
    for selection in value["sources"]:
        for binding in selection["bindings"]:
            rename[binding["column"]] = binding["variable"]
    for old, new in [item.split(" → ") for item in changed]:
        rename[old] = new
    role = {need.key: need.role for need in needs}
    value["predictors"] = list(dict.fromkeys(
        rename.get(p, p) for p in value.get("predictors", []) if rename.get(p, p) in keys)) or list(dict.fromkeys(
        b["variable"] for s in value["sources"] for b in s["bindings"]
        if role.get(b["variable"]) not in (None, "outcome", "join_key")))
    if value.get("target") not in keys:
        value["target"] = rename.get(value.get("target"), value.get("target"))
    value["assumptions"] = list(value.get("assumptions", [])) + [
        "Ánh xạ cột sang biến kế hoạch được chuẩn hóa: " + "; ".join(changed) + "."]
    return AnalysisDesign.model_validate(value)


def drop_row_identifier_filters(proposed, collection):
    """Remove sample filters on row-number/ID columns (every value unique).

    Filtering "rownames between 1 and 534" looks harmless but silently drops
    rows whose index is outside the range, so the paper reports 533 rows while
    claiming the whole file was kept. Geography/time filters never need an ID.
    """
    profiles = {s["source_key"]: s for s in collection.get("sources", [])}
    value = proposed.model_dump()
    dropped = []
    for selection in value["sources"]:
        source = profiles.get(selection["source_key"]) or {}
        rows = source.get("rows")
        unique = {c["name"]: c.get("unique") for c in source.get("columns", [])}
        keep = []
        for rule in selection.get("filters", []):
            if rows and unique.get(rule["column"]) == rows:
                dropped.append(f"{selection['source_key']}:{rule['column']}")
            else:
                keep.append(rule)
        selection["filters"] = keep
    if not dropped:
        return proposed
    value["assumptions"] = list(value.get("assumptions", [])) + [
        "Không lọc mẫu theo cột số thứ tự/định danh dòng (" + ", ".join(dropped) + "); giữ mọi dòng của nguồn."]
    return AnalysisDesign.model_validate(value)


async def design(run):
    approved(run, "research_plan")
    collection = run["artifacts"]["data_collection"]
    brief = ResearchBrief.model_validate(run["artifacts"]["research_plan"])
    if not collection["sources"]:
        proposed = AnalysisDesign(adequate=False, rationale="Chưa có nguồn chứa dữ liệu bảng có thể kiểm tra.",
            missing_requirements=["Cần dữ liệu đo biến theo câu hỏi và địa bàn đã duyệt."], method="unsupported")
    else:
        proposed = await generate_structured(
            VIETNAMESE_WRITING_RULE + "Đề xuất thiết kế phân tích từ dữ liệu THỰC TẾ dưới đây. "
            "Không bịa tên cột, nguồn, đơn vị hay độ bao phủ. adequate=true chỉ khi đủ biến bắt buộc, đúng địa bàn, "
            "Chỉ các biến outcome, join_key và phơi nhiễm chính mới chặn tính khả thi; biến kiểm soát và phơi nhiễm bổ sung là tùy chọn. "
            "Nếu thiếu biến tùy chọn, hãy đưa vào limitations và vẫn đề xuất mô hình quan sát phù hợp khi outcome và phơi nhiễm chính có dữ liệu. "
            "đúng cách đo, cùng thời gian và độ phân giải, có thể trả lời câu hỏi ở mức liên hệ quan sát. "
            "Metadata có nhắc địa bàn chưa đủ chứng minh bản ghi thuộc địa bàn. "
            "Nguồn có user_provided=true là file do nhóm nghiên cứu tải lên: user_description là khai báo của người dùng về "
            "phạm vi, cách thu thập và đơn vị, cần đối chiếu với tên cột và giá trị mẫu chứ không tự coi là đã kiểm chứng. "
            "Cột trong ambiguous_numeric_columns không được dùng transform numeric. Cột trong code_like_columns là mã, "
            "dùng strip/casefold khi làm khóa ghép, không coi là đại lượng. Nếu dữ liệu giả lập, "
            "instruction tuning hoặc ảnh không có biến định lượng phù hợp thì adequate=false. "
            "Đọc ý nghĩa và giá trị mẫu để phân biệt mã số với đại lượng. "
            "Chọn tối đa 3 source_key có thật, ánh xạ bindings từ column sang key data_needs: trường variable PHẢI là "
            "đúng một key trong data_needs (ví dụ hourly_wage), không phải tên cột hay tên khái niệm; target và predictors cũng dùng key. "
            "Nếu nguồn chứa nhiều địa bàn hoặc thời kỳ, khai báo filters trước ánh xạ bằng cột gốc, "
            "operator eq/in/between và values chính xác có trong nguồn, kèm rationale. Không đoán mã quốc gia. "
            "Không có cách lọc đúng địa bàn/thời kỳ thì adequate=false. "
            "Khai báo source_unit và target_unit trùng đơn vị trong kế hoạch; mọi đổi đơn vị phải có lý do, scale/offset rõ. "
            "Chỉ dùng transform identity/numeric/year/strip/casefold. year chỉ cho cột ngày tháng, "
            "cột năm dạng số dùng numeric. Không ghép và không lọc mẫu theo số thứ tự dòng hay cột định danh (rownames, id, stt). "
            "joins ghép lần lượt vào nguồn đầu tiên bằng các key chung. Chỉ one_to_one hoặc many_to_one, "
            "không ghép nhiều-nhiều, không lặp biến ngoài khóa ghép. "
            "method chỉ descriptive/correlation/ols; nếu câu hỏi bắt buộc mô hình panel, time-series, spatial "
            "hoặc thiết kế nhân quả mà OLS không phù hợp, chọn unsupported và ghi missing_requirements. "
            "predictors chỉ gồm biến đã ánh xạ tới cột SỐ; biến dạng chữ (giới tính, ngành, nghề…) ghi vào limitations, không đưa vào predictors. "
            "Nếu chỉ dùng một nguồn thì không cần joins và không cần ánh xạ khóa ghép. "
            "Không coi các giả định là đã kiểm chứng. causal_claims_allowed=false.\n"
            + json.dumps(brief.model_dump(), ensure_ascii=False) + "\n"
            + "Phản hồi người dùng: " + run["artifacts"].get("feedback", "") + "\n"
            + data.design_context(collection), AnalysisDesign)
    proposed = align_units(repair_binding_variables(proposed, brief), brief)
    proposed = drop_row_identifier_filters(proposed, collection)
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
    single_source = len({selection.source_key for selection in proposed.sources}) <= 1
    uncovered = [row for row in coverage.values() if row["required"] and not row["sources"]
                 and not (single_source and row["role"] == "join_key")]
    validation = {"ok": False, "errors": []}
    if uncovered:
        validation["errors"].append("Snapshot chưa phủ đủ biến bắt buộc ở đúng độ phân giải.")
    if invalid_bindings:
        validation["errors"].append("Có ánh xạ trỏ tới biến, nguồn hoặc cột không hợp lệ.")
    if not uncovered and not invalid_bindings and proposed.adequate and proposed.method != "unsupported":
        try:
            _, validation = await asyncio.to_thread(data.prepare_data, run["id"],
                run["artifacts"]["research_plan"], candidate, collection,
                run["artifacts"]["settings"]["max_rows"], True)
            if validation.get("dropped_predictors"):
                # Freeze the reduced model into the design the user approves, then
                # re-validate it strictly so analysis runs exactly what was approved.
                candidate["predictors"] = validation["predictors"]
                candidate["limitations"] = list(candidate.get("limitations", [])) + [
                    "Không đưa vào mô hình: " + item["reason"] + " Biến này chỉ được bàn luận như hạn chế."
                    for item in validation["dropped_predictors"]]
                dropped = validation["dropped_predictors"]
                _, validation = await asyncio.to_thread(data.prepare_data, run["id"],
                    run["artifacts"]["research_plan"], candidate, collection,
                    run["artifacts"]["settings"]["max_rows"])
                validation["dropped_predictors"] = dropped
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
            rationale=("Chưa thể chạy phân tích cho câu hỏi đã duyệt. Hệ thống chỉ tính biến bắt buộc là biến kết quả, khóa ghép và phơi nhiễm chính; biến kiểm soát được báo riêng để không chặn phương án. "
                       + ("Chưa tìm thấy biến kết quả trong các snapshot đã tải. "
                          if not any(row["role"] == "outcome" and row["sources"] for row in coverage.values()) else "")
                       + "Các nguồn ứng viên và lỗi tìm/tải được ghi bên dưới để phân biệt thiếu dữ liệu với lỗi bộ tìm kiếm."),
            missing_requirements=list(dict.fromkeys(missing)), method="unsupported",
            limitations=proposed.limitations, assumptions=proposed.assumptions)
        validation.update({"ok": False, "coverage": list(coverage.values()),
                           "invalid_bindings": invalid_bindings,
                           "candidate_mappings": candidate.get("sources", []),
                           "collection_diagnostics": {
                               "source_count": len(collection.get("sources", [])),
                               "search_errors": collection.get("errors", [])[:20],
                               "time_limit_reached": collection.get("time_limit_reached", False),
                               "search_coverage": collection.get("search_coverage", []),
                               "rejected_previous_sources": collection.get("rejected_previous_sources", [])[:20],
                           },
                           "next_actions": ["Tìm lại nguồn cho các biến còn thiếu; nguồn đã kiểm tra sẽ được loại khỏi lượt tìm lại.",
                                            "Đề xuất phương pháp phù hợp với dữ liệu hiện có.",
                                            "Điều chỉnh câu hỏi hoặc yêu cầu độ phân giải nếu bạn chấp thuận thay đổi phạm vi."]})
        result = safe.model_dump()
        try:
            await create_proposal_draft(run, result["missing_requirements"], list(coverage.values()))
        except Exception as exc:
            run["artifacts"]["proposal_draft_error"] = type(exc).__name__
        if collection.get("sources"):
            try:
                run["artifacts"]["alternative_questions"] = await suggest_alternatives(
                    run, brief, collection, result["missing_requirements"])
            except Exception as exc:
                run["artifacts"]["alternative_questions"] = {"alternatives": [], "rejected": [],
                                                            "error": type(exc).__name__}
    else:
        run["artifacts"].pop("alternative_questions", None)
        validation["coverage"] = list(coverage.values())
        result = candidate
    run["artifacts"]["design_validation"] = validation
    return result


def source_methods_text(provenance):
    """Describe each approved source, including user uploads and their cleaning log."""
    text = ""
    for source in provenance:
        if source.get("origin") == "user_upload":
            text += (f"Nguồn {source['source_key']} là dữ liệu do nhóm nghiên cứu cung cấp. "
                     + (source.get("cleaning_summary") or "") + " "
                     + f"Snapshot sau làm sạch có SHA-256 {source['snapshot_sha256'][:12]}… với "
                     f"{source['rows_before_filter']} dòng; {source['rows']} dòng đáp ứng điều kiện chọn mẫu. ")
        else:
            text += (f"Nguồn {source['dataset_id']} ({source['dataset_url']}) cung cấp "
                     f"{source['rows_before_filter']} dòng snapshot; {source['rows']} dòng đáp ứng điều kiện chọn mẫu. ")
        for rule in source["filters"]:
            text += rule["rationale"] + ". "
    return text


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
    origins = {source.get("origin", "public_repository") for source in provenance}
    if "user_upload" in origins:
        metadata["data_origin"] = "user_upload" if origins == {"user_upload"} else "mixed"
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
    code = [Path(__file__), Path(data.__file__), Path(__file__).parent / "uploads.py",
            Path(__file__).parents[1] / "data_research" / "service.py"]
    result["reproducibility"] = {"python": platform.python_version(),
        "packages": {name: importlib.metadata.version(name) for name in ("pandas", "numpy", "scipy", "pyarrow")},
        "code_sha256": {str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(p.read_bytes()).hexdigest() for p in code},
        "research_plan_sha256": fingerprint(a["research_plan"]), "design_sha256": fingerprint(a["design"])}
    result["methods_draft"] += " Phương án chuẩn hóa và kết hợp nguồn đã được người dùng duyệt. "
    result["methods_draft"] += source_methods_text(provenance)
    if any(source.get("origin") == "user_upload" for source in provenance):
        result.setdefault("limitations", []).append(
            "Có nguồn do nhóm nghiên cứu tự cung cấp: hệ thống chỉ kiểm tra cấu trúc, giá trị và tính toàn vẹn của file; "
            "quy trình thu thập, tính đại diện của mẫu và quyền sử dụng dữ liệu cần được nhóm mô tả và chịu trách nhiệm.")
    for join in report["joins"]:
        result["methods_draft"] += ("Dữ liệu được ghép trái theo " + ", ".join(join["keys"]) +
            f"; {join['matched_rows']} dòng khớp và {join['unmatched_rows']} dòng không khớp. ")
    result["methods_draft"] += "Các phép phân tích chỉ đánh giá mối liên hệ quan sát, không xác định tác động nhân quả."
    papers = await evidence(run["project_id"])
    brief = ResearchBrief.model_validate(a["research_plan"])
    paper_ids = set(brief.evidence_paper_ids)
    literature = [{key: paper.get(key) for key in ("id", "title", "year", "doi", "method", "key_finding", "limitation", "abstract")}
                  for paper in papers if paper["id"] in paper_ids][:30]
    analysis_evidence = {key: result.get(key) for key in (
        "methods_draft", "results_draft", "dataset", "overview", "ols_regression",
        "correlation_pearson", "limitations", "analysis_plan") if result.get(key) is not None}
    story = await generate_structured(
        VIETNAMESE_WRITING_RULE +
        "Define the paper story before any manuscript section is drafted. Use the approved question, verified literature, and completed analysis. "
        "State thesis, gap, contribution boundary, supported claims, and claims to avoid. Do not infer causality or novelty.\n" +
        json.dumps({"topic": a["topic"], "plan": brief.model_dump(), "literature": literature,
                    "analysis_evidence": analysis_evidence}, ensure_ascii=False, default=str), PaperStory)
    run["artifacts"]["paper_story"] = dumped(story)
    run["artifacts"]["paper_state"] = {"schema_version": "httm/paper-state-v1", "mode": "empirical",
        "stage": "writing", "required_artifacts": ["paper_story", "claim_evidence_map", "analysis"],
        "blockers": ["Bản thảo, claim-evidence map và kiểm chứng đang chờ hoàn tất."],
        "build": {"status": "not_run", "format": "Markdown/PDF"}}
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
    brief = ResearchBrief.model_validate(a["research_plan"])
    papers = [paper for paper in await evidence(run["project_id"])
              if paper["id"] in set(brief.evidence_paper_ids)]
    paper_story = a.get("paper_story")
    if not paper_story:
        paper_story = await generate_structured(VIETNAMESE_WRITING_RULE +
        "Before final framing, define the paper story from the approved question, verified literature, and completed analysis. "
        "State the thesis, literature gap, contribution boundary, claims supported by this run, and claims to avoid. "
        "Do not infer causality or novelty. Treat numeric analysis as the only source of empirical claims.\n" +
        json.dumps({"topic": a["topic"], "plan": brief.model_dump(), "literature": papers,
                    "analysis": a["analysis"], "limitations": a["analysis"].get("limitations", [])},
                   ensure_ascii=False, default=str), PaperStory)
    front = await generate_structured(VIETNAMESE_WRITING_RULE +
        "Viết title, abstract, keywords cho bản thảo dưới đây. Không tạo tác giả. "
        "Abstract chỉ dùng số liệu từ kết quả thực tế, không suy luận nhân quả, không thêm số từ literature. "
        "Không tự nhận là nghiên cứu đầu tiên. Nêu giới hạn quan trọng và phạm vi mẫu.\n" +
        json.dumps({"topic": a["topic"], "brief": brief.model_dump(), "paper_story": dumped(paper_story),
                    "results": a["analysis"]["results_draft"], "discussion": a["discussion"],
                    "feedback": a.get("feedback", "")}, ensure_ascii=False, default=str), FrontMatter)
    intro = a["introduction"]["introduction"]
    work = a["related_work"]["related_work"]
    disc = a["discussion"]["discussion"]
    refs = papers
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
    allowed_papers = {paper["id"] for paper in refs}
    allowed_sources = {selection["source_key"] for selection in a["design"].get("sources", [])}
    map_papers = [{key: paper.get(key) for key in ("id", "title", "year", "doi", "method", "key_finding", "limitation")}
                  for paper in refs if paper["id"] in allowed_papers]
    map_analysis = {key: a["analysis"].get(key) for key in (
        "methods_draft", "results_draft", "ols_regression", "correlation_pearson", "limitations")
                    if a["analysis"].get(key) is not None}
    claim_map = await generate_structured(
        VIETNAMESE_WRITING_RULE +
        "Map the manuscript's decision-relevant factual claims to evidence. Include key claims, not every sentence. "
        "supported_literature requires paper IDs from the supplied read-paper corpus; supported_data requires source keys from the approved design; "
        "proposed is for planned work and unsupported is for claims without supplied evidence. Do not invent IDs or strengthen claims.\n" +
        json.dumps({"paper_story": dumped(paper_story), "sections": sections, "papers": map_papers,
                    "analysis_evidence": map_analysis, "approved_source_keys": sorted(allowed_sources)},
                   ensure_ascii=False, default=str), ClaimEvidenceMap)
    claims = dumped(claim_map)
    from app.routers.discussion import _find_unverified_numbers
    analysis_json = json.dumps(a["analysis"], ensure_ascii=False, default=str)

    def analysis_numbers_match(text):
        return not _find_unverified_numbers([text], analysis_json)

    for claim in claims["claims"]:
        bad_ids = set(claim.get("evidence_paper_ids", [])) - allowed_papers
        bad_keys = set(claim.get("source_keys", [])) - allowed_sources
        claim["evidence_paper_ids"] = sorted(set(claim.get("evidence_paper_ids", [])) & allowed_papers)
        claim["source_keys"] = sorted(set(claim.get("source_keys", [])) & allowed_sources)
        if (claim["status"] == "supported_data" and not claim["source_keys"] and allowed_sources
                and not bad_keys and analysis_numbers_match(claim.get("claim", ""))):
            # The writer often omits the key for a figure taken from the analysis
            # itself; the figure matching the analysis is the verified evidence.
            claim["source_keys"] = sorted(allowed_sources)
        if bad_ids or bad_keys or (claim["status"] == "supported_literature" and not claim["evidence_paper_ids"]) or (
                claim["status"] == "supported_data" and not claim["source_keys"]):
            claim["status"] = "unsupported"
            claim["rationale"] += " Hệ thống không xác nhận được nguồn bằng chứng đã khai báo."
    cited.update(paper_id for claim in claims["claims"] for paper_id in claim.get("evidence_paper_ids", []))
    references = [dict(row) for row in rows if row["id"] in cited]
    for ref in references:
        if isinstance(ref["authors"], str):
            ref["authors"] = json.loads(ref["authors"])
    a["paper_story"] = dumped(paper_story)
    a["claim_evidence_map"] = claims
    a["paper_state"] = {"schema_version": "httm/paper-state-v1", "mode": "empirical",
        "stage": "verification_pending", "required_artifacts": ["paper_story", "claim_evidence_map", "analysis"],
        "blockers": ["Đang chờ kiểm chứng trích dẫn, con số và nội dung khoa học."],
        "build": {"status": "not_run", "format": "Markdown/PDF"}}
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
    for claim in a.get("claim_evidence_map", {}).get("claims", []):
        if claim.get("status") == "unsupported":
            issues.append({"section": claim.get("section", "Unknown"), "claim": claim.get("claim", ""),
                           "reason": "Claim-evidence map chưa gắn được bằng chứng đã xác minh.", "severity": "error"})
    paper_state = a.get("paper_state")
    if paper_state:
        blockers = [issue["reason"] for issue in issues if issue["severity"] == "error"]
        paper_state["stage"] = "verification_passed_pending_author" if not blockers else "blocked"
        paper_state["blockers"] = list(dict.fromkeys(blockers))
    return {"ok": not any(i["severity"] == "error" for i in issues), "issues": issues,
        "citation_reports": citations, "source_checks": source_report,
        "limitations": audit.limitations + ["Kiểm chứng ngữ nghĩa dùng LLM; cần người nghiên cứu xem lại nguồn gốc."]}


async def execute_stage(run):
    from app.routers import search, analysis, synthesis, writer, related_work, discussion
    from app.models import WriteDiscussionRequest
    stage, pid = run["stage"], run["project_id"]
    from .state import is_review
    if is_review(run) and stage in ("research_plan", "manuscript", "verification"):
        from . import literature
        return await {"research_plan": literature.plan, "manuscript": literature.manuscript,
                      "verification": literature.verify}[stage](run)
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
