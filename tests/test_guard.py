import base64
import hashlib
import http.server
import json
from pathlib import Path
import socket
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import guard
from senai_errors import WorkerError

from datetime import datetime, timedelta, timezone

EXAMPLES = ROOT / "contract" / "senai-worker-1" / "examples"
NOW = datetime(2026, 9, 23, 9, 0, 0, tzinfo=timezone.utc)

PNG_1x1 = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _load(name):
    return json.loads((EXAMPLES / name).read_text())["input"]


WORKFLOW_EXAMPLES = [
    "ltx25_i2v_inline_image.request.json",
    "h3_r2v_url_inputs.request.json",
]


@pytest.mark.parametrize("name", WORKFLOW_EXAMPLES)
def test_examples_pass_parse_envelope(name):
    envelope = guard.parse_envelope(_load(name), now=NOW)
    assert envelope.kind == "workflow"


def test_health_example_passes_parse_envelope():
    envelope = guard.parse_envelope(_load("health.request.json"), now=NOW)
    assert envelope.kind == "health"


def _base_workflow_input(**overrides):
    job_input = json.loads(json.dumps(_load("ltx25_i2v_inline_image.request.json")))
    job_input.update(overrides)
    return job_input


def test_missing_protocol_rejected():
    job_input = _base_workflow_input()
    del job_input["protocol"]
    with pytest.raises(WorkerError) as exc:
        guard.parse_envelope(job_input, now=NOW)
    assert exc.value.code == "UNSUPPORTED_PROTOCOL"


def test_wrong_protocol_rejected():
    job_input = _base_workflow_input(protocol="senai-worker/2")
    with pytest.raises(WorkerError) as exc:
        guard.parse_envelope(job_input, now=NOW)
    assert exc.value.code == "UNSUPPORTED_PROTOCOL"


def test_editor_format_rejected():
    job_input = _base_workflow_input(workflow={"nodes": [], "links": []})
    with pytest.raises(WorkerError) as exc:
        guard.parse_envelope(job_input, now=NOW)
    assert exc.value.code == "EDITOR_FORMAT"


@pytest.mark.parametrize("key", ["images", "comfy_org_api_key"])
def test_unknown_top_level_key_rejected(key):
    job_input = _base_workflow_input(**{key: "x"})
    with pytest.raises(WorkerError) as exc:
        guard.parse_envelope(job_input, now=NOW)
    assert exc.value.code == "INVALID_ENVELOPE"


def test_url_and_data_together_rejected():
    job_input = _base_workflow_input()
    job_input["inputs"][0]["url"] = "https://acct.r2.cloudflarestorage.com/x.png"
    with pytest.raises(WorkerError) as exc:
        guard.parse_envelope(job_input, now=NOW)
    assert exc.value.code == "INVALID_ENVELOPE"


def test_data_for_video_rejected():
    job_input = _base_workflow_input()
    job_input["inputs"][0]["media_type"] = "video/mp4"
    with pytest.raises(WorkerError) as exc:
        guard.parse_envelope(job_input, now=NOW)
    assert exc.value.code == "INVALID_ENVELOPE"


def test_deadline_passed_rejected():
    job_input = _base_workflow_input()
    job_input["limits"]["deadline_at"] = "2026-09-23T08:59:59Z"
    with pytest.raises(WorkerError) as exc:
        guard.parse_envelope(job_input, now=NOW)
    assert exc.value.code == "DEADLINE_PASSED"


def test_name_with_path_escape_rejected():
    job_input = _base_workflow_input()
    job_input["inputs"][0]["name"] = "../x.png"
    with pytest.raises(WorkerError) as exc:
        guard.parse_envelope(job_input, now=NOW)
    assert exc.value.code == "INVALID_ENVELOPE"


def test_check_allowlist_rejects_disallowed_node():
    workflow = {
        "1": {"class_type": "LoadImage", "inputs": {}},
        "2": {"class_type": "SaveImageWebsocket", "inputs": {}},
    }
    with pytest.raises(WorkerError) as exc:
        guard.check_allowlist(workflow, frozenset({"LoadImage"}))
    assert exc.value.code == "NODE_NOT_ALLOWED"
    assert exc.value.node_id == "2"
    assert exc.value.class_type == "SaveImageWebsocket"


def test_check_allowlist_passes_when_all_allowed():
    workflow = {"1": {"class_type": "LoadImage", "inputs": {}}}
    guard.check_allowlist(workflow, frozenset({"LoadImage"}))


def test_check_model_references_rejects_undeclared_model():
    workflow = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "evil.safetensors"}},
    }
    with pytest.raises(WorkerError) as exc:
        guard.check_model_references(workflow, frozenset({"known.safetensors"}))
    assert exc.value.code == "MODEL_NOT_IN_MANIFEST"


def test_check_model_references_passes_when_declared():
    workflow = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "known.safetensors"}},
    }
    guard.check_model_references(workflow, frozenset({"known.safetensors"}))


def test_check_model_references_ignores_prompt_text_with_whitespace():
    workflow = {
        "1": {
            "class_type": "PrimitiveStringMultiline",
            "inputs": {"value": "a lighthouse at dusk, render as .pt"},
        },
    }
    guard.check_model_references(workflow, frozenset())


def test_check_model_references_still_rejects_no_whitespace_reference():
    workflow = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "evil.safetensors"}},
    }
    with pytest.raises(WorkerError) as exc:
        guard.check_model_references(workflow, frozenset())
    assert exc.value.code == "MODEL_NOT_IN_MANIFEST"


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    payload = b""
    status = 200
    content_type = "application/octet-stream"
    delay_sec = 0.0
    send_content_length = True
    location = None

    def do_GET(self):
        if self.delay_sec:
            time.sleep(self.delay_sec)
        self.send_response(self.status)
        self.send_header("Content-Type", self.content_type)
        if self.location is not None:
            self.send_header("Location", self.location)
        if self.send_content_length:
            self.send_header("Content-Length", str(len(self.payload)))
        else:
            self.close_connection = True
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args):
        pass


def _start_server(
    payload,
    *,
    status=200,
    content_type="application/octet-stream",
    delay_sec=0.0,
    send_content_length=True,
    location=None,
):
    handler_cls = type(
        "Handler",
        (_RangeHandler,),
        {
            "payload": payload,
            "status": status,
            "content_type": content_type,
            "delay_sec": delay_sec,
            "location": location,
            "send_content_length": send_content_length,
        },
    )
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_fetch_inputs_redirect_rejected_target_host_never_contacted(tmp_path):
    other_server, other_thread = _start_server(PNG_1x1, content_type="image/png")
    try:
        other_host, other_port = other_server.server_address
        other_contacted = {"value": False}
        original_do_GET = other_server.RequestHandlerClass.do_GET

        def tracking_do_GET(self):
            other_contacted["value"] = True
            return original_do_GET(self)

        other_server.RequestHandlerClass.do_GET = tracking_do_GET

        redirect_server, redirect_thread = _start_server(
            b"",
            status=302,
            location=f"http://{other_host}:{other_port}/a.png",
        )
        try:
            host, port = redirect_server.server_address
            url = f"http://{host}:{port}/a.png"
            spec = guard.InputSpec(name="a.png", media_type="image/png", url=url)
            with pytest.raises(WorkerError) as exc:
                guard.fetch_inputs(
                    [spec],
                    tmp_path,
                    allowed_hosts=frozenset({host}),
                    max_bytes=1024 * 1024,
                    inline_max_bytes=1024 * 1024,
                )
            assert exc.value.code == "INPUT_FETCH_FAILED"
            assert "redirect" in exc.value.message
        finally:
            redirect_server.shutdown()
            redirect_thread.join()

        assert other_contacted["value"] is False
        assert not any(tmp_path.iterdir())
    finally:
        other_server.shutdown()
        other_thread.join()


def test_fetch_inputs_host_rejected_without_request(tmp_path):
    spec = guard.InputSpec(name="a.png", media_type="image/png", url="https://evil.example.com/a.png")
    with pytest.raises(WorkerError) as exc:
        guard.fetch_inputs(
            [spec],
            tmp_path,
            allowed_hosts=frozenset({"acct.r2.cloudflarestorage.com"}),
            max_bytes=1024,
            inline_max_bytes=1024,
        )
    assert exc.value.code == "INPUT_HOST_REJECTED"
    assert not any(tmp_path.iterdir())


def test_fetch_inputs_too_large_rejected_via_content_length_header(tmp_path):
    payload = b"\x89PNG\r\n\x1a\n" + b"0" * (3 * 1024 * 1024)
    server, thread = _start_server(payload, content_type="image/png")
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/a.png"
        spec = guard.InputSpec(name="a.png", media_type="image/png", url=url)
        max_bytes = 1024 * 1024
        with pytest.raises(WorkerError) as exc:
            guard.fetch_inputs(
                [spec],
                tmp_path,
                allowed_hosts=frozenset({host}),
                max_bytes=max_bytes,
                inline_max_bytes=1024,
            )
        assert exc.value.code == "INPUT_TOO_LARGE"
        assert not any(tmp_path.iterdir())
    finally:
        server.shutdown()
        thread.join()


def test_fetch_inputs_too_large_stops_early_and_removes_part(tmp_path):
    payload = b"\x89PNG\r\n\x1a\n" + b"0" * (3 * 1024 * 1024)
    server, thread = _start_server(payload, content_type="image/png", send_content_length=False)
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/a.png"
        spec = guard.InputSpec(name="a.png", media_type="image/png", url=url)
        read_bytes = {"value": 0}
        import requests

        session = requests.Session()
        original_get = session.get

        def tracking_get(*args, **kwargs):
            response = original_get(*args, **kwargs)
            original_iter = response.iter_content

            def tracking_iter(*a, **kw):
                for chunk in original_iter(*a, **kw):
                    read_bytes["value"] += len(chunk)
                    yield chunk

            response.iter_content = tracking_iter
            return response

        session.get = tracking_get

        max_bytes = 1024 * 1024
        with pytest.raises(WorkerError) as exc:
            guard.fetch_inputs(
                [spec],
                tmp_path,
                allowed_hosts=frozenset({host}),
                max_bytes=max_bytes,
                inline_max_bytes=1024,
                session=session,
            )
        assert exc.value.code == "INPUT_TOO_LARGE"
        assert not any(tmp_path.iterdir())
        assert read_bytes["value"] > max_bytes
        assert read_bytes["value"] < len(payload)
        print(f"bytes read before stop (no Content-Length): {read_bytes['value']}")
    finally:
        server.shutdown()
        thread.join()


def test_fetch_inputs_media_type_mismatch(tmp_path):
    server, thread = _start_server(PNG_1x1, content_type="image/png")
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/a.mp4"
        spec = guard.InputSpec(name="a.mp4", media_type="video/mp4", url=url)
        with pytest.raises(WorkerError) as exc:
            guard.fetch_inputs(
                [spec],
                tmp_path,
                allowed_hosts=frozenset({host}),
                max_bytes=1024 * 1024,
                inline_max_bytes=1024 * 1024,
            )
        assert exc.value.code == "INPUT_INVALID"
    finally:
        server.shutdown()
        thread.join()


def test_fetch_inputs_sha256_mismatch(tmp_path):
    server, thread = _start_server(PNG_1x1, content_type="image/png")
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/a.png"
        spec = guard.InputSpec(
            name="a.png",
            media_type="image/png",
            url=url,
            sha256="0" * 64,
        )
        with pytest.raises(WorkerError) as exc:
            guard.fetch_inputs(
                [spec],
                tmp_path,
                allowed_hosts=frozenset({host}),
                max_bytes=1024 * 1024,
                inline_max_bytes=1024 * 1024,
            )
        assert exc.value.code == "INPUT_INVALID"
    finally:
        server.shutdown()
        thread.join()


def test_fetch_inputs_fetch_failed_on_500(tmp_path):
    server, thread = _start_server(b"error", status=500)
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/a.png"
        spec = guard.InputSpec(name="a.png", media_type="image/png", url=url)
        with pytest.raises(WorkerError) as exc:
            guard.fetch_inputs(
                [spec],
                tmp_path,
                allowed_hosts=frozenset({host}),
                max_bytes=1024 * 1024,
                inline_max_bytes=1024 * 1024,
            )
        assert exc.value.code == "INPUT_FETCH_FAILED"
    finally:
        server.shutdown()
        thread.join()


def test_fetch_inputs_fetch_failed_on_timeout(tmp_path):
    server, thread = _start_server(PNG_1x1, content_type="image/png", delay_sec=1.0)
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/a.png"
        spec = guard.InputSpec(name="a.png", media_type="image/png", url=url)
        with pytest.raises(WorkerError) as exc:
            guard.fetch_inputs(
                [spec],
                tmp_path,
                allowed_hosts=frozenset({host}),
                max_bytes=1024 * 1024,
                inline_max_bytes=1024 * 1024,
                timeout_sec=0.1,
            )
        assert exc.value.code == "INPUT_FETCH_FAILED"
    finally:
        server.shutdown()
        thread.join()


def test_fetch_inputs_success_with_matching_hash(tmp_path):
    digest = hashlib.sha256(PNG_1x1).hexdigest()
    server, thread = _start_server(PNG_1x1, content_type="image/png")
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/a.png"
        spec = guard.InputSpec(
            name="a.png",
            media_type="image/png",
            url=url,
            bytes=len(PNG_1x1),
            sha256=digest,
        )
        result = guard.fetch_inputs(
            [spec],
            tmp_path,
            allowed_hosts=frozenset({host}),
            max_bytes=1024 * 1024,
            inline_max_bytes=1024 * 1024,
        )
        assert result["a.png"] == tmp_path / "a.png"
        assert result["a.png"].read_bytes() == PNG_1x1
    finally:
        server.shutdown()
        thread.join()


def test_fetch_inputs_corrupt_base64_rejected(tmp_path):
    spec = guard.InputSpec(name="a.png", media_type="image/png", data="not-valid-base64!!!")
    with pytest.raises(WorkerError) as exc:
        guard.fetch_inputs(
            [spec],
            tmp_path,
            allowed_hosts=frozenset(),
            max_bytes=1024 * 1024,
            inline_max_bytes=1024 * 1024,
        )
    assert exc.value.code == "INPUT_INVALID"


def test_fetch_inputs_inline_too_large_rejected(tmp_path):
    data_b64 = base64.b64encode(PNG_1x1).decode()
    spec = guard.InputSpec(name="a.png", media_type="image/png", data=f"data:image/png;base64,{data_b64}")
    with pytest.raises(WorkerError) as exc:
        guard.fetch_inputs(
            [spec],
            tmp_path,
            allowed_hosts=frozenset(),
            max_bytes=1024 * 1024,
            inline_max_bytes=4,
        )
    assert exc.value.code == "INPUT_TOO_LARGE"


def test_fetch_inputs_inline_data_uri_success(tmp_path):
    data_b64 = base64.b64encode(PNG_1x1).decode()
    spec = guard.InputSpec(name="a.png", media_type="image/png", data=f"data:image/png;base64,{data_b64}")
    result = guard.fetch_inputs(
        [spec],
        tmp_path,
        allowed_hosts=frozenset(),
        max_bytes=1024 * 1024,
        inline_max_bytes=1024 * 1024,
    )
    assert result["a.png"].read_bytes() == PNG_1x1


def test_rewrite_input_names_only_exact_matches():
    workflow = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "start.png"}},
        "2": {"class_type": "Note", "inputs": {"text": "not start.png exactly but close"}},
    }
    mapping = {"start.png": "senai/job123/start.png"}
    rewritten = guard.rewrite_input_names(workflow, mapping)
    assert rewritten["1"]["inputs"]["image"] == "senai/job123/start.png"
    assert rewritten["2"]["inputs"]["text"] == "not start.png exactly but close"
    assert workflow["1"]["inputs"]["image"] == "start.png"
    assert rewritten is not workflow
    assert rewritten["1"] is not workflow["1"]


@pytest.mark.parametrize(
    "head,expected",
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "image/png"),
        (b"\xff\xd8\xff" + b"\x00" * 8, "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4, "image/webp"),
        (b"GIF89a" + b"\x00" * 8, "image/gif"),
        (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4, "video/mp4"),
        (b"\x00\x00\x00\x18ftypqt  " + b"\x00" * 4, "video/quicktime"),
        (b"\x1a\x45\xdf\xa3" + b"\x00" * 8, "video/webm"),
        (b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 4, "audio/wav"),
        (b"fLaC" + b"\x00" * 8, "audio/flac"),
        (b"OggS" + b"\x00" * 8, "audio/ogg"),
        (b"random garbage", None),
    ],
)
def test_sniff_media_type(head, expected):
    assert guard.sniff_media_type(head) == expected
