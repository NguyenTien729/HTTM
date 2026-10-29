"""Install the Data Research module into a local clone of NguyenTien729/HTTM.

Usage: extract this patch ZIP into the root of the HTTM repository, then run:
    python apply_patch.py

The script is intentionally conservative: it stops rather than guessing if it
cannot find a top-level `app = FastAPI(...)` assignment in app/main.py.
"""

from __future__ import annotations

import ast
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULE_SOURCE = ROOT / "app" / "data_research"
MAIN_FILE = ROOT / "app" / "main.py"
REQUIREMENTS_FILE = ROOT / "requirements.txt"


def find_fastapi_app_assignment(source: str) -> ast.Assign:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise RuntimeError(f"Không đọc được app/main.py do lỗi cú pháp: {exc}") from exc

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "app" for target in node.targets):
            continue
        value = node.value
        if isinstance(value, ast.Call):
            func = value.func
            if (isinstance(func, ast.Name) and func.id == "FastAPI") or (
                isinstance(func, ast.Attribute) and func.attr == "FastAPI"
            ):
                return node
    raise RuntimeError(
        "Không tìm thấy dòng app = FastAPI(...) ở cấp module trong app/main.py. "
        "Module đã được chép vào app/data_research nhưng chưa tự gắn router; hãy xem README_DATA_RESEARCH.md để tích hợp thủ công."
    )


def install_router() -> None:
    if not MODULE_SOURCE.exists():
        raise RuntimeError("Không tìm thấy app/data_research. Hãy giải nén ZIP vào thư mục gốc repo HTTM.")
    if not MAIN_FILE.exists():
        raise RuntimeError("Không tìm thấy app/main.py. Hãy chạy script từ thư mục gốc repo HTTM.")

    source = MAIN_FILE.read_text(encoding="utf-8")
    import_line = "from app.data_research.router import router as data_research_router"
    include_line = "app.include_router(data_research_router)"

    if import_line in source and include_line in source:
        print("Router Data Research đã được gắn vào app/main.py; bỏ qua bước này.")
        return

    assignment = find_fastapi_app_assignment(source)
    lines = source.splitlines(keepends=True)
    # Insert after the FastAPI constructor, not inside a multiline argument list.
    include_index = assignment.end_lineno
    if include_line not in source:
        lines.insert(include_index, f"\n{include_line}\n")
    # Insert import immediately before `app = FastAPI(...)`. Use the original AST line
    # number, so do this after the router insertion to avoid line-offset mistakes.
    updated = "".join(lines)
    if import_line not in updated:
        try:
            updated_tree = ast.parse(updated)
        except SyntaxError as exc:
            raise RuntimeError(f"Tích hợp router làm phát sinh lỗi cú pháp: {exc}") from exc
        updated_assignment = find_fastapi_app_assignment(updated)
        updated_lines = updated.splitlines(keepends=True)
        updated_lines.insert(updated_assignment.lineno - 1, import_line + "\n")
        updated = "".join(updated_lines)
    ast.parse(updated)
    MAIN_FILE.write_text(updated, encoding="utf-8")
    print("Đã gắn Data Research router vào app/main.py.")


def install_requirements() -> None:
    if not REQUIREMENTS_FILE.exists():
        print("Không tìm thấy requirements.txt; hãy cài các package trong requirements-data-research.txt thủ công.")
        return
    current = REQUIREMENTS_FILE.read_text(encoding="utf-8")
    package_names = set()
    for line in current.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or entry.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[]", entry, maxsplit=1)[0].strip().lower().replace("_", "-")
        package_names.add(name)

    additions = []
    for line in (ROOT / "requirements-data-research.txt").read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        name = re.split(r"[<>=!~\[]", entry, maxsplit=1)[0].strip().lower().replace("_", "-")
        if name not in package_names:
            additions.append(entry)
            package_names.add(name)
    if additions:
        current = current.rstrip() + "\n\n# Data Research module\n" + "\n".join(additions) + "\n"
        REQUIREMENTS_FILE.write_text(current, encoding="utf-8")
        print("Đã thêm dependency vào requirements.txt: " + ", ".join(additions))
    else:
        print("Các dependency Data Research đã có trong requirements.txt.")


def main() -> None:
    install_router()
    install_requirements()
    print("Hoàn tất. Chạy: pip install -r requirements.txt")
    print("Sau đó: uvicorn app.main:app --reload --port 8000")
    print("Swagger: http://127.0.0.1:8000/docs (tag Data Research)")


if __name__ == "__main__":
    main()
