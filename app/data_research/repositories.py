"""Public research datasets: DataCite discovery and bounded Mendeley CSV/XLSX reads."""
import asyncio
import io
import re
import zipfile
from itertools import combinations
from urllib.parse import urlparse
import httpx
import pandas as pd

MENDELEY = re.compile(r"^mendeley:([a-z0-9]{10}):(\d+)$")
FILE_ID = re.compile(r"^[a-f0-9-]{36}$")
API = "https://data.mendeley.com/public-api/datasets"
MAX_FILE_BYTES = 32*1024*1024


def parse_id(dataset_id):
    from .service import DataResearchError
    match = MENDELEY.fullmatch(dataset_id)
    if not match:
        raise DataResearchError("ID Mendeley phải có dạng mendeley:dataset_id:version.", 422)
    return match.groups()


async def search_mendeley(client, context, queries, timeout):
    from .service import _get_json, _to_candidate, _candidate_relevance
    groups = context.get("_search_concept_groups", [])
    expressions = []
    if len(groups) >= 2:
        for group in groups[:5]:
            expressions.append("(" + " OR ".join('"' + term.replace('"', '') + '"' for term in group[:6]) + ")")
    else:
        terms = list(dict.fromkeys(term for q in queries[:3] for term in q.split() if len(term) > 2))[:12]
        if not terms:
            return []
        expressions = ["(" + " OR ".join('"' + re.sub(r'[^\w -]', '', t) + '"' for t in terms) + ")"]
    # Retrieval must also find partial matches regardless of LLM group order.
    concepts_query = "(" + " OR ".join(f"({a} AND {b})" for a, b in combinations(expressions, 2)) + ")" if len(expressions) >= 2 else expressions[0]
    base_query = "types.resourceTypeGeneral:Dataset AND " + concepts_query
    scopes = context.get("_search_scope_terms", [])
    search_queries = [base_query]
    if scopes:
        search_queries.insert(0, base_query + " AND (" + " OR ".join('"' + t.replace('"', '') + '"' for t in scopes[:8]) + ")")
    payloads = await asyncio.gather(*[_get_json(client, "https://api.datacite.org/dois", {
        "query": query, "prefix": "10.17632", "page[size]": 100,
    }, request_timeout=timeout) for query in search_queries])
    found = []
    for entry in [entry for payload in payloads for entry in payload.get("data", [])]:
        item = entry.get("attributes", {})
        doi = item.get("doi", "")
        match = re.fullmatch(r"10\.17632/([a-z0-9]{10})\.(\d+)", doi, re.I)
        if not match:
            continue
        code, version = match.groups()
        title = " ".join(t.get("title", "") for t in item.get("titles", []))
        description = " ".join(d.get("description", "") for d in item.get("descriptions", []))
        # Classify against the full title/description, not a synthetic repository ID.
        candidate = _to_candidate({"id": title, "cardData": {"description": description}}, queries)
        if not _candidate_relevance(candidate, context):
            continue
        candidate.update(dataset_id=f"mendeley:{code}:{version}", title=title, doi=doi,
            description=re.sub(r"<[^>]+>", " ", description)[:6000], source="Mendeley Data / DataCite",
            dataset_url=f"https://data.mendeley.com/datasets/{code}/{version}",
            license=next((r.get("rights") or r.get("rightsIdentifier") for r in item.get("rightsList", [])), None),
            last_modified=item.get("updated"))
        found.append(candidate)
    # Prefer latest version of each deposit.
    unique = {}
    for candidate in found:
        _, code, version = candidate["dataset_id"].split(":")
        if code not in unique or int(version) > int(unique[code]["dataset_id"].split(":")[-1]):
            unique[code] = candidate
    return list(unique.values())


async def metadata(client, dataset_id):
    from .service import _get_json, DataResearchError
    code, version = parse_id(dataset_id)
    info = await _get_json(client, f"{API}/{code}/snapshot/{version}", {})
    try:
        response = await client.get(f"{API}/{code}/files", params={"version": version, "folder_id": "root", "$limit": 1000, "$start": 0},
                                    headers={"Accept": "application/vnd.mendeley-public-dataset.1+json"})
        response.raise_for_status()
        files = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DataResearchError("Không lấy được danh sách file từ Mendeley Data.", 502) from exc
    if not isinstance(files, list):
        raise DataResearchError("Kho dữ liệu chưa trả được danh sách file công khai.", 502)
    supported = [f for f in files if str(f.get("filename", "")).lower().endswith((".csv", ".xlsx")) and FILE_ID.fullmatch(str(f.get("id", "")))]
    if not supported:
        raise DataResearchError("Dataset được tìm thấy nhưng chưa có CSV/XLSX công khai ở thư mục gốc để phân tích tự động.", 422)
    supported.sort(key=lambda f: (
        any(word in f["filename"].lower() for word in ("codebook", "dictionary", "summary", "descriptive", "output", "readme")),
        not any(word in f["filename"].lower() for word in ("data", "raw", "analysis_ready")), f["filename"].lower()))
    return info, supported


async def read_file(client, dataset_id, file):
    from .service import DataResearchError
    code, _ = parse_id(dataset_id)
    if int(file.get("size", 0)) > MAX_FILE_BYTES:
        raise DataResearchError("File dữ liệu vượt giới hạn 32 MB.", 413)
    url = f"https://data.mendeley.com/public-files/datasets/{code}/files/{file['id']}/file_downloaded"
    for _ in range(4):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or not (host == "data.mendeley.com" or host.endswith(".amazonaws.com")):
            raise DataResearchError("Địa chỉ tải file không thuộc kho dữ liệu được phép.", 502)
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.is_redirect:
                from urllib.parse import urljoin
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            content, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise DataResearchError("File dữ liệu vượt giới hạn 32 MB.", 413)
                content.append(chunk)
            return b"".join(content)
    raise DataResearchError("Kho dữ liệu chuyển hướng quá nhiều lần.", 502)


def parse_table(raw, filename, split):
    from .service import DataResearchError
    try:
        if filename.lower().endswith(".xlsx"):
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 128*1024*1024 or len(archive.infolist()) > 2000:
                    raise DataResearchError("File Excel quá lớn sau giải nén.", 413)
            return pd.read_excel(io.BytesIO(raw), sheet_name=int(split), nrows=100001, engine="openpyxl")
        return pd.read_csv(io.BytesIO(raw), sep=None, engine="python", nrows=100001)
    except ImportError as exc:
        raise DataResearchError("Thiếu thư viện openpyxl; hãy cập nhật requirements và build lại ứng dụng.", 500) from exc
    except (ValueError, OSError, zipfile.BadZipFile, pd.errors.ParserError) as exc:
        raise DataResearchError("Không đọc được bảng CSV/Excel hoặc trang tính đã chọn.", 422) from exc


async def download(dataset_id, config=None, split="0", max_rows=50000):
    from .service import DataResearchError
    if not 100 <= max_rows <= 100000:
        raise DataResearchError("max_rows phải nằm trong khoảng 100–100000.", 422)
    if not str(split).isdigit():
        raise DataResearchError("Trang tính phải là chỉ số từ 0.", 422)
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        info, files = await metadata(client, dataset_id)
        chosen = next((f for f in files if f["id"] == config), None) if config else files[0]
        if not chosen:
            raise DataResearchError("File được chọn không có trong dataset.", 422)
        try:
            raw = await read_file(client, dataset_id, chosen)
        except httpx.HTTPError as exc:
            raise DataResearchError("Mendeley chưa cho tải được file dữ liệu; hãy thử lại hoặc chọn file khác.", 502) from exc
    frame = await asyncio.to_thread(parse_table, raw, chosen["filename"], split)
    if len(frame) > 100000:
        raise DataResearchError("File có hơn 100.000 dòng; chưa hỗ trợ lấy mẫu toàn bộ file này.", 413)
    count = len(frame)
    if count > max_rows:
        frame = frame.sample(n=max_rows, random_state=42).reset_index(drop=True)
    code, version = parse_id(dataset_id)
    return frame, {"dataset_id": dataset_id, "dataset_title": info.get("name"), "doi": info.get("doi"),
        "config": chosen["id"], "split": str(split), "filename": chosen["filename"],
        "source": "Mendeley Data", "source_url": f"https://data.mendeley.com/datasets/{code}/{version}",
        "rows_analyzed": len(frame), "rows_in_selected_split": count, "sampled": count > max_rows,
        "sampling_method": "Random sampling, seed=42" if count > max_rows else "Toàn bộ bảng được chọn",
        "downloaded_bytes": len(raw), "analysis_mode": "tabular", "license": info.get("licence", {}).get("short_name"),
        "source_description": info.get("description", ""), "available_files": [{"id": f["id"], "filename": f["filename"]} for f in files]}


async def inspect(dataset_id, config=None, split=None):
    from .service import _json_safe
    frame, meta = await download(dataset_id, config, split or "0", max_rows=100000)
    return {"dataset_id": dataset_id, "dataset_url": meta["source_url"], "source": "Mendeley Data",
        "available_splits": [{"config": meta["config"], "split": meta["split"], "num_rows": len(frame), "filename": meta["filename"]}],
        "preview_selection": {"config": meta["config"], "split": meta["split"]}, "num_rows_total": len(frame),
        "columns": [{"name": str(c), "type": str(frame[c].dtype)} for c in frame],
        "preview_rows": _json_safe(frame.head(20).to_dict(orient="records")), "preview_available": True,
        "analysis_ready": not frame.empty, "available_files": meta["available_files"], "requires_user_review": True,
        "review_checklist": ["Kiểm tra biến AI và biến kết quả có đúng khái niệm trong đề tài không.",
            "Cảm nhận về tiến bộ học tập không đồng nghĩa năng suất đo khách quan hoặc tác động nhân quả.",
            "Đối chiếu bảng mã, đối tượng, cách lấy mẫu và giấy phép trước khi chọn."], "description": meta["source_description"]}
