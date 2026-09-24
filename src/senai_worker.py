"""WP A1: senai-worker/1 job orchestration — boot readiness + guarded run_job."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

import requests
import websocket

import guard
import media_output
from senai_errors import ERROR_TABLE, PROTOCOL, WorkerError

try:
    import runpod
except ImportError:  # pragma: no cover - runpod always present in prod image
    runpod = None


LOADER_CLASS_TYPE_EXACT = frozenset({"MiniMaxH3ReferenceToVideo"})
LOADER_CLASS_TYPE_PREFIXES = ("CLIPTextEncode", "TextEncode")

# RunPod cuts the job at policy.executionTimeout with no diagnosis; we must
# raise EXECUTION_DEADLINE with node context before that happens.
PLATFORM_TIMEOUT_MARGIN_SEC = 30


def _is_loader_class_type(class_type: str | None) -> bool:
    if not class_type:
        return False
    if class_type.endswith("Loader"):
        return True
    if class_type in LOADER_CLASS_TYPE_EXACT:
        return True
    return any(class_type.startswith(prefix) for prefix in LOADER_CLASS_TYPE_PREFIXES)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


@dataclass
class BootState:
    protocol: str
    workflows: str
    manifest_sha256: str
    comfyui: str
    ready: bool
    unready_code: str | None
    unready_message: str | None
    models: dict
    declared_model_names: frozenset[str]
    allowed_class_types: frozenset[str]
    custom_nodes: list
    limits: dict
    warmup_graph: dict | None
    image: str
    gpu: str
    cuda: str
    vram_gb: float
    worker_id: str
    booted_at_wall: float
    timeline: dict = field(default_factory=dict)
    served_jobs: int = 0


def _read_timeline(timeline_path: Path) -> dict:
    if not timeline_path.exists():
        return {}
    entries = {}
    for line in timeline_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        stage, _, ts = line.rpartition(" ")
        if not stage:
            continue
        try:
            entries[stage] = float(ts)
        except ValueError:
            continue
    if not entries:
        return {}
    base = entries.get("start", min(entries.values()))
    return {stage: round(ts - base, 3) for stage, ts in entries.items() if stage != "start"}


def _append_timeline(timeline_path: Path, stage: str, ts: float) -> None:
    with timeline_path.open("a") as handle:
        handle.write(f"{stage} {ts}\n")


def _gpu_info(comfy) -> tuple[str, float]:
    try:
        stats = comfy.system_stats()
    except Exception:
        return "unknown", 0.0
    devices = stats.get("devices") or []
    if not devices:
        return "unknown", 0.0
    device = devices[0]
    name = device.get("name") or "unknown"
    vram_total = device.get("vram_total")
    vram_gb = round(vram_total / (1024**3), 1) if isinstance(vram_total, (int, float)) else 0.0
    return name, vram_gb


def boot(*, state_path: Path, timeline_path: Path, comfy, ready_timeout_sec: float) -> BootState:
    # A missing/corrupt state file must never crash boot(): the handler still has to
    # start and answer jobs fast with a clear INTERNAL, instead of the process dying
    # and jobs sitting IN_QUEUE until ttl.
    state_read_error = None
    try:
        raw = json.loads(state_path.read_text())
    except (OSError, ValueError) as exc:
        state_read_error = exc
        raw = {}

    ready = bool(raw.get("ready", False))
    unready_code = raw.get("unready_code")
    unready_message = raw.get("unready_message")
    if state_read_error is not None:
        ready = False
        unready_code = "INTERNAL"
        unready_message = f"failed to read boot state {state_path}: {state_read_error}"

    deadline = time.monotonic() + ready_timeout_sec
    reachable = False
    while time.monotonic() < deadline:
        try:
            comfy.system_stats()
            reachable = True
            break
        except Exception:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    allowed_class_types = frozenset(raw.get("allowed_class_types", []))

    if not reachable:
        # A broken state file is the root cause here, not connectivity — keep INTERNAL.
        if state_read_error is None:
            ready = False
            unready_code = "COMFYUI_UNREACHABLE"
            unready_message = f"ComfyUI HTTP not reachable within {ready_timeout_sec}s"
    elif ready:
        try:
            info = comfy.object_info()
        except Exception as exc:
            ready = False
            unready_code = "COMFYUI_UNREACHABLE"
            unready_message = f"failed to fetch /object_info: {exc}"
        else:
            missing = allowed_class_types - set(info.keys())
            if missing:
                ready = False
                unready_code = "COMFYUI_UNREACHABLE"
                unready_message = "missing class types: " + ", ".join(sorted(missing))

    now_epoch = time.time()
    _append_timeline(timeline_path, "ready", now_epoch)

    warmup_graph = raw.get("warmup_graph")
    if ready and warmup_graph and os.environ.get("WARMUP", "false").lower() == "true":
        _append_timeline(timeline_path, "warmup", time.time())
        try:
            client_id = "warmup"
            queued = comfy.queue_prompt(warmup_graph, client_id=client_id)
            prompt_id = queued.get("prompt_id")
            if prompt_id:
                warmup_deadline = time.monotonic() + ready_timeout_sec
                while time.monotonic() < warmup_deadline:
                    history = comfy.history(prompt_id)
                    if prompt_id in history:
                        break
                    time.sleep(0.2)
        except Exception:
            pass  # best-effort warmup; never blocks boot

    _append_timeline(timeline_path, "serverless_start", time.time())

    gpu, vram_gb = _gpu_info(comfy) if reachable else ("unknown", 0.0)

    return BootState(
        protocol=raw.get("protocol", PROTOCOL),
        workflows=raw.get("workflows", ""),
        # Contract 0.2.1 §4.3: manifest_sha256 is always filled, even unready — fall
        # back to sha256("") when the state file didn't have one (missing/corrupt
        # file, or workflow_models.run_verify's own early-exception branch, which
        # writes manifest_sha256: null).
        manifest_sha256=raw.get("manifest_sha256") or hashlib.sha256(b"").hexdigest(),
        comfyui=raw.get("comfyui", ""),
        ready=ready,
        unready_code=unready_code,
        unready_message=unready_message,
        models=raw.get("models") or {"declared": 0, "present": 0, "missing": [], "bytes_manifest": 0, "bytes_visible": 0},
        declared_model_names=frozenset(raw.get("declared_model_names", [])),
        allowed_class_types=allowed_class_types,
        custom_nodes=raw.get("custom_nodes", []),
        limits=raw.get("limits", {}),
        warmup_graph=warmup_graph,
        image=os.environ.get("SENAI_IMAGE_REF") or "unknown",
        gpu=gpu,
        cuda=os.environ.get("CUDA_VERSION") or "unknown",
        vram_gb=vram_gb,
        worker_id=os.environ.get("RUNPOD_POD_ID", ""),
        booted_at_wall=now_epoch,
        timeline=_read_timeline(timeline_path),
    )


def _health_output(boot_state: BootState) -> dict:
    return {
        "status": "healthy",
        "protocol": PROTOCOL,
        "worker": {
            "image": boot_state.image,
            "comfyui": boot_state.comfyui,
            "workflows": boot_state.workflows,
            "manifest_sha256": boot_state.manifest_sha256,
            "gpu": boot_state.gpu,
            "cuda": boot_state.cuda,
            "vram_gb": boot_state.vram_gb,
            "ready": boot_state.ready,
            "unready_code": boot_state.unready_code,
            "models": boot_state.models,
        },
        "timings": {
            "boot_age_sec": round(time.time() - boot_state.booted_at_wall, 3),
            "boot_timeline": boot_state.timeline,
        },
    }


def _build_trace(trace_echo, boot_state, rp_job_id, prompt_id):
    trace = dict(trace_echo) if trace_echo else {}
    trace["rp_job_id"] = rp_job_id
    if boot_state is not None:
        trace["worker_id"] = boot_state.worker_id
        trace["image"] = boot_state.image
        trace["comfyui"] = boot_state.comfyui
        trace["workflows"] = boot_state.workflows
        trace["manifest_sha256"] = boot_state.manifest_sha256
        if boot_state.gpu:
            trace["gpu"] = boot_state.gpu
        if boot_state.cuda:
            trace["cuda"] = boot_state.cuda
    if prompt_id:
        trace["prompt_id"] = prompt_id
    return trace


def _error_output(exc: WorkerError, *, trace_echo, boot_state, rp_job_id, prompt_id, timings):
    # Contract 0.2.0 §4: the protocol error object lives at output["failure"], never
    # output["error"] — runpod's real rp_job.run_job() does job_output.pop("error", None)
    # on whatever dict handler() returns, which would silently strip the detail and
    # republish it as a top-level `error` (see tests/test_serverless_lifecycle.py).
    output = {
        "status": "error",
        "protocol": PROTOCOL,
        "failure": exc.to_error(),
        "trace": _build_trace(trace_echo, boot_state, rp_job_id, prompt_id),
    }
    if timings:
        output["timings"] = timings
    return output


def _apply_refresh(output: dict) -> dict:
    # refresh_worker is intentionally still set at the top level: the runpod SDK
    # pops it too (job_output.pop("refresh_worker", None)) and turns it into
    # stopPod, which is the whole point of setting it here.
    mode = os.environ.get("REFRESH_WORKER", "dirty")
    if mode == "always":
        output["refresh_worker"] = True
    elif mode == "dirty" and output["status"] == "error":
        code = output["failure"]["code"]
        if ERROR_TABLE[code].refresh:
            output["refresh_worker"] = True
    return output


def _history_outputs(comfy, prompt_id):
    history = comfy.history(prompt_id)
    entry = history.get(prompt_id)
    if entry is None:
        return None
    return entry.get("outputs", {})


def _safe_history_outputs(comfy, prompt_id):
    try:
        return _history_outputs(comfy, prompt_id)
    except Exception:
        return None


def _error_from_execution_error(node_id, class_type, exc_type, exc_message):
    exc_type = exc_type or ""
    exc_message = exc_message or ""
    if "OutOfMemoryError" in exc_type or "CUDA out of memory" in exc_message:
        return WorkerError("CUDA_OOM", exc_message or "CUDA out of memory", node_id=node_id, class_type=class_type)
    return WorkerError("NODE_EXCEPTION", exc_message or "node execution failed", node_id=node_id, class_type=class_type)


def _history_status_error(comfy, prompt_id):
    """Inspect /history's status.status_str for this prompt; return a WorkerError
    (CUDA_OOM/NODE_EXCEPTION) if it reports "error", else None. Covers the case
    where execution failed before our websocket connected, or while it was
    disconnected — the live "execution_error" message is only ever pushed to
    currently-connected clients, so reconciliation must not just look at
    `outputs` (a failed prompt with partial outputs would otherwise read as a
    truncated success, or OUTPUT_EMPTY if nothing rendered yet)."""
    history = comfy.history(prompt_id)
    entry = history.get(prompt_id)
    if not entry:
        return None
    status = entry.get("status") or {}
    if status.get("status_str") != "error":
        return None
    for message in status.get("messages") or []:
        if not (isinstance(message, (list, tuple)) and len(message) == 2):
            continue
        event, data = message
        data = data or {}
        if event == "execution_error":
            return _error_from_execution_error(
                data.get("node_id"), data.get("node_type"), data.get("exception_type"), data.get("exception_message")
            )
        if event == "execution_interrupted":
            return WorkerError("NODE_EXCEPTION", "execution interrupted", node_id=data.get("node_id"))
    return WorkerError("NODE_EXCEPTION", "workflow failed (status_str=error, no execution_error message in history)")


def _safe_history_status_error(comfy, prompt_id):
    try:
        return _history_status_error(comfy, prompt_id)
    except Exception:
        return None


def _reconcile(comfy, prompt_id):
    """/history reconciliation used from every "maybe done" point in _monitor:
    raise if ComfyUI recorded a failure, else return outputs if the prompt is
    present in history, else None (caller keeps waiting / tries to reconnect)."""
    error = _safe_history_status_error(comfy, prompt_id)
    if error is not None:
        raise error
    return _safe_history_outputs(comfy, prompt_id)


def _comfy_process_alive():
    """True unless COMFY_PID_FILE names a PID that is definitely dead. Absent
    file/env or unreadable/unparseable content -> True (can't judge, don't block)."""
    pid_file = os.environ.get("COMFY_PID_FILE")
    if not pid_file:
        return True
    path = Path(pid_file)
    if not path.is_file():
        return True
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _class_type_lookup(workflow: dict) -> dict:
    return {node_id: node.get("class_type") for node_id, node in workflow.items()}


def _send_progress(job, node_id, value, max_value):
    if runpod is None:
        return
    try:
        runpod.serverless.progress_update(job, {"node": node_id, "value": value, "max": max_value})
    except Exception:
        pass  # telemetry only, never fails the job


def _running_node_progress(nodes: dict):
    """Pick the node currently reported `state == "running"` out of a
    progress_state message's `nodes` dict; (None, None, None) if there isn't one."""
    for node_id, state in nodes.items():
        if isinstance(state, dict) and state.get("state") == "running":
            return node_id, state.get("value"), state.get("max")
    return None, None, None


def _monitor(comfy, *, job, ws, prompt_id, client_id, class_types, envelope, boot_state, wall_start, mono_start, timings):
    no_progress_sec = envelope.no_progress_sec or boot_state.limits.get(
        "no_progress_sec", _env_int("NO_PROGRESS_SEC", 120)
    )
    no_progress_load_sec = envelope.no_progress_load_sec or boot_state.limits.get(
        "no_progress_load_sec", _env_int("NO_PROGRESS_LOAD_SEC", 600)
    )
    job_ceiling = _env_int("JOB_DEADLINE_CEILING_SEC", 1800)
    limits_ceiling = boot_state.limits.get("execution_ceiling_sec", job_ceiling)
    deadline_from_wall = (envelope.deadline_at - wall_start).total_seconds()
    ceilings = [deadline_from_wall, job_ceiling, limits_ceiling]
    if envelope.max_execution_sec is not None:
        ceilings.append(max(1, envelope.max_execution_sec - PLATFORM_TIMEOUT_MARGIN_SEC))
    effective_ceiling = min(ceilings)
    deadline_mono = mono_start + effective_ceiling

    # No-progress baseline starts now — right after queue_prompt succeeded, not at
    # job start (which would double-count fetch_inputs time against the window).
    no_progress_baseline_mono = time.monotonic()
    last_progress_mono = no_progress_baseline_mono
    last_reconcile_mono = no_progress_baseline_mono
    current_node = None
    current_class_type = None
    current_value = None
    current_max = None
    last_progress_sent_mono = 0.0
    # Until the first real progress tick, cold GPU loading can happen inside any
    # node (not just ones classified as a "loader"), so the generous load window
    # applies broadly; afterwards the strict window applies except for loader nodes.
    seen_progress = False

    node_sec: dict[str, float] = {}
    node_start_mono: float | None = None

    def _finalize_node_sec(end_mono):
        nonlocal node_start_mono
        if current_node is not None and node_start_mono is not None:
            node_sec[current_node] = node_sec.get(current_node, 0.0) + (end_mono - node_start_mono)
            node_start_mono = None
        if node_sec:
            timings["node_sec"] = {k: round(v, 3) for k, v in node_sec.items()}

    def _location_suffix():
        if not current_node:
            return ""
        loc = f" at node {current_node} ({current_class_type})"
        if current_value is not None and current_max is not None:
            loc += f" {current_value}/{current_max}"
        return loc

    def abort_and_raise(worker_error: WorkerError):
        _finalize_node_sec(time.monotonic())
        try:
            comfy.interrupt()
        except Exception:
            pass
        try:
            comfy.delete_queue()
        except Exception:
            pass
        raise worker_error

    try:
        while True:
            now_mono = time.monotonic()
            if now_mono >= deadline_mono:
                abort_and_raise(
                    WorkerError(
                        "EXECUTION_DEADLINE",
                        f"execution exceeded deadline of {effective_ceiling:.0f}s{_location_suffix()}",
                    )
                )

            if not seen_progress:
                window = no_progress_load_sec
            else:
                window = no_progress_load_sec if _is_loader_class_type(current_class_type) else no_progress_sec
            if now_mono - last_progress_mono >= window:
                where = f" at node {current_node} ({current_class_type})" if current_node else ""
                abort_and_raise(WorkerError("NO_PROGRESS", f"no progress for {window}s{where}"))

            try:
                ws.settimeout(1.0)
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                if now_mono - last_reconcile_mono >= 10:
                    last_reconcile_mono = now_mono
                    outputs = _reconcile(comfy, prompt_id)
                    if outputs is not None:
                        _finalize_node_sec(time.monotonic())
                        return outputs
                continue
            except (websocket.WebSocketConnectionClosedException, OSError, ConnectionError):
                outputs = _reconcile(comfy, prompt_id)
                if outputs is not None:
                    _finalize_node_sec(time.monotonic())
                    return outputs
                try:
                    ws = comfy.ws_connect(client_id)
                    continue
                except Exception:
                    raise WorkerError("COMFYUI_CRASHED", "ComfyUI websocket unreachable after disconnect")

            if not isinstance(raw, str):
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # ws.recv() can block for up to the 1s settimeout; `now_mono` above was
            # captured *before* that wait, so it can already be ~1s stale by the
            # time a message actually arrives — comparable to a short no_progress_sec
            # window and enough to trip NO_PROGRESS spuriously right after real
            # progress came in. Re-stamp "now" right after we know we got something.
            received_mono = time.monotonic()

            mtype = message.get("type")
            data = message.get("data", {}) or {}
            msg_prompt_id = data.get("prompt_id")
            if msg_prompt_id is not None and msg_prompt_id != prompt_id:
                continue

            if mtype == "executing":
                node = data.get("node")
                _finalize_node_sec(received_mono)
                if node is None:
                    current_node = None
                    current_class_type = None
                    current_value = None
                    current_max = None
                    outputs = _reconcile(comfy, prompt_id)
                    if outputs is not None:
                        return outputs
                    return {}
                current_node = str(node)
                current_class_type = class_types.get(current_node)
                current_value = None
                current_max = None
                node_start_mono = received_mono
                last_progress_mono = received_mono
            elif mtype in ("progress", "progress_state"):
                last_progress_mono = received_mono
                if mtype == "progress":
                    progress_node, value, max_value = current_node, data.get("value"), data.get("max")
                else:
                    progress_node, value, max_value = _running_node_progress(data.get("nodes") or {})
                if value is not None:
                    seen_progress = True
                if progress_node == current_node and value is not None:
                    current_value, current_max = value, max_value
                if progress_node is not None and received_mono - last_progress_sent_mono >= 5:
                    last_progress_sent_mono = received_mono
                    _send_progress(job, progress_node, value, max_value)
            elif mtype == "execution_error":
                raise _error_from_execution_error(
                    data.get("node_id"), data.get("node_type"), data.get("exception_type"), data.get("exception_message")
                )
            elif mtype == "execution_interrupted":
                raise WorkerError("NODE_EXCEPTION", "execution interrupted", node_id=data.get("node_id"))
    finally:
        # Every exit from this loop — return, or a raise from any branch above
        # (execution_error, execution_interrupted, COMFYUI_CRASHED, or
        # abort_and_raise's NO_PROGRESS/EXECUTION_DEADLINE) — must close out the
        # node that was running so timings["node_sec"] reflects it. Idempotent:
        # abort_and_raise already calls this before raising, so a second call
        # here (node_start_mono already None) is a no-op.
        _finalize_node_sec(time.monotonic())
        try:
            ws.close()
        except Exception:
            pass


def run_job(job: dict, *, boot_state: BootState, comfy) -> dict:
    job_input = job.get("input") or {}
    rp_job_id = job.get("id", "unknown")
    wall_start = datetime.now(timezone.utc)
    mono_start = time.monotonic()

    trace_echo = None
    prompt_id = None
    input_dir = None
    timings: dict = {}
    cold = None

    try:
        envelope = guard.parse_envelope(job_input, now=wall_start)

        if envelope.kind == "health":
            return _health_output(boot_state)

        # Every workflow job that reaches here counts for cold/warm, regardless of
        # whether it goes on to succeed or fail — served_jobs must not stay 0 (and
        # every later job wrongly reported cold=True) just because this one errored.
        cold = boot_state.served_jobs == 0
        boot_state.served_jobs += 1

        trace_echo = dict(envelope.trace)

        if not boot_state.ready:
            raise WorkerError(boot_state.unready_code or "COMFYUI_UNREACHABLE", boot_state.unready_message or "worker not ready")

        guard.check_allowlist(envelope.workflow, boot_state.allowed_class_types)
        guard.check_model_references(envelope.workflow, boot_state.declared_model_names)

        input_dir = Path(os.environ.get("COMFY_INPUT_DIR", "/comfyui/input")) / "senai" / str(rp_job_id)
        fetch_started = time.monotonic()
        allowed_hosts = frozenset(
            h.strip() for h in os.environ.get("INPUT_ALLOWED_HOSTS", "").split(",") if h.strip()
        )
        max_bytes = _env_int("INPUT_MAX_BYTES", 209715200)
        inline_max_bytes = _env_int("INPUT_INLINE_MAX_BYTES", 8388608)
        guard.fetch_inputs(
            envelope.inputs,
            input_dir,
            allowed_hosts=allowed_hosts,
            max_bytes=max_bytes,
            inline_max_bytes=inline_max_bytes,
        )
        timings["fetch_ms"] = int((time.monotonic() - fetch_started) * 1000)

        mapping = {spec.name: f"senai/{rp_job_id}/{spec.name}" for spec in envelope.inputs}
        graph = guard.rewrite_input_names(envelope.workflow, mapping)
        class_types = _class_type_lookup(envelope.workflow)
        client_id = str(rp_job_id)

        if not _comfy_process_alive():
            raise WorkerError("COMFYUI_CRASHED", "ComfyUI process is not running (COMFY_PID_FILE)")

        # Connect the websocket before submitting the prompt (upstream order): once
        # queue_prompt returns, ComfyUI may already be pushing "executing"/progress
        # events, and connecting after the fact would lose whatever fired first.
        try:
            ws = comfy.ws_connect(client_id)
        except Exception as exc:
            raise WorkerError("COMFYUI_CRASHED", f"failed to open ComfyUI websocket: {exc}")

        queue_started = time.monotonic()
        try:
            queued = comfy.queue_prompt(graph, client_id=client_id)
        except requests.HTTPError as exc:
            try:
                ws.close()
            except Exception:
                pass
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 400:
                node_errors = getattr(exc, "node_errors", None) or {}
                detail = getattr(exc, "error_detail", None)
                message = detail.get("message") if isinstance(detail, dict) else (detail or str(exc))
                raise WorkerError("PROMPT_REJECTED", str(message), node_errors=node_errors)
            raise WorkerError("COMFYUI_CRASHED", f"ComfyUI /prompt returned {status_code}: {exc}")
        except requests.RequestException as exc:
            try:
                ws.close()
            except Exception:
                pass
            raise WorkerError("COMFYUI_CRASHED", f"ComfyUI /prompt unreachable: {exc}")
        timings["comfy_queue_ms"] = int((time.monotonic() - queue_started) * 1000)
        prompt_id = queued.get("prompt_id")

        exec_started = time.monotonic()
        outputs = _monitor(
            comfy,
            job=job,
            ws=ws,
            prompt_id=prompt_id,
            client_id=client_id,
            class_types=class_types,
            envelope=envelope,
            boot_state=boot_state,
            wall_start=wall_start,
            mono_start=mono_start,
            timings=timings,
        )
        timings["execution_ms"] = int((time.monotonic() - exec_started) * 1000)

        if not outputs:
            fetched = _history_outputs(comfy, prompt_id)
            outputs = fetched or {}

        collect_started = time.monotonic()
        s3_client, bucket = media_output.make_s3_client()
        if s3_client is None:
            raise WorkerError("OUTPUT_NOT_CONFIGURED", "AWS_BUCKET_NAME is not configured")
        output_root = Path(os.environ.get("COMFY_OUTPUT_DIR", "/comfyui/output"))

        def resolve_path(filename, subfolder, item_type):
            return output_root / subfolder / filename

        entries = media_output.collect_outputs(
            outputs,
            resolve_path=resolve_path,
            trace=trace_echo,
            rp_job_id=rp_job_id,
            s3_client=s3_client,
            bucket=bucket,
            prefix=os.environ.get("OUTPUT_PREFIX", "renders"),
            presign_ttl_sec=_env_int("OUTPUT_PRESIGN_TTL_SEC", 86400),
        )
        timings["collect_ms"] = int((time.monotonic() - collect_started) * 1000)

        timings["cold"] = cold
        if cold:
            timings["boot_age_sec"] = round(time.time() - boot_state.booted_at_wall, 3)
            timings["boot_timeline"] = boot_state.timeline

        output = {
            "status": "success",
            "protocol": PROTOCOL,
            "outputs": entries,
            "trace": _build_trace(trace_echo, boot_state, rp_job_id, prompt_id),
            "timings": timings,
        }
    except WorkerError as exc:
        timings["execution_ms"] = timings.get("execution_ms", int((time.monotonic() - mono_start) * 1000))
        if cold is not None:
            timings["cold"] = cold
        output = _error_output(
            exc, trace_echo=trace_echo, boot_state=boot_state, rp_job_id=rp_job_id, prompt_id=prompt_id, timings=timings
        )
    except Exception as exc:  # noqa: BLE001 - last-resort guard, handler must never raise
        wrapped = WorkerError("INTERNAL", f"unexpected error: {exc}")
        timings["execution_ms"] = timings.get("execution_ms", int((time.monotonic() - mono_start) * 1000))
        if cold is not None:
            timings["cold"] = cold
        output = _error_output(
            wrapped, trace_echo=trace_echo, boot_state=boot_state, rp_job_id=rp_job_id, prompt_id=prompt_id, timings=timings
        )
    finally:
        if input_dir is not None:
            shutil.rmtree(input_dir, ignore_errors=True)

    return _apply_refresh(output)
