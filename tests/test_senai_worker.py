import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import time

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(Path(__file__).resolve().parent)]

import comfy_client
import guard
import media_output
import senai_worker
from senai_errors import ERROR_TABLE, PROTOCOL
from fake_comfy import FakeComfy, executing, progress_state, execution_error

RESPONSE_SCHEMA = json.loads((ROOT / "contract/senai-worker-1/response.schema.json").read_text())
PINS = __import__("yaml").safe_load((ROOT / "contract/senai-worker-1/pins.yaml").read_text())

ALLOWED_CLASS_TYPES = frozenset({"KSampler", "SaveVideo", "CLIPTextEncode", "LoadImage"})


def validate_response(output):
    # The real runpod SDK pops "error"/"refresh_worker" out of whatever handler()
    # returns before it becomes `output` (rp_job.run_job); validate what actually
    # reaches `output` on the wire, not our pre-pop return value.
    sdk_output = {k: v for k, v in output.items() if k not in ("error", "refresh_worker")}
    envelope = {"id": "t", "status": "COMPLETED", "output": sdk_output}
    jsonschema.validate(envelope, RESPONSE_SCHEMA)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def make_workflow(sampler_class="KSampler"):
    return {
        "10": {"class_type": sampler_class, "inputs": {}},
        "11": {"class_type": "SaveVideo", "inputs": {}},
    }


def make_job_input(
    workflow, *, deadline_at=None, no_progress_sec=None, no_progress_load_sec=None, max_execution_sec=None
):
    trace = {
        "generation_id": "gen-1",
        "attempt": 1,
        "binding_alias": "b",
        "binding_revision": 1,
        "adapter": "a",
        "workflow_id": "w",
        "graph_sha256": "a" * 64,
    }
    limits = {"deadline_at": deadline_at or iso(datetime.now(timezone.utc) + timedelta(hours=1))}
    if no_progress_sec is not None:
        limits["no_progress_sec"] = no_progress_sec
    if no_progress_load_sec is not None:
        limits["no_progress_load_sec"] = no_progress_load_sec
    if max_execution_sec is not None:
        limits["max_execution_sec"] = max_execution_sec
    return {"protocol": PROTOCOL, "workflow": workflow, "inputs": [], "trace": trace, "limits": limits}


def make_boot_state(tmp_path, fake, client=None, *, ready=True, unready_code=None, unready_message=None,
                     allowed_class_types=ALLOWED_CLASS_TYPES, ready_timeout_sec=5, limits=None):
    """fake is the FakeComfy server (holds response data); client is the
    ComfyClient boot() actually talks to. Passing the raw server object to
    boot() would silently no-op every HTTP call, so callers must go through
    ComfyClient — build one here if the caller didn't already."""
    state_path = tmp_path / "state.json"
    timeline_path = tmp_path / "timeline"
    state = {
        "protocol": PROTOCOL,
        "workflows": "test.yaml",
        "manifest_sha256": "b" * 64,
        "comfyui": "v0.36.0",
        "ready": ready,
        "unready_code": unready_code,
        "unready_message": unready_message,
        "models": {"declared": 1, "present": 1, "missing": [], "bytes_manifest": 0, "bytes_visible": 0},
        "declared_model_names": [],
        "allowed_class_types": sorted(allowed_class_types),
        "custom_nodes": [],
        "limits": limits or {"no_progress_sec": 120, "no_progress_load_sec": 600, "execution_ceiling_sec": 1800},
        "warmup_graph": None,
    }
    state_path.write_text(json.dumps(state))
    timeline_path.write_text(f"start {time.time()}\n")
    fake.object_info = {ct: {} for ct in allowed_class_types}
    client = client or comfy_client.ComfyClient(fake.base_url)
    return senai_worker.boot(state_path=state_path, timeline_path=timeline_path, comfy=client, ready_timeout_sec=ready_timeout_sec)


class FakeS3:
    def __init__(self):
        self.put_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://fake.example/{Params['Bucket']}/{Params['Key']}"


def _fake_probe(path, *, runner=None):
    return {
        "media_type": "video/mp4",
        "width": 1280,
        "height": 704,
        "duration_sec": 5.04,
        "fps": 24.0,
        "has_audio": True,
    }


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    monkeypatch.setenv("COMFY_INPUT_DIR", str(input_dir))
    monkeypatch.setenv("COMFY_OUTPUT_DIR", str(output_dir))
    monkeypatch.setattr(media_output, "probe_media", _fake_probe)
    fake_s3 = FakeS3()
    monkeypatch.setattr(media_output, "make_s3_client", lambda: (fake_s3, "test-bucket"))
    return {"output_dir": output_dir, "s3": fake_s3}


def write_output_file(output_dir, filename, content=b"fake video bytes"):
    (output_dir / filename).write_bytes(content)


def make_comfy(tmp_path):
    fake = FakeComfy()
    fake.start()
    client = comfy_client.ComfyClient(fake.base_url)
    return fake, client


# ---- 1: ok ----
def test_ok_success_and_cold_flag(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.history_store["p1"] = {"outputs": {"11": {"videos": [{"filename": "out.mp4", "subfolder": "", "type": "output"}]}}}
        fake.ws_script = [
            {"message": executing("10")},
            {"message": progress_state({"10": {"value": 1, "max": 2, "state": "running"}})},
            {"message": executing(None)},
        ]
        write_output_file(worker_env["output_dir"], "out.mp4")

        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())

        result1 = senai_worker.run_job({"id": "job-1", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result1)
        assert result1["status"] == "success"
        assert result1["timings"]["cold"] is True
        assert "refresh_worker" not in result1
        trace = result1["trace"]
        for key in ("generation_id", "attempt", "binding_alias", "binding_revision", "adapter", "workflow_id", "graph_sha256"):
            assert trace[key] == job_input["trace"][key]
        for key in ("rp_job_id", "worker_id", "prompt_id", "image", "comfyui", "workflows", "manifest_sha256", "gpu"):
            assert key in trace

        result2 = senai_worker.run_job({"id": "job-2", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result2)
        assert result2["timings"]["cold"] is False

        print("SCENARIO 1 FULL OUTPUT:\n", json.dumps(result1, indent=2))
    finally:
        fake.stop()


# ---- 2: hang_after_progress ----
def test_hang_after_progress_triggers_no_progress(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.ws_script = [
            {"message": executing("10")},
            {"message": progress_state({"10": {"value": 1, "max": 5, "state": "running"}})},
        ]
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow(), no_progress_sec=1)

        start = time.monotonic()
        result = senai_worker.run_job({"id": "job-2", "input": job_input}, boot_state=boot_state, comfy=client)
        elapsed = time.monotonic() - start

        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "NO_PROGRESS"
        assert result["refresh_worker"] is True
        assert elapsed <= 11.0
        assert ("interrupt",) in fake.calls
        assert ("queue_delete",) in fake.calls
        prompt_idx = fake.calls.index(("prompt", "job-2"))
        interrupt_idx = fake.calls.index(("interrupt",))
        queue_idx = fake.calls.index(("queue_delete",))
        assert prompt_idx < interrupt_idx < queue_idx
        print(f"SCENARIO 2 elapsed={elapsed:.2f}s calls={fake.calls}")
    finally:
        fake.stop()


# ---- 3: silent_loader ----
def test_silent_loader_does_not_trigger_no_progress(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.history_store["p1"] = {"outputs": {"11": {"videos": [{"filename": "out.mp4", "subfolder": "", "type": "output"}]}}}
        fake.ws_script = [
            {"message": executing("10")},
            {"delay": 2.0, "message": progress_state({"10": {"value": 1, "max": 2, "state": "running"}})},
            {"message": executing(None)},
        ]
        write_output_file(worker_env["output_dir"], "out.mp4")
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(
            make_workflow(sampler_class="CLIPTextEncode"), no_progress_sec=1, no_progress_load_sec=5
        )
        result = senai_worker.run_job({"id": "job-3", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "success"
    finally:
        fake.stop()


# ---- 4: deadline ----
def test_execution_deadline(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.ws_script = [{"message": executing("10")}] + [
            {"delay": 0.3, "message": progress_state({"10": {"value": i, "max": 20, "state": "running"}})}
            for i in range(20)
        ]
        boot_state = make_boot_state(tmp_path, fake, client)
        deadline_at = iso(datetime.now(timezone.utc) + timedelta(seconds=1))
        job_input = make_job_input(make_workflow(), deadline_at=deadline_at)
        start = time.monotonic()
        result = senai_worker.run_job({"id": "job-4", "input": job_input}, boot_state=boot_state, comfy=client)
        elapsed = time.monotonic() - start
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "EXECUTION_DEADLINE"
        assert elapsed < 8.0
    finally:
        fake.stop()


# ---- 4b: max_execution_sec budget ----
def test_max_execution_sec_budget_triggers_deadline_before_platform_timeout(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.ws_script = [{"message": executing("10")}] + [
            {"delay": 0.3, "message": progress_state({"10": {"value": i, "max": 20, "state": "running"}})}
            for i in range(40)
        ]
        boot_state = make_boot_state(tmp_path, fake, client)
        deadline_at = iso(datetime.now(timezone.utc) + timedelta(hours=1))
        job_input = make_job_input(make_workflow(), deadline_at=deadline_at, max_execution_sec=35)
        start = time.monotonic()
        result = senai_worker.run_job({"id": "job-4b", "input": job_input}, boot_state=boot_state, comfy=client)
        elapsed = time.monotonic() - start
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "EXECUTION_DEADLINE"
        # budget = max_execution_sec (35) - PLATFORM_TIMEOUT_MARGIN_SEC (30) = 5s
        assert 4.0 <= elapsed < 10.0
    finally:
        fake.stop()


def test_max_execution_sec_budget_clamped_to_one_second(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.ws_script = [{"message": executing("10")}] + [
            {"delay": 0.3, "message": progress_state({"10": {"value": i, "max": 20, "state": "running"}})}
            for i in range(40)
        ]
        boot_state = make_boot_state(tmp_path, fake, client)
        deadline_at = iso(datetime.now(timezone.utc) + timedelta(hours=1))
        job_input = make_job_input(make_workflow(), deadline_at=deadline_at, max_execution_sec=10)
        start = time.monotonic()
        result = senai_worker.run_job({"id": "job-4c", "input": job_input}, boot_state=boot_state, comfy=client)
        elapsed = time.monotonic() - start
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "EXECUTION_DEADLINE"
        assert elapsed < 5.0
    finally:
        fake.stop()


# ---- 4d: EXECUTION_DEADLINE message includes node location ----
def test_execution_deadline_message_includes_node_location(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.ws_script = [
            {"message": executing("10")},
            {"message": progress_state({"10": {"value": 2, "max": 4, "state": "running"}})},
        ] + [
            {"delay": 0.3, "message": progress_state({"10": {"value": 2, "max": 4, "state": "running"}})}
            for _ in range(20)
        ]
        boot_state = make_boot_state(tmp_path, fake, client)
        deadline_at = iso(datetime.now(timezone.utc) + timedelta(seconds=1))
        job_input = make_job_input(make_workflow(sampler_class="KSampler"), deadline_at=deadline_at)
        result = senai_worker.run_job({"id": "job-4d", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "EXECUTION_DEADLINE"
        assert "at node 10 (KSampler) 2/4" in result["failure"]["message"]
    finally:
        fake.stop()


# ---- 4e: node_sec timings ----
def test_node_sec_recorded_on_success(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.history_store["p1"] = {"outputs": {"11": {"videos": [{"filename": "out.mp4", "subfolder": "", "type": "output"}]}}}
        fake.ws_script = [
            {"message": executing("10")},
            {"delay": 0.05, "message": progress_state({"10": {"value": 1, "max": 2, "state": "running"}})},
            {"delay": 0.05, "message": executing("11")},
            {"delay": 0.05, "message": progress_state({"11": {"value": 1, "max": 1, "state": "running"}})},
            {"delay": 0.05, "message": executing(None)},
        ]
        write_output_file(worker_env["output_dir"], "out.mp4")
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())

        result = senai_worker.run_job({"id": "job-4e", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "success"
        node_sec = result["timings"]["node_sec"]
        assert set(node_sec) == {"10", "11"}
        assert all(v >= 0 for v in node_sec.values())
    finally:
        fake.stop()


def test_node_sec_recorded_on_deadline_error(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.ws_script = [{"message": executing("10")}] + [
            {"delay": 0.3, "message": progress_state({"10": {"value": i, "max": 20, "state": "running"}})}
            for i in range(20)
        ]
        boot_state = make_boot_state(tmp_path, fake, client)
        deadline_at = iso(datetime.now(timezone.utc) + timedelta(seconds=1))
        job_input = make_job_input(make_workflow(), deadline_at=deadline_at)
        result = senai_worker.run_job({"id": "job-4f", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "EXECUTION_DEADLINE"
        assert "10" in result["timings"]["node_sec"]
        assert result["timings"]["node_sec"]["10"] >= 0
    finally:
        fake.stop()


def test_node_sec_recorded_on_execution_error(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.ws_script = [
            {"message": executing("10")},
            {"delay": 0.05, "message": progress_state({"10": {"value": 1, "max": 2, "state": "running"}})},
            {"delay": 0.05, "message": executing("11")},
            {"delay": 0.05, "message": execution_error("11", "SaveVideo")},
        ]
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())

        result = senai_worker.run_job({"id": "job-4g", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        node_sec = result["timings"]["node_sec"]
        assert set(node_sec) == {"10", "11"}
        assert node_sec["11"] > 0
    finally:
        fake.stop()


# ---- 5: ws_drop_then_complete ----
def test_ws_drop_then_complete_reconciles_via_history(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.history_store["p1"] = {"outputs": {"11": {"videos": [{"filename": "out.mp4", "subfolder": "", "type": "output"}]}}}
        fake.ws_script = [
            {"message": executing("10")},
            {"message": progress_state({"10": {"value": 1, "max": 2, "state": "running"}})},
            {"action": "close"},
        ]
        write_output_file(worker_env["output_dir"], "out.mp4")
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())
        result = senai_worker.run_job({"id": "job-5", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "success"
    finally:
        fake.stop()


# ---- 6: reject_400 ----
def test_prompt_rejected_400(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        node_errors = {f"n{i}": {"value_not_in_list": "bad"} for i in range(25)}
        fake.prompt_response = lambda graph, client_id: (400, {"error": {"message": "bad workflow"}, "node_errors": node_errors})
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())
        result = senai_worker.run_job({"id": "job-6", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "PROMPT_REJECTED"
        assert len(result["failure"]["node_errors"]) <= 20
    finally:
        fake.stop()


# ---- 7: oom ----
def test_cuda_oom(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.ws_script = [
            {"message": executing("10")},
            {"message": execution_error("10", "KSampler", exception_type="torch.OutOfMemoryError", exception_message="CUDA out of memory")},
        ]
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())
        result = senai_worker.run_job({"id": "job-7", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "CUDA_OOM"
        assert result["refresh_worker"] is True
    finally:
        fake.stop()


# ---- 8: node_exception ----
def test_node_exception(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.ws_script = [
            {"message": executing("10")},
            {"message": execution_error("10", "KSampler", exception_type="RuntimeError", exception_message="bad tensor shape")},
        ]
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())
        result = senai_worker.run_job({"id": "job-8", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "NODE_EXCEPTION"
        assert result["failure"]["node_id"] == "10"
        assert result["failure"]["class_type"] == "KSampler"
        assert "refresh_worker" not in result
    finally:
        fake.stop()


# ---- 9: crash ----
def test_comfyui_crashed(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.ws_script = [
            {"message": executing("10")},
            {"delay": 0.2, "action": "kill"},
        ]
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())
        result = senai_worker.run_job({"id": "job-9", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "COMFYUI_CRASHED"
        assert result["refresh_worker"] is True
    finally:
        fake.stop()


# ---- 10: deadline_at in the past ----
def test_deadline_passed_rejected_before_submit(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        boot_state = make_boot_state(tmp_path, fake, client)
        past = iso(datetime.now(timezone.utc) - timedelta(seconds=5))
        job_input = make_job_input(make_workflow(), deadline_at=past)
        start = time.monotonic()
        result = senai_worker.run_job({"id": "job-10", "input": job_input}, boot_state=boot_state, comfy=client)
        elapsed = time.monotonic() - start
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "DEADLINE_PASSED"
        assert elapsed < 0.1
        assert not any(call[0] == "prompt" for call in fake.calls)
    finally:
        fake.stop()


# ---- 11: unready worker ----
def test_unready_worker_fails_fast_and_health_reports_unready(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        boot_state = make_boot_state(tmp_path, fake, client, ready=False, unready_code="MODEL_CACHE_MISSING", unready_message="missing models")
        job_input = make_job_input(make_workflow())
        start = time.monotonic()
        result = senai_worker.run_job({"id": "job-11", "input": job_input}, boot_state=boot_state, comfy=client)
        elapsed = time.monotonic() - start
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "MODEL_CACHE_MISSING"
        assert elapsed < 0.1

        health_result = senai_worker.run_job({"id": "job-11h", "input": {"protocol": PROTOCOL, "health_check": True}}, boot_state=boot_state, comfy=client)
        validate_response(health_result)
        assert health_result["status"] == "healthy"
        assert health_result["worker"]["ready"] is False
        assert health_result["worker"]["unready_code"] == "MODEL_CACHE_MISSING"
    finally:
        fake.stop()


# ---- 12: missing protocol ----
def test_unsupported_protocol_without_legacy_flag(tmp_path, worker_env, monkeypatch):
    monkeypatch.delenv("LEGACY_UPSTREAM_INPUT", raising=False)
    fake, client = make_comfy(tmp_path)
    try:
        boot_state = make_boot_state(tmp_path, fake, client)
        result = senai_worker.run_job({"id": "job-12", "input": {"workflow": {}}}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "UNSUPPORTED_PROTOCOL"
    finally:
        fake.stop()


# ---- 13: unexpected internal exception ----
def test_internal_error_never_raises(tmp_path, worker_env, monkeypatch):
    fake, client = make_comfy(tmp_path)
    try:
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(guard, "rewrite_input_names", boom)
        result = senai_worker.run_job({"id": "job-13", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "INTERNAL"
        assert result["refresh_worker"] is True
    finally:
        fake.stop()


# ---- 14: boot never ready ----
def test_boot_never_ready_times_out_quickly(tmp_path):
    fake, client = make_comfy(tmp_path)
    try:
        fake.system_stats_fail = True
        state_path = tmp_path / "state.json"
        timeline_path = tmp_path / "timeline"
        state_path.write_text(json.dumps({
            "protocol": PROTOCOL, "workflows": "test.yaml", "manifest_sha256": "b" * 64, "comfyui": "v0.36.0",
            "ready": True, "unready_code": None, "unready_message": None,
            "models": {}, "declared_model_names": [], "allowed_class_types": [],
            "custom_nodes": [], "limits": {}, "warmup_graph": None,
        }))
        timeline_path.write_text(f"start {time.time()}\n")

        start = time.monotonic()
        boot_state = senai_worker.boot(state_path=state_path, timeline_path=timeline_path, comfy=client, ready_timeout_sec=1)
        elapsed = time.monotonic() - start

        assert elapsed <= 2.0
        assert boot_state.ready is False
        assert boot_state.unready_code == "COMFYUI_UNREACHABLE"
    finally:
        fake.stop()


# ---- 15: REFRESH_WORKER=never suppresses refresh even on oom ----
def test_refresh_worker_never_suppresses_flag(tmp_path, worker_env, monkeypatch):
    monkeypatch.setenv("REFRESH_WORKER", "never")
    fake, client = make_comfy(tmp_path)
    try:
        fake.ws_script = [
            {"message": executing("10")},
            {"message": execution_error("10", "KSampler", exception_type="torch.OutOfMemoryError", exception_message="CUDA out of memory")},
        ]
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())
        result = senai_worker.run_job({"id": "job-15", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "CUDA_OOM"
        assert "refresh_worker" not in result
    finally:
        fake.stop()


# ---- cuda from CUDA_VERSION env, per internals doc §8 ----
def test_cuda_from_env_var(tmp_path, monkeypatch):
    fake, client = make_comfy(tmp_path)
    try:
        monkeypatch.setenv("CUDA_VERSION", "12.8.1")
        boot_state = make_boot_state(tmp_path, fake, client)
        assert boot_state.cuda == "12.8.1"
    finally:
        fake.stop()


def test_cuda_unknown_without_env_var(tmp_path, monkeypatch):
    fake, client = make_comfy(tmp_path)
    try:
        monkeypatch.delenv("CUDA_VERSION", raising=False)
        boot_state = make_boot_state(tmp_path, fake, client)
        assert boot_state.cuda == "unknown"
    finally:
        fake.stop()


# ---- progress_update sourced from progress_state's running node, per §9a ----
def test_progress_update_uses_running_node_from_progress_state(tmp_path, worker_env, monkeypatch):
    sent = []
    monkeypatch.setattr(senai_worker.runpod.serverless, "progress_update", lambda job, payload: sent.append(payload))

    fake, client = make_comfy(tmp_path)
    try:
        fake.history_store["p1"] = {"outputs": {"11": {"videos": [{"filename": "out.mp4", "subfolder": "", "type": "output"}]}}}
        fake.ws_script = [
            {"message": executing("10")},
            {"message": progress_state({
                "9": {"value": 1, "max": 1, "state": "finished"},
                "10": {"value": 3, "max": 10, "state": "running"},
            })},
            {"message": executing(None)},
        ]
        write_output_file(worker_env["output_dir"], "out.mp4")
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())
        result = senai_worker.run_job({"id": "job-16", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "success"
        assert sent == [{"node": "10", "value": 3, "max": 10}]
    finally:
        fake.stop()


def test_progress_update_skipped_when_no_node_is_running(tmp_path, worker_env, monkeypatch):
    sent = []
    monkeypatch.setattr(senai_worker.runpod.serverless, "progress_update", lambda job, payload: sent.append(payload))

    fake, client = make_comfy(tmp_path)
    try:
        fake.history_store["p1"] = {"outputs": {"11": {"videos": [{"filename": "out.mp4", "subfolder": "", "type": "output"}]}}}
        fake.ws_script = [
            {"message": executing("10")},
            {"message": progress_state({"9": {"value": 1, "max": 1, "state": "finished"}})},
            {"message": executing(None)},
        ]
        write_output_file(worker_env["output_dir"], "out.mp4")
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())
        result = senai_worker.run_job({"id": "job-17", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "success"
        assert sent == []
    finally:
        fake.stop()


# ---------------------------------------------------------------------------
# Independent review fixes (contract 0.2.1): NO_PROGRESS baseline/grace window,
# ws-before-queue + /history status_str reconciliation, queue_prompt error
# classification + COMFY_PID_FILE liveness, missing boot state, cold after error.
# ---------------------------------------------------------------------------

# ---- fix 1: fetch time must not count against the no-progress window ----
def test_fix1_slow_fetch_does_not_count_against_no_progress(tmp_path, worker_env, monkeypatch):
    fake, client = make_comfy(tmp_path)
    try:
        # The real runpod SDK's progress_update() attempts a genuine (blocking)
        # network call in this test environment; stub it so it can't itself eat
        # into the tight no-progress windows these timing tests assert on.
        monkeypatch.setattr(senai_worker.runpod.serverless, "progress_update", lambda job, payload: None)

        original_fetch_inputs = guard.fetch_inputs

        def slow_fetch_inputs(*args, **kwargs):
            time.sleep(3)
            return original_fetch_inputs(*args, **kwargs)

        monkeypatch.setattr(guard, "fetch_inputs", slow_fetch_inputs)

        fake.history_store["p1"] = {"outputs": {"11": {"videos": [{"filename": "out.mp4", "subfolder": "", "type": "output"}]}}}
        fake.ws_script = [
            {"message": executing("10")},
            {"message": progress_state({"10": {"value": 1, "max": 2, "state": "running"}})},
            {"message": executing(None)},
        ]
        write_output_file(worker_env["output_dir"], "out.mp4")
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow(), no_progress_sec=1, no_progress_load_sec=10)
        result = senai_worker.run_job({"id": "job-fix1", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "success", result
    finally:
        fake.stop()


# ---- fix 1: pre-first-progress grace applies to any node, not just loaders ----
def test_fix1_pre_first_progress_grace_ignores_class_type(tmp_path, worker_env, monkeypatch):
    fake, client = make_comfy(tmp_path)
    try:
        monkeypatch.setattr(senai_worker.runpod.serverless, "progress_update", lambda job, payload: None)
        fake.history_store["p1"] = {"outputs": {"11": {"videos": [{"filename": "out.mp4", "subfolder": "", "type": "output"}]}}}
        fake.ws_script = [
            {"message": executing("10")},  # "10" is KSampler in make_workflow(), not a loader
            {"delay": 3.0, "message": progress_state({"10": {"value": 1, "max": 2, "state": "running"}})},
            {"message": executing(None)},
        ]
        write_output_file(worker_env["output_dir"], "out.mp4")
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow(), no_progress_sec=1, no_progress_load_sec=10)
        result = senai_worker.run_job({"id": "job-fix1b", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "success", result
    finally:
        fake.stop()


# ---- fix 1: the strict window applies once seen_progress is true ----
def test_fix1_strict_window_after_first_progress(tmp_path, worker_env, monkeypatch):
    fake, client = make_comfy(tmp_path)
    try:
        monkeypatch.setattr(senai_worker.runpod.serverless, "progress_update", lambda job, payload: None)
        fake.ws_script = [
            {"message": executing("10")},
            {"message": progress_state({"10": {"value": 1, "max": 5, "state": "running"}})},
            {"delay": 2.0, "message": progress_state({"10": {"value": 2, "max": 5, "state": "running"}})},
        ]
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow(), no_progress_sec=1, no_progress_load_sec=10)
        result = senai_worker.run_job({"id": "job-fix1c", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "NO_PROGRESS"
    finally:
        fake.stop()


# ---- fix 2: /history status_str == "error" reconciliation on ws disconnect ----
def test_fix2_history_status_error_detected_as_oom(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.history_store["p1"] = {
            "status": {
                "status_str": "error",
                "messages": [
                    ["execution_error", {
                        "node_id": "10", "node_type": "KSampler",
                        "exception_type": "torch.OutOfMemoryError", "exception_message": "CUDA out of memory",
                    }],
                ],
            },
            "outputs": {},
        }
        fake.ws_script = [{"action": "close"}]  # disconnect immediately -> forces /history reconciliation
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())
        result = senai_worker.run_job({"id": "job-fix2", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "CUDA_OOM"
    finally:
        fake.stop()


# ---- fix 3: HTTP 5xx from /prompt is COMFYUI_CRASHED, not an unhandled crash ----
def test_fix3_prompt_5xx_is_comfyui_crashed(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        fake.prompt_response = lambda graph, client_id: (503, {"error": "server error"})
        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())
        result = senai_worker.run_job({"id": "job-fix3", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "COMFYUI_CRASHED"
        assert result["refresh_worker"] is True
    finally:
        fake.stop()


# ---- fix 3: a dead COMFY_PID_FILE process blocks the job before /prompt ----
def test_fix3_dead_pid_file_blocks_before_prompt(tmp_path, worker_env, monkeypatch):
    fake, client = make_comfy(tmp_path)
    try:
        proc = subprocess.Popen(["true"])
        dead_pid = proc.pid
        proc.wait()
        pid_file = tmp_path / "comfyui.pid"
        pid_file.write_text(str(dead_pid))
        monkeypatch.setenv("COMFY_PID_FILE", str(pid_file))

        boot_state = make_boot_state(tmp_path, fake, client)
        job_input = make_job_input(make_workflow())
        result = senai_worker.run_job({"id": "job-fix3b", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "COMFYUI_CRASHED"
        assert not any(call[0] == "prompt" for call in fake.calls)
    finally:
        fake.stop()


# ---- fix 4: missing/corrupt boot state never crashes boot(); handler stays up ----
def test_fix4_missing_state_file_boots_unready_internal(tmp_path):
    fake, client = make_comfy(tmp_path)
    try:
        state_path = tmp_path / "state.json"  # never written
        timeline_path = tmp_path / "timeline"
        timeline_path.write_text(f"start {time.time()}\n")

        boot_state = senai_worker.boot(state_path=state_path, timeline_path=timeline_path, comfy=client, ready_timeout_sec=2)
        assert boot_state.ready is False
        assert boot_state.unready_code == "INTERNAL"
        assert boot_state.manifest_sha256 == hashlib.sha256(b"").hexdigest()

        job_input = make_job_input(make_workflow())
        result = senai_worker.run_job({"id": "job-fix4", "input": job_input}, boot_state=boot_state, comfy=client)
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "INTERNAL"

        health = senai_worker.run_job(
            {"id": "job-fix4h", "input": {"protocol": PROTOCOL, "health_check": True}}, boot_state=boot_state, comfy=client
        )
        validate_response(health)
        assert health["status"] == "healthy"
        assert health["worker"]["ready"] is False
        assert health["worker"]["manifest_sha256"] == hashlib.sha256(b"").hexdigest()
    finally:
        fake.stop()


# ---- fix 5: served_jobs (and therefore cold) advances on error too ----
def test_fix5_cold_false_on_job_after_a_prior_error(tmp_path, worker_env):
    fake, client = make_comfy(tmp_path)
    try:
        boot_state = make_boot_state(tmp_path, fake, client)

        bad_workflow = make_workflow()
        bad_workflow["999"] = {"class_type": "NotAllowedNode", "inputs": {}}
        result1 = senai_worker.run_job(
            {"id": "job-fix5a", "input": make_job_input(bad_workflow)}, boot_state=boot_state, comfy=client
        )
        validate_response(result1)
        assert result1["status"] == "error"
        assert result1["failure"]["code"] == "NODE_NOT_ALLOWED"

        fake.history_store["p1"] = {"outputs": {"11": {"videos": [{"filename": "out.mp4", "subfolder": "", "type": "output"}]}}}
        fake.ws_script = [
            {"message": executing("10")},
            {"message": progress_state({"10": {"value": 1, "max": 2, "state": "running"}})},
            {"message": executing(None)},
        ]
        write_output_file(worker_env["output_dir"], "out.mp4")
        result2 = senai_worker.run_job(
            {"id": "job-fix5b", "input": make_job_input(make_workflow())}, boot_state=boot_state, comfy=client
        )
        validate_response(result2)
        assert result2["status"] == "success", result2
        assert result2["timings"]["cold"] is False
    finally:
        fake.stop()


def test_error_table_matches_pins_yaml():
    pins_codes = PINS["error_codes"]
    assert set(pins_codes) == set(ERROR_TABLE)
    for code, spec in pins_codes.items():
        entry = ERROR_TABLE[code]
        assert entry.type == spec["type"]
        assert entry.stage == spec["stage"]
        assert entry.gpu_work == spec["gpu_work"]
        assert entry.infra == spec["infra"]
        assert entry.retryable == spec["retryable"]
        assert entry.refresh == spec["refresh"]
