"""Tests for the Cloud Run HTTP transport (src/cloudrun_server.py).

No ComfyUI/GPU: handler.handler is mocked, and the boot state consumed by
GET /ready is injected directly rather than running senai_worker.boot().
"""
import http.client
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from http.server import ThreadingHTTPServer

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_REPO_ROOT), str(_REPO_ROOT / "src")]

import cloudrun_server  # noqa: E402


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cloudrun_server.SenaiCloudRunHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        srv.shutdown()
        thread.join(5)


def _post(port, path, payload_bytes, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=payload_bytes, headers=headers or {})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        return resp.status, data
    finally:
        conn.close()


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        return resp.status, data
    finally:
        conn.close()


def test_run_happy_path_returns_completed_envelope(server):
    canned_output = {"status": "success", "protocol": "senai-worker/1", "outputs": []}
    with patch.object(cloudrun_server.handler_module, "handler", return_value=dict(canned_output)) as mock_handler:
        status, data = _post(server, "/run", json.dumps({"input": {"protocol": "senai-worker/1"}}).encode())

    assert status == 200
    assert data["status"] == "COMPLETED"
    assert data["output"] == canned_output
    mock_handler.assert_called_once()
    job_arg = mock_handler.call_args.args[0]
    assert job_arg["input"] == {"protocol": "senai-worker/1"}
    assert isinstance(job_arg["id"], str) and job_arg["id"]


def test_run_concurrent_second_call_gets_429(server):
    entered = threading.Event()
    release = threading.Event()

    def blocking_handler(job):
        entered.set()
        assert release.wait(5), "test deadlocked waiting for release"
        return {"status": "success", "protocol": "senai-worker/1", "outputs": []}

    results = {}
    with patch.object(cloudrun_server.handler_module, "handler", side_effect=blocking_handler):
        first = threading.Thread(
            target=lambda: results.update(
                first=_post(server, "/run", json.dumps({"input": {}}).encode())
            )
        )
        first.start()
        assert entered.wait(5), "first /run never started"

        status, data = _post(server, "/run", json.dumps({"input": {}}).encode())

        release.set()
        first.join(5)

    assert status == 429
    assert data["status"] == "FAILED"
    assert results["first"][0] == 200


def test_run_malformed_json_returns_400(server):
    status, data = _post(server, "/run", b"not json")
    assert status == 400
    assert data["status"] == "FAILED"


def test_run_missing_input_returns_400(server):
    status, data = _post(server, "/run", json.dumps({}).encode())
    assert status == 400
    assert data["status"] == "FAILED"


def test_healthz_always_200(server):
    status, data = _get(server, "/healthz")
    assert status == 200
    assert data["status"] == "ok"


def test_ready_503_when_not_booted(server):
    with patch.object(cloudrun_server.handler_module, "_BOOT_STATE", None):
        status, data = _get(server, "/ready")
    assert status == 503
    assert data["unready_code"] == "NOT_BOOTED"


def test_ready_503_when_boot_state_not_ready(server):
    fake_state = SimpleNamespace(ready=False, unready_code="MODEL_CACHE_MISSING")
    with patch.object(cloudrun_server.handler_module, "_BOOT_STATE", fake_state):
        status, data = _get(server, "/ready")
    assert status == 503
    assert data["unready_code"] == "MODEL_CACHE_MISSING"


def test_ready_503_when_comfy_unreachable(server):
    fake_state = SimpleNamespace(ready=True, unready_code=None)
    with patch.object(cloudrun_server.handler_module, "_BOOT_STATE", fake_state), patch.object(
        cloudrun_server.handler_module.COMFY_CLIENT, "system_stats", side_effect=RuntimeError("down")
    ):
        status, data = _get(server, "/ready")
    assert status == 503
    assert data["unready_code"] == "COMFYUI_UNREACHABLE"


def test_ready_200_when_ready_and_comfy_reachable(server):
    fake_state = SimpleNamespace(ready=True, unready_code=None)
    with patch.object(cloudrun_server.handler_module, "_BOOT_STATE", fake_state), patch.object(
        cloudrun_server.handler_module.COMFY_CLIENT, "system_stats", return_value={}
    ):
        status, data = _get(server, "/ready")
    assert status == 200
    assert data["status"] == "ready"


def test_idle_exit_watchdog_exits_after_idle():
    exits = []
    cloudrun_server._LAST_ACTIVITY[0] = cloudrun_server.time.monotonic() - 10
    cloudrun_server._idle_exit_watchdog(5, poll_sec=0.01, exit_fn=exits.append)
    assert exits == [0]
    cloudrun_server._RUN_LOCK.release()


def test_idle_exit_watchdog_waits_while_job_runs():
    exits = []
    cloudrun_server._LAST_ACTIVITY[0] = cloudrun_server.time.monotonic() - 10
    cloudrun_server._RUN_LOCK.acquire()
    thread = threading.Thread(
        target=cloudrun_server._idle_exit_watchdog, args=(5,), kwargs={"poll_sec": 0.01, "exit_fn": exits.append}
    )
    thread.start()
    thread.join(0.2)
    assert exits == []
    cloudrun_server._RUN_LOCK.release()
    thread.join(2)
    assert exits == [0]
    cloudrun_server._RUN_LOCK.release()
