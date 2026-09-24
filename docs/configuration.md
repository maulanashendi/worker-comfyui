# Configuration

This document outlines the environment variables available for configuring the `worker-comfyui`.

## General Configuration

| Environment Variable | Description                                                                                                                                                                                                                  | Default |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `REFRESH_WORKER`     | When `true`, the worker pod will stop after each completed job to ensure a clean state for the next job. See the [RunPod documentation](https://docs.runpod.io/docs/handler-additional-controls#refresh-worker) for details. | `true` |
| `SERVE_API_LOCALLY`  | When `true`, enables a local HTTP server simulating the RunPod environment for development and testing. See the [Development Guide](development.md#local-api) for more details.                                              | `false` |
| `COMFY_ORG_API_KEY`  | Comfy.org API key to enable ComfyUI API Nodes. If set, it is sent with each workflow; clients can override per request via `input.api_key_comfy_org`.                                                                        | –       |

## Logging Configuration

| Environment Variable   | Description                                                                                                                                                      | Default |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `COMFY_LOG_LEVEL`      | Controls ComfyUI's internal logging verbosity. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Use `DEBUG` for troubleshooting, `INFO` for production. | `DEBUG` |
| `NETWORK_VOLUME_DEBUG` | Enable detailed network volume diagnostics in worker logs. Useful for debugging model path issues. See [Network Volumes & Model Paths](network-volumes.md).      | `false` |

## Debugging Configuration

| Environment Variable           | Description                                                                                                            | Default |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------- | ------- |
| `WEBSOCKET_RECONNECT_ATTEMPTS` | Number of websocket reconnection attempts when connection drops during job execution.                                  | `5`     |
| `WEBSOCKET_RECONNECT_DELAY_S`  | Delay in seconds between websocket reconnection attempts.                                                              | `3`     |
| `WEBSOCKET_TRACE`              | Enable low-level websocket frame tracing for protocol debugging. Set to `true` only when diagnosing connection issues. | `false` |

## AWS S3 Upload Configuration

Configure these variables **only** if you want the worker to upload generated images directly to an AWS S3 bucket. If these are not set, images will be returned as base64-encoded strings in the API response.

- **Prerequisites:**
  - An AWS S3 bucket in your desired region.
  - An AWS IAM user with programmatic access (Access Key ID and Secret Access Key).
  - Permissions attached to the IAM user allowing `s3:PutObject` (and potentially `s3:PutObjectAcl` if you need specific ACLs) on the target bucket.

| Environment Variable       | Description                                                                                                                             | Example                                                    |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `BUCKET_ENDPOINT_URL`      | The full endpoint URL of your S3 bucket. **Must be set to enable S3 upload.**                                                           | `https://<your-bucket-name>.s3.<aws-region>.amazonaws.com` |
| `BUCKET_ACCESS_KEY_ID`     | Your AWS access key ID associated with the IAM user that has write permissions to the bucket. Required if `BUCKET_ENDPOINT_URL` is set. | `AKIAIOSFODNN7EXAMPLE`                                     |
| `BUCKET_SECRET_ACCESS_KEY` | Your AWS secret access key associated with the IAM user. Required if `BUCKET_ENDPOINT_URL` is set.                                      | `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY`                 |

**Note:** Upload uses the `runpod` Python library helper `rp_upload.upload_image`, which handles creating a unique path within the bucket based on the `job_id`.

### Example S3 Response

If the S3 environment variables (`BUCKET_ENDPOINT_URL`, `BUCKET_ACCESS_KEY_ID`, `BUCKET_SECRET_ACCESS_KEY`) are correctly configured, a successful job response will look similar to this:

```json
{
  "id": "sync-uuid-string",
  "status": "COMPLETED",
  "output": {
    "images": [
      {
        "filename": "ComfyUI_00001_.png",
        "type": "s3_url",
        "data": "https://your-bucket-name.s3.your-region.amazonaws.com/sync-uuid-string/ComfyUI_00001_.png"
      }
      // Additional images generated by the workflow would appear here
    ]
    // The "errors" key might be present here if non-fatal issues occurred
  },
  "delayTime": 123,
  "executionTime": 4567
}
```

The `data` field contains the presigned URL to the uploaded image file in your S3 bucket. The path usually includes the job ID.

## Workflow model bootstrap

See [runtime manifests](customization.md#generic-workflow-selection-and-model-cache) for `WORKFLOWS` (legacy alias `WORKFLOW_MANIFESTS`), `WORKFLOW_DIR`, `COMFY_MODEL_ROOT`, `HF_TOKEN`, `PREPARE_MODELS_ONLY`, `MODEL_DOWNLOAD_POLICY`, `MODEL_DOWNLOAD_CONCURRENCY`, and `MODEL_DOWNLOAD_CHECK_ONLY`.

## Senai worker/1 protocol

Status: draft binding, not yet verified against a real GPU (see
[Known limitations](#known-limitations) below). Machine-checked source of
truth for env names/defaults and error codes is `contract/senai-worker-1/pins.yaml`
in this repo; the request/response JSON Schemas live alongside it in the same
directory. The prose contract (rationale, request/response walkthrough, error
taxonomy) is **not** copied into this repo — it lives at the senai repo's
`contracts/runpod-senai-worker/CONTRACT.md`. This section only summarizes both
for operators of this repo. Internal module boundaries (which file does what
inside the worker) are in
[`docs/senai-worker-internals.md`](senai-worker-internals.md).

### One image, many deployments

The same Docker image serves every Senai deployment. `WORKFLOWS=ltx25.yaml` or
`WORKFLOWS=minimax-h3.yaml` (never both) picks which model set — and therefore
which `class_type` allowlist and graphs — that particular endpoint verifies
and accepts. See [runtime manifests](customization.md#generic-workflow-selection-and-model-cache)
for the manifest format.

### Model weights: Hugging Face cached models on RunPod, not a hand-filled network volume

Production endpoints do **not** use the `PREPARE_MODELS_ONLY` / network-volume
flow described in the [Deployment Guide](deployment.md#generic-worker-prepare-assets-separately-from-serving)
for self-managed caches. Instead, each set pins Hugging Face repos as RunPod's
native "cached model" feature:

| Set (`WORKFLOWS`) | Hugging Face repos |
| --- | --- |
| `ltx25.yaml` | `Lightricks/LTX-2.5` (gated — requires `HF_TOKEN`), `Comfy-Org/gemma-4` |
| `minimax-h3.yaml` | `Comfy-Org/MiniMax-H3`, `Comfy-Org/SDPose` |

RunPod caches the selected repos under the RunPod cached-model mount
`/runpod/model-store/huggingface/<org>/<repo>/<revision>/<file>` (fixed path,
not an env var), with `HF_CACHE_ROOT` (default
`/runpod-volume/huggingface-cache/hub`, laid out as
`models--<org>--<repo>/snapshots/<revision>/<file>`) as a fallback for the
`huggingface_hub` layout. Production always runs
with `MODEL_DOWNLOAD_POLICY=cache-only`: the GPU worker never downloads
weights itself, it only reads what RunPod already cached.

Boot verification (`workflow_models.py --verify`, see internals doc §5) checks
every declared model file with `stat` — existence and byte size against the
manifest — and never re-hashes the content. If anything is missing or the
wrong size, or `AWS_BUCKET_NAME` is unset, the worker does **not** crash-loop:
it boots into an explicit **unready mode** (`ready: false`,
`unready_code` set to `MODEL_CACHE_MISSING` or `OUTPUT_NOT_CONFIGURED`), keeps
ComfyUI and the handler running, and answers every job and `/health` probe
with that code so the failure is visible instead of silently retried.

### Environment variables (senai-worker/1)

Names and defaults below are copied verbatim from
`contract/senai-worker-1/pins.yaml` (`env`) — the machine-checked source of
truth. Do not add new env vars without updating that file first.

| Env | Default | Nilai produksi | Catatan |
| --- | --- | --- | --- |
| `WORKFLOWS` | — (wajib) | `ltx25.yaml` atau `minimax-h3.yaml` | Selektor set, daftar `workflow/<set>.yaml` dipisah koma. |
| `MODEL_DOWNLOAD_POLICY` | — (wajib) | `cache-only` | Worker GPU tidak pernah mengunduh bobot. |
| `PREPARE_MODELS_ONLY` | `false` | `false` | Hanya dipakai di job preparation terpisah, tidak di endpoint serving. |
| `MODEL_DOWNLOAD_CHECK_ONLY` | `false` | `false` | Idem, dipakai untuk mencetak checklist saja. |
| `COMFY_MODEL_ROOT` | `/runpod-volume/models` bila termount, selain itu `/comfyui/models` | (default) | Jalur cadangan model bila tidak ada di mount cached model RunPod maupun `HF_CACHE_ROOT`. |
| `COMFY_LOG_LEVEL` | `INFO` | `INFO` | Default fork sebelumnya adalah `DEBUG`. |
| `AWS_BUCKET_NAME` | — (wajib) | bucket R2 staging | Tanpa ini, boot masuk mode unready `OUTPUT_NOT_CONFIGURED`. |
| `AWS_ACCESS_KEY_ID` | — (wajib) | secret endpoint | Jangan ditulis di dokumen atau log. |
| `AWS_SECRET_ACCESS_KEY` | — (wajib) | secret endpoint | Jangan ditulis di dokumen atau log. |
| `AWS_DEFAULT_REGION` | — (wajib) | `auto` | R2 memakai `auto`. |
| `AWS_ENDPOINT_URL` | — (wajib) | endpoint R2, sama dengan endpoint vavo | Dibaca botocore sendiri; jangan hardcode di kode. |
| `HF_CACHE_ROOT` | `/runpod-volume/huggingface-cache/hub` | (default) | Cadangan layout `huggingface_hub`; lokasi utama adalah mount `/runpod/model-store/huggingface/<org>/<repo>/<revision>/`. |
| `REFRESH_WORKER` | `dirty` | `dirty` | `dirty` \| `always` \| `never`. `dirty` mempensiunkan worker hanya pada error dengan `refresh: true` (tabel §5 kontrak). |
| `LEGACY_UPSTREAM_INPUT` | `false` | `false` | Jalur upstream lama (`input.workflow` + `input.images` tanpa `protocol`). Tidak pernah `true` di endpoint Senai. |
| `INPUT_ALLOWED_HOSTS` | `""` | host bucket aset Senai (dipisah koma) | Kosong berarti setiap `inputs[].url` ditolak `INPUT_HOST_REJECTED`. |
| `INPUT_MAX_BYTES` | `209715200` | (default) | Batas ukuran per input yang di-fetch. |
| `INPUT_INLINE_MAX_BYTES` | `8388608` | (default) | Batas ukuran setelah decode untuk `inputs[].data` base64. |
| `OUTPUT_PREFIX` | `renders` | (default) | Awalan key R2: `{OUTPUT_PREFIX}/{generation_id}/{attempt}/{NN}-{filename}`. |
| `OUTPUT_PRESIGN_TTL_SEC` | `86400` | (default) | Masa berlaku URL presigned. |
| `JOB_DEADLINE_CEILING_SEC` | `1800` | (default) | Batas atas deadline efektif, terlepas dari `limits.deadline_at` request. |
| `NO_PROGRESS_SEC` | `120` | (default) | Default `limits.no_progress_sec` bila tidak dikirim request. |
| `NO_PROGRESS_LOAD_SEC` | `600` | (default) | Jendela tanpa-progres saat node loader (`*Loader`, `CLIPTextEncode`, dst.) sedang jalan. |
| `COMFY_READY_TIMEOUT_SEC` | `300` | (default) | Batas tunggu `/system_stats` saat boot sebelum unready `COMFYUI_UNREACHABLE`. |
| `MODEL_DOWNLOAD_BUDGET_SEC` | `1800` | (default) | Hanya relevan saat `MODEL_DOWNLOAD_POLICY=missing`, tidak dipakai di produksi `cache-only`. |
| `MODEL_LOCK_TIMEOUT_SEC` | `600` | (default) | Timeout lock antar-worker saat berbagi volume model. |
| `WARMUP` | `false` | sesuai kebutuhan | `true` menjalankan `warmup_graph` manifest (bila ada) sebelum menandai worker siap. |

### Protocol summary

Requests set `input.protocol: "senai-worker/1"`. A job carries the ComfyUI API
graph (`input.workflow`), optional external/inline media (`input.inputs[]`),
an echoed `input.trace` object, and `input.limits` (deadline and no-progress
windows). A minimal health probe (`{"input": {"protocol": "senai-worker/1",
"health_check": true}}`) does no GPU work and returns boot/readiness state.

```json
// request (abridged)
{"input": {"protocol": "senai-worker/1",
           "workflow": {"75": {"class_type": "SaveVideo", "inputs": {}}},
           "trace": {"generation_id": "…", "attempt": 1},
           "limits": {"deadline_at": "2026-09-23T10:00:00Z"}}}
```

```json
// response.output on success (abridged)
{"status": "success", "protocol": "senai-worker/1",
 "outputs": [{"url": "https://…", "media_type": "video/mp4", "width": 1280,
              "height": 704, "duration_sec": 5.04, "fps": 24.0, "has_audio": true}],
 "trace": {"…": "…"}, "timings": {"…": "…"}}
```

On error, the handler returns `status: "error"` with the error object under
**`output.failure`** — never under `output.error`. This is deliberate: the
RunPod SDK's `run_job` pops the `error` and `refresh_worker` keys off whatever
the handler returns (`error` becomes the outer `FAILED` status and the
structured detail is lost; `refresh_worker` becomes `stopPod`). Those two keys
are therefore reserved for the SDK and must never be used to carry protocol
data — see the senai repo's `contracts/runpod-senai-worker/CONTRACT.md` §4 for
the full rationale, and `contract/senai-worker-1/response.schema.json` in
this repo for the exact shape of `outputs[]` and `failure`.

The **legacy upstream path** (`input.workflow` + `input.images`, no
`protocol` key, described elsewhere in this guide and in the README's
Quickstart) only runs when `LEGACY_UPSTREAM_INPUT=true`. It is never enabled
on an endpoint that serves Senai; any request without `input.protocol` on a
Senai endpoint is rejected with `UNSUPPORTED_PROTOCOL`.

### Running the test suite locally

```bash
cd runpod/worker-comfyui
uv run --quiet --no-project --python 3.12 --with-requirements requirements.txt --with pytest --with jsonschema python -m pytest tests -q -p no:cacheprovider
```

### Known limitations

- H3 reference/control videos are probed and treated as a fixed 24 fps; there
  is no frame-rate normalization yet.
- Cached models store the entire Hugging Face repo, not just the files a
  manifest declares; download/storage efficiency is measured at gate G4, not
  guaranteed here.
- Nothing in this section has been exercised against a real GPU worker yet;
  contract status stays `draft` until gates G5 and G7 pass (see
  `contracts/runpod-senai-worker/CONTRACT.md` §10).
