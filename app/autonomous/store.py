"""PostgreSQL queue with leases and version checks; safe across API workers."""
import json

from fastapi import HTTPException

from app.db import get_pool
from .state import check_version, decide, event

SCHEMA = """
CREATE TABLE IF NOT EXISTS autonomous_runs (
    id BIGSERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'literature_search',
    version INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,
    artifacts JSONB NOT NULL DEFAULT '{}',
    events JSONB NOT NULL DEFAULT '[]',
    error TEXT,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS autonomous_runs_queue ON autonomous_runs(status, id);
CREATE UNIQUE INDEX IF NOT EXISTS autonomous_runs_active_project ON autonomous_runs(project_id)
WHERE status NOT IN ('completed', 'cancelled');
CREATE TABLE IF NOT EXISTS autonomous_run_history (
    run_id BIGINT NOT NULL REFERENCES autonomous_runs(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    snapshot JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(run_id, version)
);
"""


def decode(row):
    value = dict(row)
    for key in ("artifacts", "events"):
        if isinstance(value[key], str):
            value[key] = json.loads(value[key])
    return value


async def init_schema(conn):
    await conn.execute(SCHEMA)


async def create(project_id, settings, topic):
    import asyncpg
    try:
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow("""INSERT INTO autonomous_runs(project_id,artifacts)
                VALUES($1,$2::jsonb) RETURNING *""", project_id,
                json.dumps({"settings": settings, "topic": topic}, ensure_ascii=False))
        return decode(row)
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(409, "Project đã có một tiến trình tự động đang hoạt động.") from exc


async def get(run_id):
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM autonomous_runs WHERE id=$1", run_id)
    if not row:
        raise HTTPException(404, "Không tìm thấy tiến trình.")
    return decode(row)


async def recent(project_id=None):
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("""SELECT id,project_id,status,stage,version,revision,error,created_at,updated_at,
            artifacts->>'topic' AS topic FROM autonomous_runs
            WHERE ($1::integer IS NULL OR project_id=$1) ORDER BY id DESC LIMIT 50""", project_id)
    return [dict(row) for row in rows]


async def _write(conn, run, expected):
    row = await conn.fetchrow("""UPDATE autonomous_runs SET status=$2,stage=$3,revision=$4,
        artifacts=$5::jsonb,events=$6::jsonb,error=$7,version=version+1,lease_until=NULL,updated_at=now()
        WHERE id=$1 AND version=$8 RETURNING *""", run["id"], run["status"], run["stage"], run["revision"],
        json.dumps(run["artifacts"], ensure_ascii=False, allow_nan=False),
        json.dumps(run["events"], ensure_ascii=False), run.get("error"), expected)
    if row:
        await conn.execute("""INSERT INTO autonomous_run_history(run_id,version,snapshot)
            VALUES($1,$2,$3::jsonb)""", row["id"], row["version"],
            json.dumps(decode(row), ensure_ascii=False, default=str, allow_nan=False))
    return decode(row) if row else None


async def change(run_id, request, action="decision"):
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM autonomous_runs WHERE id=$1 FOR UPDATE", run_id)
            if not row:
                raise HTTPException(404, "Không tìm thấy tiến trình.")
            run = decode(row)
            check_version(run, request.expected_version)
            if action == "decision":
                run = decide(run, request)
            elif action == "resume":
                if run["status"] not in ("failed", "interrupted"):
                    raise HTTPException(409, "Chỉ tiếp tục bước bị lỗi hoặc gián đoạn.")
                run.update(status="queued", error=None)
                event(run, "Người dùng yêu cầu tiếp tục bước đang dở.")
            elif action == "cancel":
                if run["status"] in ("completed", "cancelled"):
                    raise HTTPException(409, "Tiến trình đã kết thúc.")
                run["status"] = "cancelled"
                event(run, "Người dùng dừng tiến trình.")
            return await _write(conn, run, request.expected_version)


async def save_proposal_revision(run_id, expected_version, draft, instruction):
    """Persist a user-requested proposal continuation with optimistic versioning."""
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM autonomous_runs WHERE id=$1 FOR UPDATE", run_id)
            if not row:
                raise HTTPException(404, "Không tìm thấy tiến trình.")
            run = decode(row)
            check_version(run, expected_version)
            if not run["artifacts"].get("proposal_draft"):
                raise HTTPException(409, "Run này chưa có bản đề xuất để viết tiếp.")
            run["artifacts"]["proposal_draft"] = draft
            run["artifacts"]["claim_evidence_map"] = draft["claim_evidence_map"]
            run["artifacts"].pop("proposal_draft_error", None)
            state = run["artifacts"].setdefault("paper_state", {})
            state["stage"] = "draft_with_blockers"
            state["blockers"] = draft.get("blockers", [])
            event(run, "Người dùng yêu cầu viết tiếp bản đề xuất: " + instruction[:240])
            saved = await _write(conn, run, expected_version)
            if not saved:
                raise HTTPException(409, "Run đã thay đổi; hãy tải lại trước khi viết tiếp.")
            return saved


async def claim():
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # An expired lease needs an explicit retry. It must not silently
            # repeat a partially committed external/legacy writing operation.
            await conn.execute("""UPDATE autonomous_runs SET status='interrupted',version=version+1,
                error='Worker bị gián đoạn. Có thể tiếp tục từ bước đã lưu.',lease_until=NULL
                WHERE status='running' AND lease_until < now()""")
            row = await conn.fetchrow("""SELECT * FROM autonomous_runs WHERE status='queued'
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1""")
            if not row:
                return None
            row = await conn.fetchrow("""UPDATE autonomous_runs SET status='running',version=version+1,
                lease_until=now()+interval '2 minutes',updated_at=now() WHERE id=$1 RETURNING *""", row["id"])
            return decode(row)


async def heartbeat(run):
    async with get_pool().acquire() as conn:
        return await conn.fetchval("""UPDATE autonomous_runs SET lease_until=now()+interval '2 minutes'
            WHERE id=$1 AND version=$2 AND status='running' RETURNING id""", run["id"], run["version"])


async def collection_progress(run, value):
    async with get_pool().acquire() as conn:
        return await conn.fetchval("""UPDATE autonomous_runs
            SET artifacts=jsonb_set(artifacts,'{collection_progress}',$3::jsonb),updated_at=now()
            WHERE id=$1 AND version=$2 AND status='running' RETURNING id""",
            run["id"], run["version"], json.dumps(value, ensure_ascii=False))


async def save_worker(run):
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            return await _write(conn, run, run["version"])


async def history(run_id):
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT version,snapshot,created_at FROM autonomous_run_history WHERE run_id=$1 ORDER BY version", run_id)
    return [{"version": row["version"], "created_at": row["created_at"],
             "snapshot": json.loads(row["snapshot"]) if isinstance(row["snapshot"], str) else row["snapshot"]}
            for row in rows]
