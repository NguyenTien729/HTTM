"""A durable queue consumer; each completed stage is committed separately."""
import asyncio
import contextlib
import logging

from fastapi import HTTPException

from . import store
from .pipeline import execute_stage
from .state import event, finish_stage

logger = logging.getLogger(__name__)


async def _keep_lease(run):
    while True:
        await asyncio.sleep(20)
        if not await store.heartbeat(run):
            raise RuntimeError("Tiến trình đã bị dừng hoặc được worker khác nhận.")


async def process(run):
    work = asyncio.create_task(execute_stage(run))
    lease = asyncio.create_task(_keep_lease(run))
    try:
        done, _ = await asyncio.wait({work, lease}, timeout=15*60, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise TimeoutError("Bước xử lý vượt quá 15 phút; có thể thử lại.")
        if lease in done:
            lease.result()
        result = work.result()
        await store.save_worker(finish_stage(run, result))
    except asyncio.CancelledError:
        # Shutdown leaves the lease to expire; resume is explicit after restart.
        raise
    except Exception as exc:
        logger.exception("Autonomous run %s failed at %s", run["id"], run["stage"])
        run["status"] = "failed"
        run["error"] = (str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
                        if isinstance(exc, (ValueError, TimeoutError)) else type(exc).__name__)
        event(run, "Bước xử lý thất bại: " + run["error"])
        await store.save_worker(run)
    finally:
        for task in (work, lease):
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def run_worker():
    while True:
        try:
            run = await store.claim()
            if run:
                await process(run)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Autonomous queue unavailable")
        await asyncio.sleep(2)
