"""Tests for the Cloud Run Jobs queue-drain transport (src/cloudrun_job.py).

No ComfyUI/GPU: handler.handler and boot_from_env are mocked, and the queue
lives under tmp_path so no real GCS mount is needed.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_REPO_ROOT), str(_REPO_ROOT / "src")]

import cloudrun_job  # noqa: E402


def _write_pending(queue_dir, name, payload, mtime=None):
    pending = queue_dir / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    path = pending / name
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))
    return path


def _run_main(monkeypatch, queue_dir, idle_exit_sec="0.2", poll_sec="0.05"):
    monkeypatch.setenv("QUEUE_DIR", str(queue_dir))
    monkeypatch.setenv("IDLE_EXIT_SEC", idle_exit_sec)
    monkeypatch.setenv("QUEUE_POLL_SEC", poll_sec)
    with patch.object(cloudrun_job, "boot_from_env", return_value="fake-boot-state"):
        cloudrun_job.main()


def test_processes_oldest_first_and_clears_claimed(tmp_path, monkeypatch):
    queue_dir = tmp_path / "queue"
    _write_pending(queue_dir, "a.json", {"input": {"n": "a"}}, mtime=100)
    _write_pending(queue_dir, "b.json", {"input": {"n": "b"}}, mtime=50)

    calls = []

    def fake_handler(job):
        calls.append(job["input"]["n"])
        return {"status": "success"}

    with patch.object(cloudrun_job.handler_module, "handler", side_effect=fake_handler):
        _run_main(monkeypatch, queue_dir)

    assert calls == ["b", "a"]
    assert list((queue_dir / "claimed").glob("*.json")) == []

    done_a = json.loads((queue_dir / "done" / "a.json").read_text())
    done_b = json.loads((queue_dir / "done" / "b.json").read_text())
    assert done_a == {"id": "a", "status": "COMPLETED", "output": {"status": "success"}}
    assert done_b == {"id": "b", "status": "COMPLETED", "output": {"status": "success"}}


def test_malformed_json_produces_failed_envelope(tmp_path, monkeypatch):
    queue_dir = tmp_path / "queue"
    _write_pending(queue_dir, "bad.json", "not json")

    with patch.object(cloudrun_job.handler_module, "handler") as mock_handler:
        _run_main(monkeypatch, queue_dir)

    mock_handler.assert_not_called()
    done = json.loads((queue_dir / "done" / "bad.json").read_text())
    assert done["id"] == "bad"
    assert done["status"] == "FAILED"
    assert list((queue_dir / "claimed").glob("*.json")) == []


def test_missing_input_produces_failed_envelope(tmp_path, monkeypatch):
    queue_dir = tmp_path / "queue"
    _write_pending(queue_dir, "noinput.json", {"foo": "bar"})

    with patch.object(cloudrun_job.handler_module, "handler") as mock_handler:
        _run_main(monkeypatch, queue_dir)

    mock_handler.assert_not_called()
    done = json.loads((queue_dir / "done" / "noinput.json").read_text())
    assert done["status"] == "FAILED"


def test_handler_exception_produces_failed_envelope(tmp_path, monkeypatch):
    queue_dir = tmp_path / "queue"
    _write_pending(queue_dir, "boom.json", {"input": {}})

    with patch.object(cloudrun_job.handler_module, "handler", side_effect=RuntimeError("kaboom")):
        _run_main(monkeypatch, queue_dir)

    done = json.loads((queue_dir / "done" / "boom.json").read_text())
    assert done["status"] == "FAILED"
    assert "kaboom" in done["error"]


def test_refresh_worker_exits_after_that_job_leaving_other_pending(tmp_path, monkeypatch):
    queue_dir = tmp_path / "queue"
    _write_pending(queue_dir, "first.json", {"input": {"n": "first"}}, mtime=50)
    _write_pending(queue_dir, "second.json", {"input": {"n": "second"}}, mtime=100)

    def fake_handler(job):
        if job["input"]["n"] == "first":
            return {"status": "success", "refresh_worker": True}
        raise AssertionError("second job should not run before process exit")

    with patch.object(cloudrun_job.handler_module, "handler", side_effect=fake_handler):
        _run_main(monkeypatch, queue_dir, idle_exit_sec="5")

    done_first = json.loads((queue_dir / "done" / "first.json").read_text())
    assert done_first["status"] == "COMPLETED"
    assert "refresh_worker" not in done_first["output"]

    assert (queue_dir / "pending" / "second.json").exists()
    assert not (queue_dir / "done" / "second.json").exists()


def test_idle_exit_when_queue_empty(tmp_path, monkeypatch):
    queue_dir = tmp_path / "queue"
    with patch.object(cloudrun_job.handler_module, "handler") as mock_handler:
        _run_main(monkeypatch, queue_dir, idle_exit_sec="0.1", poll_sec="0.02")

    mock_handler.assert_not_called()
    assert (queue_dir / "pending").is_dir()
    assert (queue_dir / "claimed").is_dir()
    assert (queue_dir / "done").is_dir()
