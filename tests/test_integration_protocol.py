"""Cross-module integration tests for senai-worker/1: real workflow_models,
guard, media_output, senai_worker, and handler wired together end-to-end
against a scripted fake_comfy. No module here is mocked except the S3 client
(network egress) and, where noted, ffprobe/ffmpeg availability.

Do not edit any source module from this file — if a scenario surfaces a bug
in guard/media_output/workflow_models, report it (file, line, symptom)
instead of patching around it.
"""
import functools
import http.server
import json
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import which
from unittest.mock import MagicMock

import base64
import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(Path(__file__).resolve().parent)]

import comfy_client
import guard
import handler
import media_output
import senai_worker
import workflow_models
from senai_errors import PROTOCOL
from fake_comfy import FakeComfy, executing, progress_state

RESPONSE_SCHEMA = json.loads((ROOT / "contract/senai-worker-1/response.schema.json").read_text())
WORKFLOW_DIR = ROOT / "workflow"

# Minimal valid 1x1 transparent PNG (magic bytes real enough for guard.sniff_media_type).
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def future_deadline(hours=1):
    return iso(datetime.now(timezone.utc) + timedelta(hours=hours))


def validate_response(output):
    # Mirrors the real runpod SDK: it pops "error"/"refresh_worker" out of
    # whatever handler() returns before that dict becomes `output` on the wire.
    sdk_output = {k: v for k, v in output.items() if k not in ("error", "refresh_worker")}
    jsonschema.validate({"id": "t", "status": "COMPLETED", "output": sdk_output}, RESPONSE_SCHEMA)


def ffmpeg_available():
    return which("ffmpeg") is not None


def make_output_mp4(path):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=8",
         "-t", "1", "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=30,
    )


def make_fake_s3():
    s3 = MagicMock()
    s3.generate_presigned_url.side_effect = (
        lambda op, Params, ExpiresIn: f"https://acct.r2.cloudflarestorage.com/{Params['Bucket']}/{Params['Key']}"
    )
    return s3


def write_sparse(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        if size > 0:
            handle.seek(size - 1)
            handle.write(b"\0")
        # size == 0: an empty file is enough (unpinned manifest entry, bytes: 0).


def seed_hf_cache(hf_cache_root, plan, *, corrupt_first=False):
    """Write one fixture file per model in `plan` at its HF snapshot path.
    Content is never read by workflow_models here (none of our fixture models
    pin a sha256), only presence and size. If corrupt_first, the first model
    with bytes > 0 gets a wrong size to force MODEL_CACHE_MISSING; returns its
    manifest path (or None if no model had bytes > 0 to corrupt)."""
    corrupted = None
    for item in plan.values():
        size = item.get("bytes") or 0
        hf = item["hf"]
        snapshot = workflow_models.resolve_snapshot_dir(hf_cache_root, hf["repo"], hf.get("revision"))
        path = snapshot / hf["file"]
        write_size = size
        if corrupt_first and corrupted is None and size > 0:
            write_size = size - 1
            corrupted = item["path"]
        write_sparse(path, write_size)
    return corrupted


def run_verify(monkeypatch, *, workflows, model_root, hf_cache_root, state_path, paths_path, aws_bucket="test-bucket"):
    monkeypatch.setenv("WORKFLOWS", workflows)
    monkeypatch.setenv("WORKFLOW_DIR", str(WORKFLOW_DIR))
    monkeypatch.setenv("COMFY_MODEL_ROOT", str(model_root))
    monkeypatch.setenv("HF_CACHE_ROOT", str(hf_cache_root))
    monkeypatch.setenv("SENAI_WORKER_STATE", str(state_path))
    monkeypatch.setenv("WORKFLOW_MODEL_PATHS", str(paths_path))
    monkeypatch.setenv("AWS_BUCKET_NAME", aws_bucket)
    return workflow_models.run_verify()


def boot_from_state(state_path, timeline_path, fake, *, ready_timeout_sec=5):
    state = json.loads(state_path.read_text())
    fake.object_info = {ct: {} for ct in state.get("allowed_class_types", [])}
    timeline_path.write_text(f"start {time.time()}\n")
    client = comfy_client.ComfyClient(fake.base_url)
    boot_state = senai_worker.boot(
        state_path=state_path, timeline_path=timeline_path, comfy=client, ready_timeout_sec=ready_timeout_sec
    )
    return boot_state, client


def build_job_input(graph, *, trace_overrides=None, inputs=None, deadline_at=None):
    trace = {
        "generation_id": "int-gen-1", "attempt": 1, "binding_alias": "b", "binding_revision": 1,
        "adapter": "a", "workflow_id": "w", "graph_sha256": "a" * 64,
    }
    if trace_overrides:
        trace.update(trace_overrides)
    return {
        "protocol": PROTOCOL, "workflow": graph, "inputs": inputs or [],
        "trace": trace, "limits": {"deadline_at": deadline_at or future_deadline()},
    }


@pytest.fixture
def worker_paths(tmp_path, monkeypatch):
    input_dir = tmp_path / "comfy-input"
    output_dir = tmp_path / "comfy-output"
    output_dir.mkdir()
    monkeypatch.setenv("COMFY_INPUT_DIR", str(input_dir))
    monkeypatch.setenv("COMFY_OUTPUT_DIR", str(output_dir))
    fake_s3 = make_fake_s3()
    monkeypatch.setattr(media_output, "make_s3_client", lambda: (fake_s3, "test-bucket"))
    return {"input_dir": input_dir, "output_dir": output_dir, "s3": fake_s3}


@pytest.fixture
def ltx_ready(tmp_path, monkeypatch):
    plan = workflow_models.load_manifests("ltx25.yaml", WORKFLOW_DIR, tmp_path / "models").plan
    hf_cache_root = tmp_path / "hf-cache"
    seed_hf_cache(hf_cache_root, plan)
    state_path = tmp_path / "state.json"
    state = run_verify(
        monkeypatch, workflows="ltx25.yaml", model_root=tmp_path / "models",
        hf_cache_root=hf_cache_root, state_path=state_path, paths_path=tmp_path / "paths.yaml",
    )
    assert state["ready"] is True, state

    fake = FakeComfy()
    fake.start()
    boot_state, client = boot_from_state(state_path, tmp_path / "timeline", fake)
    assert boot_state.ready is True, boot_state.unready_message
    monkeypatch.setattr(handler, "_BOOT_STATE", boot_state)
    monkeypatch.setattr(handler, "COMFY_CLIENT", client)
    yield {"state": state, "boot_state": boot_state, "fake": fake, "client": client}
    fake.stop()


@pytest.fixture
def minimax_ready(tmp_path, monkeypatch):
    plan = workflow_models.load_manifests("minimax-h3.yaml", WORKFLOW_DIR, tmp_path / "models").plan
    hf_cache_root = tmp_path / "hf-cache"
    seed_hf_cache(hf_cache_root, plan)
    state_path = tmp_path / "state.json"
    state = run_verify(
        monkeypatch, workflows="minimax-h3.yaml", model_root=tmp_path / "models",
        hf_cache_root=hf_cache_root, state_path=state_path, paths_path=tmp_path / "paths.yaml",
    )
    assert state["ready"] is True, state

    fake = FakeComfy()
    fake.start()
    boot_state, client = boot_from_state(state_path, tmp_path / "timeline", fake)
    assert boot_state.ready is True, boot_state.unready_message
    monkeypatch.setattr(handler, "_BOOT_STATE", boot_state)
    monkeypatch.setattr(handler, "COMFY_CLIENT", client)
    yield {"state": state, "boot_state": boot_state, "fake": fake, "client": client}
    fake.stop()


# ---------------------------------------------------------------------------
# Local HTTPS fixture for url-input scenarios (guard.py requires https:// URLs
# and uses a plain requests.Session(), so a plain http:// loopback server
# can't be used here without editing guard.py; a self-signed cert trusted via
# REQUESTS_CA_BUNDLE — an env var requests already honors — keeps the fetch
# path fully real instead of mocking it away).
# ---------------------------------------------------------------------------

def generate_self_signed_cert(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=5))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    key_path = tmp_path / "key.pem"
    cert_path = tmp_path / "cert.pem"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


class _CountingHandler(http.server.SimpleHTTPRequestHandler):
    counter = None

    def do_GET(self):
        self.counter[0] += 1
        super().do_GET()

    def log_message(self, *args):
        pass  # keep test output quiet


class LocalHttpsServer:
    def __init__(self, directory, cert_path, key_path):
        counter = [0]
        handler_cls = functools.partial(
            type("CountingHandler", (_CountingHandler,), {"counter": counter}), directory=str(directory)
        )
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self._counter = counter
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"https://127.0.0.1:{self.port}"

    @property
    def request_count(self):
        return self._counter[0]

    def stop(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()


@pytest.fixture
def local_https_asset_server(tmp_path):
    pytest.importorskip("cryptography.x509")
    cert_path, key_path = generate_self_signed_cert(tmp_path)
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    (asset_dir / "ref.png").write_bytes(TINY_PNG)
    server = LocalHttpsServer(asset_dir, cert_path, key_path)
    yield server, cert_path
    server.stop()


# ---------------------------------------------------------------------------
# Scenario 1
# ---------------------------------------------------------------------------
def test_scenario1_ltx_t2v_end_to_end(ltx_ready, worker_paths):
    if not ffmpeg_available():
        pytest.skip("ffmpeg not available")

    state, boot_state, fake, client = (
        ltx_ready["state"], ltx_ready["boot_state"], ltx_ready["fake"], ltx_ready["client"]
    )
    print("SCENARIO 1 allowed_class_types:", len(state["allowed_class_types"]), state["allowed_class_types"][:5])
    if not any(item.get("bytes") for item in workflow_models.load_manifests(
        "ltx25.yaml", WORKFLOW_DIR, Path("/nonexistent")
    ).plan.values()):
        print("SCENARIO 1 note: every ltx25.yaml model still has bytes: 0 (unpinned) — "
              "size-mismatch detection cannot be exercised for LTX; fixture files just need to exist.")

    graph = json.loads((WORKFLOW_DIR / "ltx25-t2v-v1.json").read_text())
    mp4_path = worker_paths["output_dir"] / "LTX-2.5_00001_.mp4"
    make_output_mp4(mp4_path)
    fake.history_store["p1"] = {"outputs": {"75": {"videos": [{"filename": mp4_path.name, "subfolder": "", "type": "output"}]}}}
    fake.ws_script = [
        {"message": executing("75")},
        {"message": progress_state({"75": {"value": 1, "max": 1, "state": "running"}})},
        {"message": executing(None)},
    ]

    job_input = build_job_input(graph, trace_overrides={"workflow_id": "ltx25-t2v-v1"})
    result = handler.handler({"id": "int-job-1", "input": job_input})
    validate_response(result)

    assert result["status"] == "success", result
    assert result["trace"]["workflows"] == "ltx25.yaml"
    assert result["trace"]["manifest_sha256"] == state["manifest_sha256"]
    key = result["outputs"][0]["key"]
    assert key == f"renders/int-gen-1/1/00-{mp4_path.name}"
    assert key.startswith("renders/int-gen-1/1/00-")

    print("SCENARIO 1 FULL OUTPUT:\n", json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# Scenario 2
# ---------------------------------------------------------------------------
def test_scenario2_disallowed_node_rejected_before_prompt(ltx_ready):
    graph = json.loads((WORKFLOW_DIR / "ltx25-t2v-v1.json").read_text())
    graph["9999"] = {"class_type": "SaveImageWebsocket", "inputs": {}}
    job_input = build_job_input(graph)

    result = handler.handler({"id": "int-job-2", "input": job_input})
    validate_response(result)
    assert result["status"] == "error"
    assert result["failure"]["code"] == "NODE_NOT_ALLOWED"
    assert not any(call[0] == "prompt" for call in ltx_ready["fake"].calls)


# ---------------------------------------------------------------------------
# Scenario 3
# ---------------------------------------------------------------------------
def test_scenario3_corrupted_model_boot_unready(tmp_path, monkeypatch):
    plan = workflow_models.load_manifests("minimax-h3.yaml", WORKFLOW_DIR, tmp_path / "models").plan
    hf_cache_root = tmp_path / "hf-cache"
    corrupted_path = seed_hf_cache(hf_cache_root, plan, corrupt_first=True)
    assert corrupted_path is not None, "minimax-h3.yaml must have at least one model with bytes > 0 to corrupt"

    state_path = tmp_path / "state.json"
    state = run_verify(
        monkeypatch, workflows="minimax-h3.yaml", model_root=tmp_path / "models",
        hf_cache_root=hf_cache_root, state_path=state_path, paths_path=tmp_path / "paths.yaml",
    )
    assert state["ready"] is False
    assert state["unready_code"] == "MODEL_CACHE_MISSING"
    assert corrupted_path in state["models"]["missing"]

    fake = FakeComfy()
    fake.start()
    try:
        boot_state, client = boot_from_state(state_path, tmp_path / "timeline", fake)
        assert boot_state.ready is False
        assert boot_state.unready_code == "MODEL_CACHE_MISSING"
        monkeypatch.setattr(handler, "_BOOT_STATE", boot_state)
        monkeypatch.setattr(handler, "COMFY_CLIENT", client)

        graph = json.loads((WORKFLOW_DIR / "minimax-h3-r2v-v1.json").read_text())
        job_input = build_job_input(graph)
        start = time.monotonic()
        result = handler.handler({"id": "int-job-3", "input": job_input})
        elapsed = time.monotonic() - start
        validate_response(result)
        assert result["status"] == "error"
        assert result["failure"]["code"] == "MODEL_CACHE_MISSING"
        assert elapsed < 0.1, f"unready short-circuit took {elapsed*1000:.1f}ms"

        health = handler.handler({"id": "int-job-3h", "input": {"protocol": PROTOCOL, "health_check": True}})
        validate_response(health)
        assert health["status"] == "healthy"
        assert health["worker"]["ready"] is False
        assert corrupted_path in health["worker"]["models"]["missing"]
    finally:
        fake.stop()


# ---------------------------------------------------------------------------
# Scenario 4
# ---------------------------------------------------------------------------
def test_scenario4_url_input_fetched_and_rewritten(minimax_ready, worker_paths, local_https_asset_server, monkeypatch):
    server, cert_path = local_https_asset_server
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert_path))
    monkeypatch.setenv("INPUT_ALLOWED_HOSTS", "127.0.0.1")

    fake = minimax_ready["fake"]
    graph = json.loads((WORKFLOW_DIR / "minimax-h3-r2v-v1.json").read_text())
    mp4_path = worker_paths["output_dir"] / "MiniMax_H3_00001_.mp4"
    if ffmpeg_available():
        make_output_mp4(mp4_path)
    else:
        mp4_path.write_bytes(b"\x00" * 32)  # media_output's own ffprobe test covers real probing
    fake.history_store["p1"] = {"outputs": {"92": {"videos": [{"filename": mp4_path.name, "subfolder": "", "type": "output"}]}}}
    fake.ws_script = [
        {"message": executing("92")},
        {"message": progress_state({"92": {"value": 1, "max": 1, "state": "running"}})},
        {"message": executing(None)},
    ]

    input_job_dir = worker_paths["input_dir"] / "senai" / "int-job-4"
    before_listing = sorted(p.name for p in input_job_dir.iterdir()) if input_job_dir.exists() else []

    captured = {}
    original_fetch_inputs = guard.fetch_inputs

    def spy_fetch_inputs(inputs, dest, **kwargs):
        result = original_fetch_inputs(inputs, dest, **kwargs)
        captured["during"] = sorted(p.name for p in dest.iterdir())
        return result

    monkeypatch.setattr(guard, "fetch_inputs", spy_fetch_inputs)

    job_input = build_job_input(graph, inputs=[
        {"name": "red_superboy_on_city_roof.png", "url": f"{server.base_url}/ref.png",
         "media_type": "image/png", "bytes": len(TINY_PNG)},
    ])

    if not ffmpeg_available():
        monkeypatch.setattr(media_output, "probe_media", lambda path, **kw: {
            "media_type": "video/mp4", "width": 8, "height": 8, "duration_sec": 1.0, "fps": 8.0, "has_audio": False,
        })

    result = handler.handler({"id": "int-job-4", "input": job_input})
    validate_response(result)
    assert result["status"] == "success", result

    received_client_id, received_graph = fake.received_prompts[-1]
    print("SCENARIO 4 LoadImage node ('137') as received by fake_comfy:", json.dumps(received_graph["137"], indent=2))
    assert received_graph["137"]["inputs"]["image"] == "senai/int-job-4/red_superboy_on_city_roof.png"
    # The second LoadImage input (no matching inputs[] entry) is left untouched.
    assert received_graph["139"]["inputs"]["image"] == "mecha_dragon_lightning.png"

    print("SCENARIO 4 input dir before job:", before_listing)
    print("SCENARIO 4 input dir during job (right after fetch_inputs):", captured.get("during"))
    print("SCENARIO 4 input dir exists after job:", input_job_dir.exists())
    assert before_listing == []
    assert captured["during"] == ["red_superboy_on_city_roof.png"]
    assert not input_job_dir.exists()


# ---------------------------------------------------------------------------
# Scenario 5
# ---------------------------------------------------------------------------
def test_scenario5_url_input_host_not_allowlisted(minimax_ready, worker_paths, local_https_asset_server, monkeypatch):
    server, cert_path = local_https_asset_server
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert_path))
    monkeypatch.delenv("INPUT_ALLOWED_HOSTS", raising=False)  # empty default -> every url input rejected

    graph = json.loads((WORKFLOW_DIR / "minimax-h3-r2v-v1.json").read_text())
    job_input = build_job_input(graph, inputs=[
        {"name": "red_superboy_on_city_roof.png", "url": f"{server.base_url}/ref.png",
         "media_type": "image/png", "bytes": len(TINY_PNG)},
    ])

    requests_before = server.request_count
    result = handler.handler({"id": "int-job-5", "input": job_input})
    validate_response(result)
    assert result["status"] == "error"
    assert result["failure"]["code"] == "INPUT_HOST_REJECTED"
    assert server.request_count == requests_before, "guard must reject the host before any request is sent"


# ---------------------------------------------------------------------------
# Scenario 6
# ---------------------------------------------------------------------------
def test_scenario6_model_not_in_manifest(minimax_ready):
    graph = json.loads((WORKFLOW_DIR / "minimax-h3-r2v-v1.json").read_text())
    graph["119"]["inputs"]["vae_name"] = "not_a_real_model.safetensors"
    job_input = build_job_input(graph)

    result = handler.handler({"id": "int-job-6", "input": job_input})
    validate_response(result)
    assert result["status"] == "error"
    assert result["failure"]["code"] == "MODEL_NOT_IN_MANIFEST"
    assert not any(call[0] == "prompt" for call in minimax_ready["fake"].calls)
