# Cloud Run GPU transport (`SENAI_TRANSPORT=cloudrun`)

Documentation only — this describes a deploy that has not been run. Do not
`docker build`/`push`/`gcloud` from this doc without separate, explicit
approval (one approval per call, per project rules).

## What this is

The same `senai-worker/1` handler (`handler.py::handler`) and the same image
(`Dockerfile.ltx25`) run as a Google Cloud Run GPU **service** instead of a
RunPod serverless worker. Only the transport differs:

- RunPod mode (`SENAI_TRANSPORT=runpod`, default): `start.sh` launches
  `handler.py`, which calls `runpod.serverless.start({"handler": handler})`.
- Cloud Run mode (`SENAI_TRANSPORT=cloudrun`): `start.sh` launches
  `src/cloudrun_server.py` instead, which boots the same `senai_worker.boot()`
  state and serves `handler.handler(job)` over a small stdlib HTTP server on
  `$PORT`.

The request/response envelope on `/run` mirrors RunPod's `/run` + `/status`
shape (`{"id", "status", "output"}`) so the rest of the senai-worker/1
contract (`contracts/runpod-senai-worker/CONTRACT.md` §3–§6) — allowlists,
model manifests, error taxonomy, `trace`, `timings` — is unchanged.

## Environment variables (cloudrun mode)

All RunPod-path env vars (`WORKFLOWS`, `MODEL_DOWNLOAD_POLICY`, `HF_CACHE_ROOT`,
`AWS_BUCKET_NAME`/`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_DEFAULT_REGION`/
`AWS_ENDPOINT_URL`, `INPUT_ALLOWED_HOSTS`, `REFRESH_WORKER`, etc. — see
`docs/configuration.md` and contract §7) apply unchanged. Cloud Run mode adds:

| Env | Value | Notes |
| --- | --- | --- |
| `SENAI_TRANSPORT` | `cloudrun` | Switches `start.sh` to launch `cloudrun_server.py`. Default `runpod`. |
| `PORT` | set by Cloud Run | HTTP port `cloudrun_server.py` binds (`0.0.0.0:$PORT`); defaults to `8080` if unset (e.g. local testing). |
| `HF_CACHE_ROOT` | `/models/hub` | Points at the GCS FUSE mount instead of the RunPod network-volume default; see "Model resolution" below. |
| `MODEL_DOWNLOAD_POLICY` | `cache-only` | Same as RunPod — the GPU service never downloads weights. |
| `COMFY_READY_TIMEOUT_SEC` | optional | Same meaning as the RunPod path's boot wait; passed to `senai_worker.boot()`. |

No other new env vars; no new dependencies (`cloudrun_server.py` uses only
`http.server` from the standard library — see PR description for proof
`fastapi`/`uvicorn`/`aiohttp` are not in `requirements.txt`).

## HTTP surface

| Method + path | Behavior |
| --- | --- |
| `POST /run` | Body `{"input": {...}}` — same `input` senai sends RunPod's `/run`. Synchronously calls `handler.handler(job)` with `job = {"id": <X-Request-Id or uuid4>, "input": body["input"]}`. Returns `{"id", "status": "COMPLETED", "output": <handler output>}` HTTP 200 on any handler-level outcome (including `output.status == "error"` — same as RunPod, a 200/`COMPLETED` envelope is not proof of job success; only `output.status` is). An uncaught exception in the server itself (not in `handler()`, which never raises) returns `{"id", "status": "FAILED", "error": "<str>"}` HTTP 500. A second concurrent `/run` while one is in flight returns HTTP 429 (never runs in parallel — same effective guarantee as Cloud Run `--concurrency 1`, enforced in-process too). Malformed JSON or a body missing an object `input` returns HTTP 400. |
| `GET /healthz` | 200 whenever the process is up — liveness probe. |
| `GET /ready` | 200 only once `senai_worker.boot()`'s state says `ready: true` **and** ComfyUI answers `/system_stats`; otherwise 503 with the `unready_code` from boot state (contract §5) — startup probe. |

## Model resolution on Cloud Run (no code change)

Bucket `gs://senfers-models-usc1` is laid out
`hub/models--<org>--<name>/snapshots/<rev>/<file>` and mounts read-only at
`/models`. Setting `HF_CACHE_ROOT=/models/hub` makes the existing resolution
code find it without any code change:

- `workflow_models.resolve_snapshot_dir(hf_cache_root, repo, revision)`
  (`src/workflow_models.py:313-321`) builds
  `hf_cache_root / f'models--{org}--{name}' / 'snapshots' / rev` — with
  `HF_CACHE_ROOT=/models/hub` that's exactly
  `/models/hub/models--<org>--<name>/snapshots/<rev>`, matching the bucket
  layout.
- `workflow_models.find_model_file()` (`src/workflow_models.py:324-335`) tries
  that HF snapshot path first, falling back to `COMFY_MODEL_ROOT`.
- `workflow_models.run_verify()` (`src/workflow_models.py:390-394`) reads
  `HF_CACHE_ROOT` from the environment (default
  `/runpod-volume/huggingface-cache/hub`), so overriding it to `/models/hub`
  is the only change needed; `write_model_paths()` (`:372-387`) then writes
  the matching `extra_model_paths.yaml` section from the same value.

## Deploy command (document only — do not run)

```bash
gcloud run deploy worker-comfyui \
  --image us-central1-docker.pkg.dev/senfers/senfers-workers/worker-comfyui:<tag> \
  --region us-central1 \
  --gpu 1 \
  --gpu-type nvidia-rtx-pro-6000 \
  --cpu 20 \
  --memory 80Gi \
  --concurrency 1 \
  --max-instances 1 \
  --no-cpu-throttling \
  --timeout 3600 \
  --service-account worker-comfyui@senfers.iam.gserviceaccount.com \
  --set-env-vars SENAI_TRANSPORT=cloudrun,MODEL_DOWNLOAD_POLICY=cache-only,HF_CACHE_ROOT=/models/hub,WORKFLOWS=ltx25.yaml \
  --add-volume name=models,type=cloud-storage,bucket=senfers-models-usc1,readonly=true \
  --add-volume-mount volume=models,mount-path=/models \
  --startup-probe httpGet.path=/ready,httpGet.port=8080,initialDelaySeconds=0,periodSeconds=10,failureThreshold=180,timeoutSeconds=5
```

Flags checked against `gcloud run deploy --help` (SDK 580.0.0) and the Cloud Run
GPU / health-check docs on 2026-10-01:

- `--gpu-type nvidia-rtx-pro-6000` (L4 is `nvidia-l4`); GA command group, no `beta`.
- `--[no-]gpu-zonal-redundancy`: zonal redundancy is the default (reserved
  capacity, higher price). Keep it for production; `--no-gpu-zonal-redundancy`
  is the cheaper best-effort option.
- `--add-volume` `type=cloud-storage` keys are `bucket`, `readonly`,
  `mount-options` (GCSFuse flags, `;`-separated).
- `--startup-probe` accepts `httpGet.path`/`httpGet.port` keys. With a GPU,
  `failureThreshold * periodSeconds` may be at most 1800 s (180 * 10 here).
- `--no-cpu-throttling` is required for a background `start.sh` (ComfyUI +
  handler as separate processes) to keep running between requests; without it
  Cloud Run may throttle CPU outside of request handling.

A generous `failureThreshold` on the startup probe is intentional: cold model
load + ComfyUI boot can take minutes (see `h3-viability-validation` notes:
boot budget must fit the product's job-latency ceiling).

## Job mode (`SENAI_TRANSPORT=cloudrun-job`)

Documentation only — this describes a deploy that has not been run. Same
caveats as above: no `docker build`/`push`/`gcloud` from this doc without
separate, explicit approval.

A Cloud Run **service** keeps the GPU instance billed for 5-17 minutes after
the last request (`--no-cpu-throttling` plus Cloud Run's own idle-instance
teardown window), and that tail is not configurable. Job mode trades the
always-listening HTTP server for a **Cloud Run Jobs execution** that boots the
same way (`senai_worker.boot()`, same model staging, same `comfy_args`), then
drains a small file queue on a mounted GCS bucket and exits once the queue has
been empty for `IDLE_EXIT_SEC` — so the idle tail is ours to choose instead of
Cloud Run's.

Each queued request runs through the exact same `handler.handler(job)` as the
`runpod` and `cloudrun` transports; the senai-worker/1 request/response shape
is unchanged.

### Environment variables (job mode)

All env vars from the HTTP `cloudrun` mode above apply unchanged (`HF_CACHE_ROOT`,
`MODEL_DOWNLOAD_POLICY`, `COMFY_READY_TIMEOUT_SEC`, etc.), except there is no
`PORT`/HTTP surface. Job mode adds:

| Env | Default | Notes |
| --- | --- | --- |
| `SENAI_TRANSPORT` | — | Set to `cloudrun-job` to launch `cloudrun_job.py` instead of `cloudrun_server.py`. |
| `QUEUE_DIR` | `/queue` | Root of the file queue; expects a GCS FUSE mount with `pending/`, `claimed/`, `done/`, `heartbeat/` subdirectories (created on boot if missing). |
| `IDLE_EXIT_SEC` | `60` | Exit 0 once `pending/` has been empty for this many seconds, measured since the last job finished (or since boot if none ran). |
| `QUEUE_POLL_SEC` | `2` | Poll interval for new files in `pending/`. |
| `CLOUD_RUN_EXECUTION` | set by Cloud Run | Used (falling back to hostname) to name this execution's `heartbeat/<name>.json` file. |

No new dependencies: `cloudrun_job.py` uses only the Python standard library.

### Queue layout

```
QUEUE_DIR/
  pending/<id>.json    # enqueuer writes {"input": {...}} here (same shape as POST /run)
  claimed/<id>.json    # in-flight: claimed via os.rename(pending/<id>.json, claimed/<id>.json)
  done/<id>.json        # {"id", "status": "COMPLETED"|"FAILED", "output"|"error"}, written atomically
  heartbeat/<exec>.json # {"ts", "state": "booting"|"idle"|"busy", "processed"}, refreshed every ~10s
```

Files are claimed oldest-first (by mtime, then name) with `os.rename`, so two
concurrent executions racing the same file have exactly one winner; the loser
sees `FileNotFoundError` and moves on. A `refresh_worker` result from the
handler finishes writing its `done/` file and then exits the execution
immediately, leaving the rest of the queue for the next execution.

### Deploy and enqueue commands (document only — do not run)

```bash
gcloud run jobs create worker-comfyui-job \
  --image us-central1-docker.pkg.dev/senfers/senfers-workers/worker-comfyui:<tag> \
  --region us-central1 \
  --gpu 1 \
  --gpu-type nvidia-rtx-pro-6000 \
  --no-gpu-zonal-redundancy \
  --cpu 20 \
  --memory 80Gi \
  --task-timeout 3600 \
  --max-retries 0 \
  --network default \
  --subnet default \
  --vpc-egress all-traffic \
  --service-account worker-comfyui@senfers.iam.gserviceaccount.com \
  --set-env-vars SENAI_TRANSPORT=cloudrun-job,MODEL_DOWNLOAD_POLICY=cache-only,HF_CACHE_ROOT=/models/hub,WORKFLOWS=ltx25.yaml \
  --add-volume name=models,type=cloud-storage,bucket=senfers-models-usc1,readonly=true \
  --add-volume-mount volume=models,mount-path=/models \
  --add-volume name=queue,type=cloud-storage,bucket=senfers-jobs-usc1,mount-options=metadata-cache-ttl-secs=0 \
  --add-volume-mount volume=queue,mount-path=/queue

# Enqueue a request:
gcloud storage cp req.json gs://senfers-jobs-usc1/pending/<id>.json

# Trigger an execution to drain the queue:
gcloud run jobs execute worker-comfyui-job --region us-central1
```

The queue volume is mounted read-write (no `readonly=true`), unlike the models
volume, since the execution writes `claimed/`, `done/`, and `heartbeat/`
entries back to the bucket. `metadata-cache-ttl-secs=0` on the queue mount
keeps `pending/` listings fresh across concurrent executions and the
out-of-band `gcloud storage cp` that enqueues work; the read-only models mount
has no such requirement.
