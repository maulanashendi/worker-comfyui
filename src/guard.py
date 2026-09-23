"""WP A2: validate senai-worker/1 requests before GPU work starts."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
import re
from typing import Literal, Sequence
from urllib.parse import urlparse

import requests

from senai_errors import WorkerError

PROTOCOL = "senai-worker/1"

_NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
_MEDIA_TYPE_RE = re.compile(r"^(image|video|audio)/[A-Za-z0-9.+-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

MODEL_SUFFIXES = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin")

_HEALTH_KEYS = {"protocol", "health_check"}
_WORKFLOW_KEYS = {"protocol", "workflow", "inputs", "trace", "limits"}
_TRACE_KEYS = {
    "generation_id",
    "attempt",
    "binding_alias",
    "binding_revision",
    "adapter",
    "workflow_id",
    "graph_sha256",
}
_LIMITS_REQUIRED = {"deadline_at"}
_LIMITS_OPTIONAL = {"no_progress_sec", "no_progress_load_sec"}
_INPUT_ITEM_KEYS = {"name", "media_type", "url", "data", "bytes", "sha256"}
_NODE_ID_RE = re.compile(r"^[0-9A-Za-z:_-]+$")


@dataclass(frozen=True)
class InputSpec:
    name: str
    media_type: str
    url: str | None = None
    data: str | None = None
    bytes: int | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class Envelope:
    kind: Literal["workflow", "health"]
    workflow: dict | None
    inputs: tuple[InputSpec, ...]
    trace: dict | None
    deadline_at: datetime | None
    no_progress_sec: int | None
    no_progress_load_sec: int | None


def _invalid(message: str) -> WorkerError:
    return WorkerError("INVALID_ENVELOPE", message)


def _parse_deadline(value: str) -> datetime:
    if not isinstance(value, str) or not re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$", value
    ):
        raise _invalid("limits.deadline_at must be an RFC 3339 UTC timestamp")
    from datetime import timezone

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _is_editor_format(workflow: dict) -> bool:
    if "nodes" in workflow or "links" in workflow:
        return True
    for node in workflow.values():
        if not isinstance(node, dict) or "class_type" not in node:
            return True
    return False


def _parse_input_item(item: object) -> InputSpec:
    if not isinstance(item, dict):
        raise _invalid("inputs[] entries must be objects")
    unknown = set(item) - _INPUT_ITEM_KEYS
    if unknown:
        raise _invalid(f"inputs[] has unknown keys: {sorted(unknown)}")

    name = item.get("name")
    if not isinstance(name, str) or len(name) > 128 or not _NAME_RE.match(name):
        raise _invalid("inputs[].name is missing or malformed")
    if ".." in name or "/" in name or "\\" in name:
        raise _invalid("inputs[].name must not contain a path")

    media_type = item.get("media_type")
    if not isinstance(media_type, str) or not _MEDIA_TYPE_RE.match(media_type):
        raise _invalid("inputs[].media_type is missing or malformed")

    url = item.get("url")
    data = item.get("data")
    if (url is None) == (data is None):
        raise _invalid("inputs[] requires exactly one of url or data")
    if url is not None:
        if not isinstance(url, str) or not url.startswith("https://"):
            raise _invalid("inputs[].url must be an https:// URL")
    if data is not None:
        if not isinstance(data, str) or not data:
            raise _invalid("inputs[].data must be a nonempty string")
        if not media_type.startswith("image/"):
            raise _invalid("inputs[].data is only allowed for image/* media_type")

    raw_bytes = item.get("bytes")
    if raw_bytes is not None and (not isinstance(raw_bytes, int) or isinstance(raw_bytes, bool) or raw_bytes < 1):
        raise _invalid("inputs[].bytes must be a positive integer")

    sha256 = item.get("sha256")
    if sha256 is not None and (not isinstance(sha256, str) or not _SHA256_RE.match(sha256)):
        raise _invalid("inputs[].sha256 must be a 64-char lowercase hex string")

    return InputSpec(
        name=name,
        media_type=media_type,
        url=url,
        data=data,
        bytes=raw_bytes,
        sha256=sha256,
    )


def parse_envelope(job_input: dict, *, now: datetime) -> Envelope:
    if not isinstance(job_input, dict):
        raise _invalid("input must be an object")

    protocol = job_input.get("protocol")
    if protocol != PROTOCOL:
        raise WorkerError("UNSUPPORTED_PROTOCOL", f"unsupported protocol: {protocol!r}")

    if job_input.get("health_check") is True:
        unknown = set(job_input) - _HEALTH_KEYS
        if unknown:
            raise _invalid(f"health input has unknown keys: {sorted(unknown)}")
        return Envelope(
            kind="health",
            workflow=None,
            inputs=(),
            trace=None,
            deadline_at=None,
            no_progress_sec=None,
            no_progress_load_sec=None,
        )

    unknown = set(job_input) - _WORKFLOW_KEYS
    if unknown:
        raise _invalid(f"input has unknown keys: {sorted(unknown)}")

    workflow = job_input.get("workflow")
    if not isinstance(workflow, dict) or not workflow:
        raise _invalid("workflow is missing or empty")
    if _is_editor_format(workflow):
        raise WorkerError("EDITOR_FORMAT", "workflow must be API-format graph, not editor format")
    for node_id, node in workflow.items():
        if not _NODE_ID_RE.match(node_id):
            raise _invalid(f"workflow node id {node_id!r} has invalid characters")
        if not isinstance(node, dict) or "class_type" not in node or "inputs" not in node:
            raise _invalid(f"workflow node {node_id!r} must have class_type and inputs")
        if not isinstance(node["class_type"], str) or not node["class_type"]:
            raise _invalid(f"workflow node {node_id!r}.class_type must be a nonempty string")
        if not isinstance(node["inputs"], dict):
            raise _invalid(f"workflow node {node_id!r}.inputs must be an object")
        node_unknown = set(node) - {"class_type", "inputs", "_meta"}
        if node_unknown:
            raise _invalid(f"workflow node {node_id!r} has unknown keys: {sorted(node_unknown)}")

    raw_inputs = job_input.get("inputs", [])
    if not isinstance(raw_inputs, list):
        raise _invalid("inputs must be an array")
    inputs = tuple(_parse_input_item(item) for item in raw_inputs)

    trace = job_input.get("trace")
    if not isinstance(trace, dict):
        raise _invalid("trace is missing")
    missing_trace = _TRACE_KEYS - set(trace)
    unknown_trace = set(trace) - _TRACE_KEYS
    if missing_trace or unknown_trace:
        raise _invalid(f"trace is incomplete: missing={sorted(missing_trace)} unknown={sorted(unknown_trace)}")

    limits = job_input.get("limits")
    if not isinstance(limits, dict):
        raise _invalid("limits is missing")
    missing_limits = _LIMITS_REQUIRED - set(limits)
    unknown_limits = set(limits) - _LIMITS_REQUIRED - _LIMITS_OPTIONAL
    if missing_limits or unknown_limits:
        raise _invalid(f"limits is incomplete: missing={sorted(missing_limits)} unknown={sorted(unknown_limits)}")

    deadline_at = _parse_deadline(limits["deadline_at"])
    if deadline_at <= now:
        raise WorkerError("DEADLINE_PASSED", "limits.deadline_at has already passed")

    no_progress_sec = limits.get("no_progress_sec")
    if no_progress_sec is not None and (not isinstance(no_progress_sec, int) or isinstance(no_progress_sec, bool) or no_progress_sec < 1):
        raise _invalid("limits.no_progress_sec must be a positive integer")

    no_progress_load_sec = limits.get("no_progress_load_sec")
    if no_progress_load_sec is not None and (
        not isinstance(no_progress_load_sec, int) or isinstance(no_progress_load_sec, bool) or no_progress_load_sec < 1
    ):
        raise _invalid("limits.no_progress_load_sec must be a positive integer")

    return Envelope(
        kind="workflow",
        workflow=workflow,
        inputs=inputs,
        trace=trace,
        deadline_at=deadline_at,
        no_progress_sec=no_progress_sec,
        no_progress_load_sec=no_progress_load_sec,
    )


def check_allowlist(workflow: dict, allowed_class_types: frozenset[str]) -> None:
    for node_id in sorted(workflow):
        class_type = workflow[node_id].get("class_type")
        if class_type not in allowed_class_types:
            raise WorkerError(
                "NODE_NOT_ALLOWED",
                f"node {node_id} uses disallowed class_type {class_type!r}",
                node_id=node_id,
                class_type=class_type,
            )


def check_model_references(workflow: dict, declared_model_names: frozenset[str]) -> None:
    missing = set()
    for node in workflow.values():
        for value in node.get("inputs", {}).values():
            if (
                isinstance(value, str)
                and value.endswith(MODEL_SUFFIXES)
                and not any(ch.isspace() for ch in value)
                and value not in declared_model_names
            ):
                missing.add(value)
    if missing:
        raise WorkerError(
            "MODEL_NOT_IN_MANIFEST",
            "model references not declared in manifest: " + ", ".join(sorted(missing)),
        )


def sniff_media_type(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"qt  ",):
            return "video/quicktime"
        return "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"ID3") or head[:2] == b"\xff\xfb":
        return "audio/mpeg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head[:4] == b"OggS":
        return "audio/ogg"
    return None


_FAMILY_COMPATIBLE = {
    frozenset({"video/mp4", "video/quicktime"}),
}


def _media_types_compatible(declared: str, sniffed: str) -> bool:
    if declared == sniffed:
        return True
    declared_family = declared.split("/", 1)[0]
    sniffed_family = sniffed.split("/", 1)[0]
    if declared_family != sniffed_family:
        return False
    pair = frozenset({declared, sniffed})
    return pair in _FAMILY_COMPATIBLE


def _decode_data(data: str) -> bytes:
    if data.startswith("data:"):
        comma = data.find(",")
        if comma == -1:
            raise WorkerError("INPUT_INVALID", "malformed data URI")
        data = data[comma + 1 :]
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WorkerError("INPUT_INVALID", f"malformed base64 data: {exc}") from None


def _write_bytes(dest_path: Path, content: bytes) -> None:
    part_path = dest_path.with_suffix(dest_path.suffix + ".part")
    part_path.write_bytes(content)
    part_path.rename(dest_path)


def _verify_written(dest_path: Path, spec: InputSpec) -> None:
    content = dest_path.read_bytes()
    if spec.bytes is not None and len(content) != spec.bytes:
        dest_path.unlink(missing_ok=True)
        raise WorkerError("INPUT_INVALID", f"input {spec.name} size does not match declared bytes")
    if spec.sha256 is not None:
        digest = hashlib.sha256(content).hexdigest()
        if digest != spec.sha256:
            dest_path.unlink(missing_ok=True)
            raise WorkerError("INPUT_INVALID", f"input {spec.name} sha256 does not match declared value")
    sniffed = sniff_media_type(content[:64])
    if sniffed is None or not _media_types_compatible(spec.media_type, sniffed):
        dest_path.unlink(missing_ok=True)
        raise WorkerError("INPUT_INVALID", f"input {spec.name} content does not match declared media_type")


def _fetch_url(
    spec: InputSpec,
    dest_path: Path,
    *,
    allowed_hosts: frozenset[str],
    max_bytes: int,
    timeout_sec: float,
    session: requests.Session,
) -> None:
    host = urlparse(spec.url).hostname
    if host not in allowed_hosts:
        raise WorkerError("INPUT_HOST_REJECTED", f"input {spec.name} host is not allow-listed")

    part_path = dest_path.with_suffix(dest_path.suffix + ".part")
    try:
        response = session.get(spec.url, stream=True, timeout=timeout_sec, allow_redirects=False)
    except requests.RequestException as exc:
        raise WorkerError("INPUT_FETCH_FAILED", f"input {spec.name} fetch failed: {exc}") from None

    with response:
        if response.is_redirect or 300 <= response.status_code < 400:
            raise WorkerError(
                "INPUT_FETCH_FAILED",
                f"input {spec.name} fetch failed: redirect not allowed",
            )
        if not response.ok:
            raise WorkerError(
                "INPUT_FETCH_FAILED",
                f"input {spec.name} fetch failed with status {response.status_code}",
            )
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    raise WorkerError("INPUT_TOO_LARGE", f"input {spec.name} exceeds max_bytes")
            except ValueError:
                pass

        written = 0
        try:
            with part_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise WorkerError("INPUT_TOO_LARGE", f"input {spec.name} exceeds max_bytes")
                    handle.write(chunk)
        except requests.RequestException as exc:
            part_path.unlink(missing_ok=True)
            raise WorkerError("INPUT_FETCH_FAILED", f"input {spec.name} fetch failed: {exc}") from None
        except WorkerError:
            part_path.unlink(missing_ok=True)
            raise

    part_path.rename(dest_path)


def fetch_inputs(
    inputs: Sequence[InputSpec],
    dest: Path,
    *,
    allowed_hosts: frozenset[str],
    max_bytes: int,
    inline_max_bytes: int,
    timeout_sec: float = 60.0,
    session: requests.Session | None = None,
) -> dict[str, Path]:
    dest.mkdir(parents=True, exist_ok=True)
    session = session or requests.Session()
    result: dict[str, Path] = {}

    for spec in inputs:
        dest_path = dest / spec.name
        if spec.url is not None:
            _fetch_url(
                spec,
                dest_path,
                allowed_hosts=allowed_hosts,
                max_bytes=max_bytes,
                timeout_sec=timeout_sec,
                session=session,
            )
        else:
            content = _decode_data(spec.data)
            if len(content) > inline_max_bytes:
                raise WorkerError("INPUT_TOO_LARGE", f"input {spec.name} inline data exceeds inline_max_bytes")
            _write_bytes(dest_path, content)

        _verify_written(dest_path, spec)
        result[spec.name] = dest_path

    return result


def rewrite_input_names(workflow: dict, mapping: dict[str, str]) -> dict:
    import copy

    rewritten = copy.deepcopy(workflow)
    for node in rewritten.values():
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for key, value in list(inputs.items()):
            if isinstance(value, str) and value in mapping:
                inputs[key] = mapping[value]
    return rewritten
