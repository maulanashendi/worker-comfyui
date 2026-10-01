"""Cloud Run Jobs queue-drain transport (SENAI_TRANSPORT=cloudrun-job).

Boots exactly like SENAI_TRANSPORT=cloudrun (cloudrun_server.py), then drains
a file queue on a mounted GCS bucket instead of serving HTTP: each
pending/<id>.json is run through the same handler(job) that RunPod and the
cloudrun HTTP transport call. The process exits once the queue has been empty
for IDLE_EXIT_SEC, so a Cloud Run Jobs execution only bills the active span
instead of the un-configurable 5-17 min idle tail a Cloud Run *service* holds
open after its last request.

No new dependencies: this uses only the Python standard library.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
from pathlib import Path

import handler as handler_module

from cloudrun_server import boot_from_env

logger = logging.getLogger(__name__)


def _queue_dirs(queue_dir: Path):
    pending = queue_dir / "pending"
    claimed = queue_dir / "claimed"
    done = queue_dir / "done"
    heartbeat = queue_dir / "heartbeat"
    for directory in (pending, claimed, done, heartbeat):
        directory.mkdir(parents=True, exist_ok=True)
    return pending, claimed, done, heartbeat


def _next_pending(pending: Path):
    files = sorted(pending.glob("*.json"), key=lambda p: (p.stat().st_mtime, p.name))
    return files[0] if files else None


def _claim(pending_file: Path, claimed: Path) -> Path | None:
    target = claimed / pending_file.name
    try:
        os.rename(pending_file, target)
    except OSError:
        return None
    return target


def _write_atomic(directory: Path, filename: str, payload: dict) -> None:
    tmp = directory / f".{filename}.tmp"
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, directory / filename)


def _write_heartbeat(heartbeat: Path, state: str, processed: int) -> None:
    name = os.environ.get("CLOUD_RUN_EXECUTION") or socket.gethostname()
    _write_atomic(heartbeat, f"{name}.json", {"ts": time.time(), "state": state, "processed": processed})


def _run_one(claimed_file: Path, done: Path) -> bool:
    """Run one claimed job file; returns True if the handler signalled refresh_worker."""
    filename = claimed_file.name
    job_id = claimed_file.stem
    started = time.monotonic()

    try:
        raw = json.loads(claimed_file.read_text())
    except (OSError, ValueError) as exc:
        _write_atomic(done, filename, {"id": job_id, "status": "FAILED", "error": f"invalid JSON: {exc}"})
        claimed_file.unlink(missing_ok=True)
        logger.info("job %s FAILED (%.2fs): invalid JSON", job_id, time.monotonic() - started)
        return False

    if not isinstance(raw, dict) or not isinstance(raw.get("input"), dict):
        _write_atomic(done, filename, {"id": job_id, "status": "FAILED", "error": "missing 'input' object"})
        claimed_file.unlink(missing_ok=True)
        logger.info("job %s FAILED (%.2fs): missing input", job_id, time.monotonic() - started)
        return False

    job = {"id": job_id, "input": raw["input"]}
    try:
        output = handler_module.handler(job)
    except Exception as exc:  # noqa: BLE001 - mirror cloudrun_server's transport-level guard;
        # handler() never raises today, but this covers a future regression.
        _write_atomic(done, filename, {"id": job_id, "status": "FAILED", "error": str(exc)})
        claimed_file.unlink(missing_ok=True)
        logger.exception("job %s FAILED (%.2fs)", job_id, time.monotonic() - started)
        return False

    refresh = bool(output.pop("refresh_worker", False)) if isinstance(output, dict) else False
    _write_atomic(done, filename, {"id": job_id, "status": "COMPLETED", "output": output})
    claimed_file.unlink(missing_ok=True)
    logger.info("job %s COMPLETED (%.2fs)", job_id, time.monotonic() - started)
    return refresh


def main():
    queue_dir = Path(os.environ.get("QUEUE_DIR", "/queue"))
    idle_exit_sec = float(os.environ.get("IDLE_EXIT_SEC", "60"))
    poll_sec = float(os.environ.get("QUEUE_POLL_SEC", "2"))

    pending, claimed, done, heartbeat = _queue_dirs(queue_dir)

    processed = 0
    _write_heartbeat(heartbeat, "booting", processed)
    handler_module._BOOT_STATE = boot_from_env()
    _write_heartbeat(heartbeat, "idle", processed)

    idle_since = time.monotonic()
    last_heartbeat = time.monotonic()
    while True:
        pending_file = _next_pending(pending)
        if pending_file is None:
            now = time.monotonic()
            if now - last_heartbeat >= 10:
                _write_heartbeat(heartbeat, "idle", processed)
                last_heartbeat = now
            if now - idle_since >= idle_exit_sec:
                logger.info(
                    "cloudrun-job idle exit after %ds, processed=%d", int(now - idle_since), processed
                )
                return
            time.sleep(poll_sec)
            continue

        claimed_file = _claim(pending_file, claimed)
        if claimed_file is None:
            continue

        _write_heartbeat(heartbeat, "busy", processed)
        last_heartbeat = time.monotonic()
        refresh = _run_one(claimed_file, done)
        processed += 1
        idle_since = time.monotonic()
        _write_heartbeat(heartbeat, "idle", processed)
        last_heartbeat = time.monotonic()

        if refresh:
            logger.info("cloudrun-job exiting after refresh_worker, processed=%d", processed)
            return


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
