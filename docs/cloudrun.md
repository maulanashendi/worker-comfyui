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

## Boot staging copies only the selected sets

In both `cloudrun` and `cloudrun-job` mode, `start.sh` asks
`workflow_models.py --stage-list` for the HF-cache-relative files the
`WORKFLOWS` manifests reference (each model's `snapshots/<rev>/<file>`, plus
`refs/<rev>` for a branch revision) and copies only those into the in-memory
stage. The bucket can therefore hold more sets than fit in the 80 GiB RAM cap.
If the list can't be produced (unreadable manifest, no listed file found), it
falls back to copying all of `$HF_CACHE_ROOT`, the earlier behaviour.

## LTX 2.5 Ingredients set (`ltx25-ingredients.yaml`)

A reference sheet plus a prompt produces a video that keeps the sheet's
character, product and location. It uses the IC-LoRA
`Lightricks/LTX-2.5-22b-IC-LoRA-Ingredients@12040e40…`
(`ltx-2.5-22b-ic-lora-ingredients-0.9.safetensors`, 1,308,787,472 bytes, at the
repo root) on top of the **int8** transformer and int8 Gemma 12B encoder. The
bf16 transformer is never used. Graph: `workflow/ltx25-ingredients-v1.json`
(single stage, 8 distilled steps, cfg 1, no prompt enhancer, no upscaler, no
`GemmaAPITextEncode`).

Select it with:

- `WORKFLOWS=ltx25-ingredients.yaml`: stages 5 files, 40,022,880,956 bytes
  (int8 transformer, int8 Gemma 12B, video VAE, audio VAE, LoRA).
- `WORKFLOWS=ltx25.yaml,ltx25-ingredients.yaml`: both sets on one worker
  (7 files). A warm worker that alternates between i2v and Ingredients re-patches
  the LoRA on each switch, so a separate service for Ingredients is cheaper.

The LoRA must be in the bucket at the real HF layout:
`hub/models--Lightricks--LTX-2.5-22b-IC-LoRA-Ingredients/snapshots/12040e4091ac2008d3906a594e31a7fb1ab9d546/ltx-2.5-22b-ic-lora-ingredients-0.9.safetensors`.
The manifest files it as `path: loras/…`, and `write_model_paths` registers that
snapshot root as a `loras` search folder, so `LTXICLoRALoaderModelOnly` finds it.

Graph inputs senai sets: `395` LoadImage `reference.png` (the sheet),
`398:376` prompt, `398:372`/`398:360` width/height (default 768×448),
`398:362` duration in seconds (default 5) and `398:361` fps (default 24);
length is `1 + floor(duration*fps/8)*8` (121 frames by default). The sheet is
resized to width×height (stretched, not cropped), so send a width/height with the
sheet's aspect ratio.

Reference-sheet guidance (from the model card):

- One composite image with one clean panel per element (each character as a
  face close-up plus a body turnaround, each prop as a product-style render, one
  clean location panel) on a black background, with no text. Bigger panels carry
  over better; elements that are not on the sheet will not appear.
- Trained at **768×448, 121 frames, 24 fps**. Other sizes and longer clips are
  out of distribution; keep ≥121 frames. Portrait 448×768 is untested.
- Two-part prompt: `Reference sheet: <each panel by position>` then
  `Generated video: <action, shot, dialogue>`. Default negative:
  `worst quality, inconsistent motion, blurry, jittery, distorted`.

## LTX 2.5 motion transfer set (`ltx25-motion.yaml`)

A driving video supplies the motion, a start image supplies the person: the
output is the start-image person doing the driving video's motion ("Match &
Move"). Graph `workflow/ltx25-motion-v1.json`, from the pose branch of the
upstream `LTX-2.5_ICLoRA_Union_Control_Distilled.json`:

`LoadVideo driving.mp4` → `Video Slice` (decode only `length/fps` seconds) →
`GetVideoComponents` → `ImageFromBatch` (at most `length` frames) →
`ResizeImageMaskNode` (width×height, center crop) → `DWPreprocessor`
(body + hands + face, torchscript `yolox_l` + `dw-ll_ucoco_384_bs5`) →
`LTXAddVideoICLoRAGuide`, with `LTXICLoRALoaderModelOnly` loading
`Lightricks/LTX-2.3-22b-IC-LoRA-Union-Control@b4d1c4d8…`
(`ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors`, downscale factor 2;
there is no 2.5 build) onto the **int8** transformer, and `start.png` as the
first frame through `LTXVImgToVideoInplace` (strength 1). One distilled stage,
8 steps, cfg 1, generated audio, `SaveVideo`. No bf16 transformer, no
`GemmaAPITextEncode`, no enhancer, no upscaler, no onnxruntime.

Select it with `WORKFLOWS=ltx25-motion.yaml` (or add it to a list, e.g.
`WORKFLOWS=ltx25.yaml,ltx25-motion.yaml`). Alone it stages 7 files,
39,721,315,609 bytes: int8 transformer, int8 Gemma 12B, video VAE, audio VAE,
the Union Control LoRA, `hr16/DWPose-TorchScript-BatchSize5@359d662a…/dw-ll_ucoco_384_bs5.torchscript.pt`
and `hr16/yolox-onnx@a124b32c…/yolox_l.torchscript.pt`, all at their repo roots
in the bucket's `hub/models--<org>--<name>/snapshots/<rev>/` layout.

### Image requirement

The set needs the `comfyui_controlnet_aux` custom node
(`Fannovel16/comfyui_controlnet_aux@0cd29047…`) in the image. `Dockerfile.ltx25`
still defaults to `CUSTOM_NODE_MANIFESTS="ltx25.yaml"`, so build with
`--build-arg CUSTOM_NODE_MANIFESTS=ltx25.yaml,ltx25-motion.yaml`. The manifest's
`pip:` list replaces the pack's `requirements.txt`, so the build installs only
`opencv-python-headless>=4.7.0.72,<5`, `matplotlib` and `scikit-image` (plus
their dependencies), not `onnxruntime-gpu`, `mediapipe`, `opencv-contrib-python`
or `trimesh`. ComfyUI loads the pack only when a selected set lists it
(`--whitelist-custom-nodes`).

### Annotator checkpoints (no runtime download)

`comfyui_controlnet_aux` looks for `$AUX_ANNOTATOR_CKPTS_PATH/<hf repo>/<file>`
and downloads from Hugging Face when the file is missing. The manifest files
the two annotators under `path: annotators/<file>`; `workflow_models.py --verify`
symlinks each one to `$AUX_ANNOTATOR_CKPTS_PATH/<hf.repo>/<hf.file>`, pointing at
the staged copy (or the mount when staging fails). `start.sh` exports
`AUX_ANNOTATOR_CKPTS_PATH=/tmp/aux-annotator-ckpts` by default, and the pack reads
that variable before its own `config.yaml`. An annotator missing from the cache
makes the worker unready with `MODEL_CACHE_MISSING`, the same as any other model.
The graph must keep the torchscript `bbox_detector`/`pose_estimator` values.
Other values (`.onnx`) are not staged, and the pack would try to download them.

### Inputs senai sends

| Input | Node | Notes |
| --- | --- | --- |
| `driving.mp4`, `media_type: video/mp4` (or `video/quicktime`), via `url` | `396` LoadVideo | Only `url` works: `data` inputs are image-only. Written to `/comfyui/input/senai/<job>/driving.mp4`; the graph's `driving.mp4` is rewritten to that path. |
| `start.png` (image/png, jpeg or webp), `url` or `data` | `395` LoadImage | Target person. It becomes the first frame, so the person's pose should roughly match the driving video's first frame. |
| prompt | `398:376` | Describe the person and the action. Negative is fixed: `worst quality, inconsistent motion, blurry, jittery, distorted`. |
| width, height | `398:372`, `398:360` | Default 448×768 (portrait). Use multiples of 64: the guide is encoded at half size. The driving frames and the start image are center-cropped to this aspect. |
| duration (s), fps | `398:362`, `398:361` | Default 5 s at 24 fps. `length = 1 + floor(duration*fps/8)*8` (121 by default). |

Input hosts: R2 URLs need
`INPUT_ALLOWED_HOSTS=0c65aed74e6e0d8447b486e52bbdcb91.r2.cloudflarestorage.com`
on the service (comma-separate any other hosts). There is no default allowlist:
an empty value rejects every `url` input with `INPUT_HOST_REJECTED`.
`INPUT_MAX_BYTES` (default 209,715,200) caps the driving video download.

### What senai must clamp or prepare

- **Duration ≤ driving video length.** The graph caps the guide at `length`
  frames, so a longer driving video is fine (only its first `length/fps`
  seconds are decoded). A shorter one gives a guide that covers only the start
  of the clip, and the rest of the clip has no motion guidance. senai should set
  `duration ≤ floor(driving_seconds)`.
- **Driving fps = output fps.** Driving frames map 1:1 to output frames. No
  core node resamples fps, so a 30 fps driving video at fps 24 plays the motion
  at 0.8× speed, and the `Video Slice` window decodes fewer driving seconds than
  needed. Transcode the driving video to the output fps first (e.g. `ffmpeg -r 24`),
  or set `398:361` to the driving video's fps.
- **Size.** A short 720p/1080p clip is enough. The frames are resized to
  width×height right after decoding, but decoding happens at source resolution.

### Known limits

- The LoRA is the LTX-2.3 Union Control, run on the 2.5 transformer, the same
  pairing as the upstream 2.5 example. It has not been run on our int8
  weights on a GPU yet.
- One person works best: DWPose draws every detected person, and the guide
  does not say which pose belongs to which person.
- DWPose runs one frame at a time on the GPU before sampling. Expect a few
  seconds per 121 frames on top of the sampling time (not measured yet).
- A warm worker that switches between this set and Ingredients re-patches the
  LoRA on each switch.
