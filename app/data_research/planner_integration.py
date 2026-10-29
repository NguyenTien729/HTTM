"""Resolve a project's research topic from the existing Research Planner.

The integration first honors an explicit hook (DATA_RESEARCH_TOPIC_RESOLVER),
then tries common SQLAlchemy project-model/session layouts. This keeps the patch
usable with the host app without hard-coding one database module name.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect as pyinspect
import json
import os
from typing import Any

from .service import DataResearchError

# Prefer fields that are explicitly about a research topic over generic names.
_SPECIFIC_TOPIC_FIELDS = (
    "research_topic",
    "topic",
    "project_topic",
    "topic_title",
    "research_question",
    "research_problem",
    "problem_statement",
    "objective",
    "research_objective",
    "goal",
    "abstract",
)
_GENERIC_TOPIC_FIELDS = ("description", "summary", "title", "name", "project_name")
_TOPIC_FIELDS = _SPECIFIC_TOPIC_FIELDS + _GENERIC_TOPIC_FIELDS
_TITLE_FIELDS = ("title", "name", "project_name")
_MODULE_NAMES = (
    "app.models",
    "app.models.project",
    "app.models.projects",
    "app.models.project_model",
    "app.models.project_models",
    "app.database",
    "app.db",
    "app.core.database",
    "app.core.db",
    "app.project.models",
    "app.projects.models",
    "app.entities.project",
    "app.services.project_service",
    "app.repositories.project_repository",
    # main is last because importing it during application startup can be circular.
    "app.main",
)


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in _TOPIC_FIELDS:
            nested = _as_text(value.get(key))
            if nested:
                return nested
        return None
    # Do not turn arbitrary ORM objects/lists into their repr strings.
    return None


def _object_value(obj: Any, field: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(field)
    try:
        return getattr(obj, field, None)
    except Exception:
        return None


def _extract_topic(record: Any) -> tuple[str | None, str | None, str | None]:
    """Return (topic, source_field, display_title) from a project object."""
    title = None
    for field in _TITLE_FIELDS:
        title = _as_text(_object_value(record, field))
        if title:
            break

    for field in _SPECIFIC_TOPIC_FIELDS:
        topic = _as_text(_object_value(record, field))
        if topic:
            return topic, field, title

    # Planner output can sometimes be stored as a JSON string or nested object.
    # Check nested output before falling back to a generic project title.
    for container_field in ("planner_output", "research_plan", "metadata", "planning_data", "details"):
        value = _object_value(record, container_field)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                continue
        if isinstance(value, dict):
            for field in _TOPIC_FIELDS:
                topic = _as_text(value.get(field))
                if topic:
                    return topic, f"{container_field}.{field}", title

    for field in _GENERIC_TOPIC_FIELDS:
        topic = _as_text(_object_value(record, field))
        if topic:
            return topic, field, title
    return None, None, title


def _module_names() -> list[str]:
    configured = [x.strip() for x in os.getenv("DATA_RESEARCH_MODELS_MODULES", "").split(",") if x.strip()]
    return list(dict.fromkeys(configured + list(_MODULE_NAMES)))


def _import_modules() -> list[Any]:
    modules: list[Any] = []
    for module_name in _module_names():
        try:
            modules.append(importlib.import_module(module_name))
        except Exception:
            # A missing optional module or an import side-effect should not stop
            # discovery; we return a precise setup hint if no adapter is found.
            continue
    return modules


def _custom_resolver_path() -> tuple[str, str] | None:
    raw = os.getenv("DATA_RESEARCH_TOPIC_RESOLVER", "").strip()
    if not raw:
        return None
    if ":" in raw:
        module_name, function_name = raw.split(":", 1)
    else:
        module_name, _, function_name = raw.rpartition(".")
    if not module_name or not function_name:
        raise DataResearchError(
            "DATA_RESEARCH_TOPIC_RESOLVER phải có dạng 'module:function', ví dụ 'app.services.project_service:get_topic'.",
            500,
        )
    return module_name, function_name


def _resolve_with_custom_hook(project_id: str) -> Any:
    path = _custom_resolver_path()
    if not path:
        return None
    module_name, function_name = path
    try:
        module = importlib.import_module(module_name)
        function = getattr(module, function_name)
    except (ImportError, AttributeError) as exc:
        raise DataResearchError(
            f"Không import được hàm planner resolver '{module_name}:{function_name}'. Hãy kiểm tra cấu hình DATA_RESEARCH_TOPIC_RESOLVER.",
            500,
        ) from exc
    if not callable(function):
        raise DataResearchError("DATA_RESEARCH_TOPIC_RESOLVER không trỏ tới một hàm có thể gọi.", 500)
    return function(project_id)


def _candidate_models(modules: list[Any]) -> list[type]:
    found: list[type] = []
    seen: set[type] = set()
    for module in modules:
        for value in vars(module).values():
            if not isinstance(value, type) or value in seen:
                continue
            model_name = value.__name__.lower()
            table_name = str(getattr(getattr(value, "__table__", None), "name", "")).lower()
            if "project" not in model_name and "project" not in table_name:
                continue
            # Keep only mapped SQLAlchemy classes.
            try:
                from sqlalchemy import inspect as sa_inspect
                sa_inspect(value)
            except Exception:
                continue
            found.append(value)
            seen.add(value)
    # Prefer a conventional Project model, then more specific project classes.
    found.sort(key=lambda cls: (0 if cls.__name__.lower() == "project" else 1, cls.__name__.lower()))
    return found


def _get_sessions_and_engines(modules: list[Any]) -> tuple[list[Any], list[Any]]:
    sessions: list[Any] = []
    engines: list[Any] = []
    seen_sessions: set[int] = set()
    seen_engines: set[int] = set()
    for module in modules:
        for name in ("SessionLocal", "session_local", "SessionMaker", "SessionFactory", "db_session", "get_db", "get_session"):
            provider = getattr(module, name, None)
            if not callable(provider):
                continue
            try:
                result = provider()
                # FastAPI get_db-style generators are also common.
                if hasattr(result, "__next__") and not hasattr(result, "query"):
                    result = next(result)
                if hasattr(result, "query") or hasattr(result, "get"):
                    if id(result) not in seen_sessions:
                        sessions.append(result)
                        seen_sessions.add(id(result))
            except Exception:
                continue
        engine = getattr(module, "engine", None) or getattr(module, "db_engine", None)
        if engine is not None and (hasattr(engine, "connect") or hasattr(engine, "dialect")):
            if id(engine) not in seen_engines:
                engines.append(engine)
                seen_engines.add(id(engine))
    return sessions, engines


def _id_values(project_id: str) -> list[Any]:
    values: list[Any] = [project_id]
    try:
        integer = int(project_id)
        if integer not in values:
            values.append(integer)
    except (TypeError, ValueError):
        pass
    return values


def _record_by_id(session: Any, model: type, project_id: str) -> Any:
    try:
        from sqlalchemy import inspect as sa_inspect
        mapper = sa_inspect(model)
    except Exception:
        return None

    candidate_keys: list[str] = []
    for column in mapper.primary_key:
        candidate_keys.append(column.key)
    for key in ("id", "project_id", "uuid"):
        if key in mapper.attrs and key not in candidate_keys:
            candidate_keys.append(key)

    for value in _id_values(project_id):
        # Session.get is correct for the mapped primary key.
        try:
            record = session.get(model, value)
            if record is not None:
                return record
        except Exception:
            pass
        for key in candidate_keys:
            try:
                field = getattr(model, key)
                record = session.query(model).filter(field == value).first()
                if record is not None:
                    return record
            except Exception:
                continue
    return None


def _try_resolve_via_orm(project_id: str) -> dict[str, str] | None:
    modules = _import_modules()
    models = _candidate_models(modules)
    sessions, engines = _get_sessions_and_engines(modules)

    # If app exposes models but not a session factory, construct a short-lived
    # SQLAlchemy Session from its existing engine.
    if not sessions and engines:
        try:
            from sqlalchemy.orm import Session
            sessions = [Session(bind=engines[0])]
        except Exception:
            sessions = []

    for session in sessions:
        try:
            for model in models:
                record = _record_by_id(session, model, project_id)
                if record is None:
                    continue
                topic, source_field, title = _extract_topic(record)
                if topic:
                    return {
                        "project_id": project_id,
                        "topic": topic,
                        "topic_source": source_field or "project model",
                        "project_title": title or "",
                    }
        finally:
            try:
                session.close()
            except Exception:
                pass
    return None


async def resolve_project_topic(project_id: str | int) -> dict[str, str]:
    """Load the exact topic saved by the existing Research Planner.

    This project uses asyncpg through ``app.db.get_pool`` and stores plans in
    ``research_projects(id, topic, research_plan)``. Do not try to discover an
    unrelated SQLAlchemy model: query the same table used by app/routers/planner.py.
    """
    normalized_id = str(project_id).strip()
    if not normalized_id:
        raise DataResearchError("project_id không được để trống.", 422)
    try:
        numeric_id = int(normalized_id)
    except (TypeError, ValueError) as exc:
        raise DataResearchError("project_id phải là số nguyên hợp lệ của Research Planner.", 422) from exc
    if numeric_id <= 0:
        raise DataResearchError("project_id phải lớn hơn 0.", 422)

    try:
        # Import locally to avoid changing the app's startup/import order.
        from app.db import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, topic, research_plan FROM research_projects WHERE id = $1;",
                numeric_id,
            )
    except DataResearchError:
        raise
    except Exception as exc:
        raise DataResearchError(
            "Không thể đọc research_projects từ PostgreSQL. Hãy kiểm tra database connection và schema.",
            500,
        ) from exc

    if row is None:
        raise DataResearchError(
            f"Không tìm thấy Research Planner project với id={numeric_id}.", 404
        )

    topic = str(row["topic"] or "").strip()
    if len(topic) < 3:
        raise DataResearchError(
            f"Research Planner project id={numeric_id} không có topic hợp lệ.", 422
        )

    project_title = ""
    research_plan = row["research_plan"]
    if isinstance(research_plan, str):
        try:
            research_plan = json.loads(research_plan)
        except (TypeError, ValueError):
            research_plan = {}
    if isinstance(research_plan, dict):
        project_title = str(
            research_plan.get("title") or research_plan.get("project_title") or ""
        ).strip()

    return {
        "project_id": str(row["id"]),
        "topic": topic,
        "topic_source": "research_projects.topic",
        "project_title": project_title,
    }
