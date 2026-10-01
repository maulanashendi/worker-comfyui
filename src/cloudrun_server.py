"""Cloud Run HTTP transport for the senai-worker/1 handler (SENAI_TRANSPORT=cloudrun).

Runs the exact same handler(job) that the RunPod serverless SDK calls, behind
a small stdlib HTTP server instead of runpod.serverless.start(). POST /run
executes one job synchronously and returns a RunPod-style envelope; GET
/healthz is a liveness probe; GET /ready is the Cloud Run startup probe,
backed by the same senai_worker.boot() state the RunPod transport computes at
import time (see handler.py's `if __name__ == "__main__"` block).

No new dependencies: this uses only the Python standard library (http.server),
since requirements.txt pulls in no ASGI/WSGI framework.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import handler as handler_module
import senai_worker

logger = logging.getLogger(__name__)

# Cloud Run is deployed with --concurrency 1, but this guard is the actual
# enforcement: a stray second request (retry, probe overlap, operator error)
# must get 429 instead of racing the single ComfyUI instance.
_RUN_LOCK = threading.Lock()


def _send_json(handler, status_code, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _schedule_refresh_exit():
    # Mirrors the RunPod transport's refresh_worker -> stopPod semantics:
    # exit this process so Cloud Run replaces the instance. Delayed slightly
    # so the HTTP response has time to flush to the socket first.
    def _exit():
        os._exit(0)

    timer = threading.Timer(1.0, _exit)
    timer.daemon = True
    timer.start()


class SenaiCloudRunHandler(BaseHTTPRequestHandler):
    server_version = "senai-cloudrun/1"

    def log_message(self, fmt, *args):  # noqa: A002 - BaseHTTPRequestHandler signature
        logger.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):
        if self.path == "/healthz":
            _send_json(self, 200, {"status": "ok"})
            return
        if self.path == "/ready":
            self._handle_ready()
            return
        _send_json(self, 404, {"error": "not found"})

    def _handle_ready(self):
        boot_state = handler_module._BOOT_STATE
        if boot_state is None or not boot_state.ready:
            code = boot_state.unready_code if boot_state is not None else "NOT_BOOTED"
            _send_json(self, 503, {"status": "not_ready", "unready_code": code})
            return
        try:
            handler_module.COMFY_CLIENT.system_stats()
        except Exception:
            _send_json(self, 503, {"status": "not_ready", "unready_code": "COMFYUI_UNREACHABLE"})
            return
        _send_json(self, 200, {"status": "ready"})

    def do_POST(self):
        if self.path != "/run":
            _send_json(self, 404, {"error": "not found"})
            return

        request_id = self.headers.get("X-Request-Id") or str(uuid.uuid4())

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw_body = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            _send_json(self, 400, {"id": request_id, "status": "FAILED", "error": "invalid JSON body"})
            return

        if not isinstance(body, dict) or not isinstance(body.get("input"), dict):
            _send_json(self, 400, {"id": request_id, "status": "FAILED", "error": "missing 'input' object"})
            return

        if not _RUN_LOCK.acquire(blocking=False):
            _send_json(self, 429, {"id": request_id, "status": "FAILED", "error": "a job is already running"})
            return

        try:
            job = {"id": request_id, "input": body["input"]}
            try:
                output = handler_module.handler(job)
            except Exception as exc:  # noqa: BLE001 - handler() never raises today, but a
                # transport-level guard must still cover a future regression; mirror the
                # RunPod SDK's FAILED-on-exception behaviour instead of a bare 500 traceback.
                logger.exception("Unhandled error in /run")
                _send_json(self, 500, {"id": request_id, "status": "FAILED", "error": str(exc)})
                return

            # The RunPod SDK pops refresh_worker off the handler's return dict
            # before it reaches /status (contract §4); replicate that here so
            # the HTTP response shape matches what senai's codec expects.
            refresh = bool(output.pop("refresh_worker", False)) if isinstance(output, dict) else False
            _send_json(self, 200, {"id": request_id, "status": "COMPLETED", "output": output})
            if refresh:
                _schedule_refresh_exit()
        finally:
            _RUN_LOCK.release()


def boot_from_env():
    """Boot senai_worker the same way for every Cloud Run transport (HTTP or job).

    Shared by this module's main() and cloudrun_job.main() so both read the
    same env vars and populate handler_module._BOOT_STATE identically.
    """
    return senai_worker.boot(
        state_path=Path(os.environ.get("SENAI_WORKER_STATE", "/tmp/senai-worker-state.json")),
        timeline_path=Path(os.environ.get("SENAI_BOOT_TIMELINE", "/tmp/senai-boot-timeline")),
        comfy=handler_module.COMFY_CLIENT,
        ready_timeout_sec=float(os.environ.get("COMFY_READY_TIMEOUT_SEC", "300")),
    )


def main():
    port = int(os.environ.get("PORT", "8080"))
    handler_module._BOOT_STATE = boot_from_env()
    server = ThreadingHTTPServer(("0.0.0.0", port), SenaiCloudRunHandler)
    logger.info("senai-worker cloudrun transport listening on 0.0.0.0:%d", port)
    server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
