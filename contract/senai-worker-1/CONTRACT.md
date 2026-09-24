# Kontrak: RunPod senai-worker (protokol `senai-worker/1`)

Status: **draft mengikat** (lihat urutan otoritas di `contracts/README.md`).
Versi: `0.3.0` — lihat `pins.yaml` dan `CHANGELOG.md`.

Kontrak ini adalah spesifikasi yang dipakai **kedua sisi** untuk implementasi paralel:
worker (`maulanashendi/worker-comfyui`, fork `runpod-workers/worker-comfyui`) dan senai
(adapter RunPod di `backend/src/senai/modules/providers/runpod/`). Rancangan dan alasannya ada
di `docs/design/2026-09-23-runpod-comfyui-pipeline/01-rancangan.md`. Kalau dokumen itu dan
kontrak ini berbeda, kontrak ini yang menang, dan perbedaannya dilaporkan.

Status draft berarti bentuknya sudah mengikat untuk implementasi, tetapi belum dibuktikan di
GPU. Kontrak naik ke `1.0.0` setelah gate G5 dan G7 lulus dengan angka nyata (§10).

## 1. Ruang lingkup & kepemilikan

- **Senai memegang graph.** Senai mengirim graph ComfyUI format API yang dibangun codec-nya.
  Worker tidak menyimpan graph yang dieksekusi. Salinan graph di repo worker hanya dipakai
  untuk menurunkan manifest model dan untuk test, dan harus identik dengan pin di §8.
- **Worker adalah eksekutor generik yang dijaga.** Worker memvalidasi envelope, allowlist
  node, input, dan model, lalu menjalankan graph dengan batas waktu dan melaporkan hasil
  beserta jejaknya.
- **Satu deployment = satu set workflow.** Set dipilih env `WORKFLOWS` (§7). Semua deployment
  memakai image yang sama.
- Kontrak ini tidak menyentuh kontrak `runpod-ltx25` (vavo). Keduanya berlaku berdampingan
  sampai cutover.

## 2. Transport

Transport RunPod sama dengan `contracts/runpod-ltx25/CONTRACT.md` §3: `/run`, `/status/{id}`,
`/cancel/{id}`, `/health`, bearer auth, batas request 10 MB, dan pemetaan error HTTP ke
taksonomi senai. Kontrak ini tidak menduplikasinya. Tambahan yang mengikat:

- Senai **selalu** mengirim `policy.executionTimeout` (ms) dan `policy.ttl` (ms) di body `/run`.
- `/runsync` tidak dipakai.

## 3. Request

Satu-satunya bentuk yang diterima jalur produksi adalah `input.protocol == "senai-worker/1"`.
Request tanpa `protocol` ditolak dengan `UNSUPPORTED_PROTOCOL`. Pengecualiannya hanya bila env
`LEGACY_UPSTREAM_INPUT=true`, yang membuat request diteruskan ke handler upstream
`worker-comfyui` apa adanya. Env itu tidak pernah dipakai di endpoint senai.

### 3.1 Job workflow

```json
{
  "input": {
    "protocol": "senai-worker/1",
    "workflow": {"<node_id>": {"class_type": "…", "inputs": {}}},
    "inputs": [
      {"name": "start.png", "data": "data:image/png;base64,…", "media_type": "image/png"},
      {"name": "ref_video_0.mp4", "url": "https://<akun>.r2.cloudflarestorage.com/…", "media_type": "video/mp4", "bytes": 4100000}
    ],
    "trace": {
      "generation_id": "5b64b095-…", "attempt": 1,
      "binding_alias": "mnx-h3", "binding_revision": 3,
      "adapter": "minimax_h3_senai_worker_v1", "workflow_id": "minimax-h3-r2v-v1",
      "graph_sha256": "…"
    },
    "limits": {"deadline_at": "2026-09-23T10:00:00Z", "no_progress_sec": 120, "no_progress_load_sec": 600}
  },
  "policy": {"executionTimeout": 1200000, "ttl": 1800000}
}
```

| Field | Wajib | Aturan |
| --- | --- | --- |
| `protocol` | Ya | Konstanta `"senai-worker/1"` |
| `workflow` | Ya | Graph format API: objek `node_id → {class_type, inputs}`. Format editor (punya `nodes`/`links`) ditolak `EDITOR_FORMAT`. |
| `inputs[]` | Tidak | Setiap entri punya `name` dan **tepat satu** dari `url` / `data`. |
| `inputs[].name` | Ya | Nama berkas yang sama persis dengan nilai yang dirujuk graph, misalnya `LoadImage.inputs.image`. Hanya `[A-Za-z0-9._-]`, tanpa path. |
| `inputs[].url` | Salah satu | `https`, host harus ada di `INPUT_ALLOWED_HOSTS` worker. Ukuran ≤ `INPUT_MAX_BYTES`. |
| `inputs[].data` | Salah satu | Base64 polos atau data URI. Hanya untuk `media_type` `image/*`, dengan ukuran hasil decode ≤ `INPUT_INLINE_MAX_BYTES`. |
| `inputs[].media_type` | Ya | Dicocokkan dengan sniff konten. Kalau tidak cocok → `INPUT_INVALID`. |
| `inputs[].bytes`, `sha256` | Tidak | Kalau ada, diverifikasi setelah fetch. |
| `trace` | Ya | Semua field wajib. Di-echo balik utuh (§4). |
| `limits.deadline_at` | Ya | RFC 3339 UTC. Deadline efektif = min(`deadline_at`, mulai job + `JOB_DEADLINE_CEILING_SEC`, mulai job + `execution_ceiling_sec` manifest, mulai job + `max_execution_sec` − 30 s). |
| `limits.no_progress_sec` | Tidak | Default dari env `NO_PROGRESS_SEC`. |
| `limits.no_progress_load_sec` | Tidak | Jendela tanpa-progres saat node loader aktif. Default dari env `NO_PROGRESS_LOAD_SEC`. |
| `limits.max_execution_sec` | Tidak | Anggaran eksekusi dihitung dari saat worker mulai menjalankan job. Pengirim mengisinya **sama dengan** `policy.executionTimeout` / 1000. Worker berhenti 30 s sebelumnya (minimal 1 s) dengan `EXECUTION_DEADLINE`, supaya error bertipe beserta node yang sedang jalan terkirim sebelum RunPod memutus job tanpa detail (`FAILED`, `executionTimeout exceeded`). |

Kunci lain di `input` ditolak (`INVALID_ENVELOPE`), termasuk `images`, `comfy_org_api_key`,
dan `api_key_comfy_org`. Env `COMFY_ORG_API_KEY` diabaikan di jalur protokol ini, sehingga
node API berbayar Comfy.org tidak pernah bisa dipakai.

### 3.2 Probe kesehatan

```json
{"input": {"protocol": "senai-worker/1", "health_check": true}}
```

Tidak ada kerja GPU. Worker menjawab dengan bentuk §4.3.

## 4. Response (`output` dari `/status`)

Handler **selalu** mengembalikan objek dengan `status`, dan objek itu menjadi `output`. Semua
kegagalan yang tertangkap dilaporkan sebagai `output.status == "error"` dengan objek detail
di **`output.failure`**, dengan outer `COMPLETED`.

**Kunci `error` dan `refresh_worker` dicadangkan SDK RunPod dan tidak boleh dipakai untuk data
protokol.** `runpod.serverless.modules.rp_job.run_job` menjalankan `job_output.pop("error")`
dan `job_output.pop("refresh_worker")` pada dict yang dikembalikan handler. `error` dipindah ke
`error` top-level `run_result`, sehingga job menjadi `FAILED` dan detail terstruktur hilang
dari `output`. `refresh_worker` diubah menjadi `stopPod`. Ini diverifikasi di kode SDK (1.7.x
dan 1.12.0) dan dengan `run_job` SDK asli di test worker, 2026-09-23. Karena itu objek error
protokol bernama `failure`.

Worker menambahkan `refresh_worker: true` di return handler untuk meminta RunPod memensiunkan
worker. SDK mencabutnya sebelum hasil dikirim, jadi field itu tidak pernah terlihat di
`/status`, dan senai tidak bergantung padanya.

### 4.1 Sukses

```json
{
  "status": "success",
  "protocol": "senai-worker/1",
  "outputs": [
    {"url": "https://<akun>.r2.cloudflarestorage.com/<bucket>/renders/<gen>/1/00-MiniMax_H3_00001_.mp4",
     "bucket": "<bucket>", "key": "renders/<gen>/1/00-MiniMax_H3_00001_.mp4",
     "filename": "MiniMax_H3_00001_.mp4", "node_id": "92",
     "media_type": "video/mp4", "bytes": 5123456, "sha256": "…",
     "width": 1344, "height": 768, "duration_sec": 5.17, "fps": 24.0, "has_audio": true}
  ],
  "trace": {"…echo request trace…": "…",
            "rp_job_id": "…", "worker_id": "<RUNPOD_POD_ID>", "prompt_id": "…",
            "image": "<repo>:<tag>@sha256:…", "comfyui": "v0.36.0",
            "workflows": "minimax-h3.yaml", "manifest_sha256": "…",
            "gpu": "NVIDIA L40S", "cuda": "12.8"},
  "timings": {"cold": true, "boot_age_sec": 41.2, "boot_timeline": {"…": 0.0},
              "fetch_ms": 900, "comfy_queue_ms": 5, "execution_ms": 61234,
              "collect_ms": 300, "upload_ms": 1200,
              "node_sec": {"131": 2.4, "142": 48.1, "92": 3.0}}
}
```

`timings.node_sec` (opsional): detik wall per node yang sempat dieksekusi, diukur dari pesan
websocket `executing` ComfyUI (node mulai → node berikutnya mulai / selesai). Juga dikirim di
output error (§4.2) untuk node yang sudah jalan sampai saat gagal, termasuk node yang sedang
jalan. Node yang di-cache ComfyUI tidak muncul.

- `outputs[]` minimal satu entri. Entri `type: "temp"` dari history ComfyUI tidak pernah
  dilaporkan.
- `media_type` berasal dari **konten** (ffprobe/sniff), bukan ekstensi atau kunci history.
  `width`/`height` wajib untuk `image/*` dan `video/*`. `duration_sec`/`fps`/`has_audio` wajib
  untuk `video/*`. `duration_sec` wajib untuk `audio/*`.
- `url` selalu presigned GET `https`, berlaku `OUTPUT_PRESIGN_TTL_SEC` (default 86400).
  Base64 tidak pernah dikirim.
- Key output deterministik: `{OUTPUT_PREFIX}/{trace.generation_id}/{trace.attempt}/{NN}-{filename}`,
  dengan `NN` = urutan dua digit mulai `00` dan `OUTPUT_PREFIX` default `renders`.
- `timings.boot_timeline` hanya ada di job pertama sebuah worker (`cold: true`). Isinya detik
  sejak proses start per tahap: `gpu_check`, `model_verify`, `comfy_start`, `ready`, `warmup`
  (bila aktif), `serverless_start`.

### 4.2 Error

```json
{
  "status": "error",
  "protocol": "senai-worker/1",
  "failure": {"type": "timeout", "code": "NO_PROGRESS", "stage": "execute",
            "message": "no progress for 120s at node 136 (MiniMaxH3ReferenceToVideo)",
            "infra": true, "retryable": true, "gpu_work": true,
            "node_id": "136", "class_type": "MiniMaxH3ReferenceToVideo"},
  "trace": {"…": "…"},
  "timings": {"…": "…"}
}
```

- `message` ≤ 500 karakter, tanpa URL presigned, token, atau credential.
- `node_errors` (opsional) diteruskan dari respons 400 ComfyUI untuk `PROMPT_REJECTED`, dan
  dipotong ke 20 entri.
- Flag `infra`, `retryable`, dan `gpu_work` **harus** sama dengan tabel §5 untuk `code`
  tersebut. Bila berbeda, senai memakai tabel, bukan flag kiriman, lalu mencatat perbedaannya
  sebagai log peringatan dan `raw.flag_mismatch`. Job tidak digagalkan hanya karena itu.

### 4.3 Sehat

```json
{
  "status": "healthy",
  "protocol": "senai-worker/1",
  "worker": {"image": "…", "comfyui": "v0.36.0", "workflows": "ltx25.yaml",
             "manifest_sha256": "…", "gpu": "NVIDIA L40S", "cuda": "12.8", "vram_gb": 48,
             "ready": true, "unready_code": null,
             "models": {"declared": 6, "present": 6, "missing": [],
                        "bytes_manifest": 0, "bytes_visible": 0}},
  "timings": {"boot_age_sec": 12.3, "boot_timeline": {}}
}
```

`manifest_sha256` selalu diisi, termasuk saat unready. Bila manifest gagal di-parse, isinya
sha256 isi mentah berkas manifest yang dipilih `WORKFLOWS`. `ready: false` berarti worker ada
dalam mode unready. `unready_code` adalah kode §5 yang
akan dikembalikan ke setiap job workflow, dan `models.missing` mencantumkan path yang kurang.

## 5. Taksonomi error

`gpu_work` = apakah GPU sudah bekerja untuk job ini. `infra` = dihitung circuit breaker
binding. `retryable` = boleh diulang otomatis oleh senai (dengan batas di §6.3).
`refresh` = worker memensiunkan dirinya (`refresh_worker: true`).

| `type` | `code` | `stage` | `gpu_work` | `infra` | `retryable` | `refresh` |
| --- | --- | --- | --- | --- | --- | --- |
| `validation_error` | `INVALID_ENVELOPE` | validate | false | false | false | false |
| `validation_error` | `UNSUPPORTED_PROTOCOL` | validate | false | false | false | false |
| `validation_error` | `EDITOR_FORMAT` | validate | false | false | false | false |
| `validation_error` | `NODE_NOT_ALLOWED` | validate | false | false | false | false |
| `validation_error` | `DEADLINE_PASSED` | validate | false | false | false | false |
| `input_error` | `INPUT_HOST_REJECTED` | fetch | false | false | false | false |
| `input_error` | `INPUT_TOO_LARGE` | fetch | false | false | false | false |
| `input_error` | `INPUT_INVALID` | fetch | false | false | false | false |
| `input_error` | `INPUT_FETCH_FAILED` | fetch | false | false | false | false |
| `model_missing` | `MODEL_NOT_IN_MANIFEST` | preflight | false | false | false | false |
| `model_missing` | `MODEL_CACHE_MISSING` | boot | false | true | false | false |
| `prompt_rejected` | `PROMPT_REJECTED` | submit | false | false | false | false |
| `execution_error` | `NODE_EXCEPTION` | execute | true | false | false | false |
| `oom` | `CUDA_OOM` | execute | true | true | true | true |
| `timeout` | `NO_PROGRESS` | execute | true | true | true | true |
| `timeout` | `EXECUTION_DEADLINE` | execute | true | true | false | true |
| `comfyui_down` | `COMFYUI_UNREACHABLE` | boot | false | true | true | true |
| `comfyui_down` | `COMFYUI_CRASHED` | execute | true | true | true | true |
| `output_error` | `OUTPUT_EMPTY` | collect | true | false | false | false |
| `output_error` | `UPLOAD_FAILED` | upload | true | true | true | false |
| `internal_error` | `OUTPUT_NOT_CONFIGURED` | boot | false | true | false | false |
| `internal_error` | `INTERNAL` | internal | true | true | false | true |

Tabel yang sama, dalam bentuk mesin-terbaca, ada di `pins.yaml` (`error_codes`). Test kontrak
memastikan keduanya konsisten.

## 6. Pemetaan di sisi senai

### 6.1 Status

Parser: `backend/src/senai/modules/providers/runpod/codecs/senai_worker_protocol.py`.

| RunPod `status` | `output` | Hasil senai (`ProviderJob`) |
| --- | --- | --- |
| `IN_QUEUE` | — | `queued` |
| `IN_PROGRESS`, `RUNNING` | — (atau progress) | `running` |
| `COMPLETED` | `status == "success"` dan lolos §6.2 | `succeeded`, dengan `context.result_manifest` |
| `COMPLETED` | `status == "error"` + `failure` | `failed`, `error_code = "WORKER_" + failure.code` |
| `COMPLETED` | bentuk lain, `protocol` salah (termasuk pada output error), atau sukses tanpa `trace` | `ProviderContractError` (`PROVIDER_CONTRACT_ERROR`) |
| `FAILED` | apa pun | `failed`, `PROVIDER_FAILED` (infra true, retryable true: worker hilang, atau handler melanggar §4 dengan mengembalikan kunci `error`) |
| `TIMED_OUT` | — | `failed`, `PROVIDER_TIMEOUT` (infra true, retryable false) |
| `CANCELLED` | — | `cancelled` |
| lainnya | — | `ProviderContractError` |

### 6.2 Validasi output sukses

1. `outputs` tidak kosong, dan minimal satu entri cocok dengan kapabilitas: `video/*` untuk
   kapabilitas video.
2. Setiap `url` berskema `https`, dan host-nya ada di `allowed_output_hosts` binding. Kalau
   tidak → `PROVIDER_OUTPUT_HOST_REJECTED`.
3. `trace.generation_id` dan `trace.attempt` sama dengan job senai. Kalau tidak →
   `PROVIDER_CONTRACT_ERROR`.
4. Artefak di `result_manifest` diklasifikasi dari `media_type`: `video/*` → video,
   `image/*` → image, `audio/*` → audio.

### 6.3 Retry otomatis

Hanya kode dengan `retryable: true` (§5, plus `PROVIDER_FAILED`) yang boleh diulang. Batasnya:
- `max_auto_retries` (default 1);
- `retry_gpu_budget_seconds` (jumlah `execution_ms` semua attempt);
- sisa deadline ≥ `execution_timeout_seconds`;
- circuit binding tertutup;
- jeda ≥ 30 detik.

Setiap retry adalah attempt baru dengan `trace.attempt` bertambah. Submit yang ambigu dan
cancel oleh user tidak pernah diulang.

### 6.4 Identitas binding

| `provider_endpoints` | Nilai |
| --- | --- |
| `adapter` | `ltx25_senai_worker_v1` (graph LTX dari `Ltx25Codec`) atau `minimax_h3_senai_worker_v1` |
| `contract_version` | `senai-worker/1` |
| `image_digest` | Digest image yang di-deploy (G3) |

Kunci `config` binding baru (JSONB, tanpa migrasi): `gpu_usd_per_second`,
`input_url_ttl_seconds`, `no_progress_seconds`, `no_progress_load_seconds`,
`max_auto_retries`, `retry_gpu_budget_seconds`. Kunci yang sudah ada tetap berlaku:
`queue_timeout_seconds`, `execution_timeout_seconds`, `ingest_timeout_seconds`,
`max_active_jobs`, `allowed_output_hosts`, `profiles`.

## 7. Environment worker

Daftar mesin-terbaca ada di `pins.yaml` (`env`). Yang wajib di setiap endpoint senai:

| Env | Nilai produksi | Catatan |
| --- | --- | --- |
| `WORKFLOWS` | `ltx25.yaml` atau `minimax-h3.yaml` | Selektor set. Hanya manifest yang disebut yang dibaca. |
| `MODEL_DOWNLOAD_POLICY` | `cache-only` | Worker GPU tidak pernah mengunduh bobot. |
| `HF_CACHE_ROOT` | default `/runpod-volume/huggingface-cache/hub` | Cadangan layout `huggingface_hub` (mis. network volume), dipakai kalau mount cached model RunPod tidak ada. |
| `AWS_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION=auto`, `AWS_ENDPOINT_URL` | R2 staging | Nama sama dengan endpoint vavo. Tanpa bucket, worker masuk mode unready dengan `OUTPUT_NOT_CONFIGURED`, dan tidak pernah mengirim base64. |
| `INPUT_ALLOWED_HOSTS` | host R2 bucket aset senai | Daftar host dipisah koma. Kosong → semua input `url` ditolak. |
| `REFRESH_WORKER` | `dirty` | `dirty` (default baru) \| `always` \| `never` |

Lokasi utama untuk bobot cached model adalah mount RunPod
`/runpod/model-store/huggingface/<org>/<repo>/<revision>/` — path tetap, bukan env var.
`HF_CACHE_ROOT` di atas hanya cadangan kalau mount itu tidak ada.

Manifest set format v2 (`workflow/<set>.yaml` di repo worker):

```yaml
version: 2
set: minimax-h3
requires_comfyui: ">=0.35.0"
custom_nodes: []                  # nama folder custom node yang di-whitelist saat boot
workflows: [minimax-h3-r2v-v1.json, minimax-h3-motion-v1.json]
allowed_class_types_extra: []     # di luar union class_type semua graph di `workflows`
limits: {no_progress_sec: 120, no_progress_load_sec: 600, execution_ceiling_sec: 1800}
warmup_graph: null
models:
  - path: diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors
    hf: {repo: Comfy-Org/MiniMax-H3, revision: "<commit 40-hex>", file: diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors}
    sha256: "<64-hex dari metadata LFS>"
    bytes: 0
```

- **Allowlist `class_type`** = union `class_type` semua graph di `workflows`, ditambah
  `allowed_class_types_extra`.
- **Preflight model**: setiap nilai input string graph yang **tidak mengandung whitespace** dan
  berakhiran `.safetensors|.gguf|.ckpt|.pt|.pth|.bin` harus sama dengan `path` tanpa kategori
  pertama. String dengan whitespace, misalnya prompt yang kebetulan berakhiran `.pt`, bukan
  rujukan model (seperti `load_plan` sekarang) dari model di
  manifest. Kalau tidak → `MODEL_NOT_IN_MANIFEST`.
- **Verifikasi boot**: setiap model harus ada dengan ukuran = `bytes`, dicek berurutan di tiga
  kandidat lokasi: (1) mount cached model RunPod
  `/runpod/model-store/huggingface/<org>/<repo>/<revision>/<file>`, (2)
  `HF_CACHE_ROOT/models--<org>--<repo>/snapshots/<revision>/<file>`, (3) `COMFY_MODEL_ROOT`
  untuk jalur cadangan. Kandidat pertama yang ada di disk dipakai. Kalau tidak ada satu pun →
  mode unready `MODEL_CACHE_MISSING`.

## 8. Pin graph

Graph yang dikirim senai di-pin sha256 kanonik di `pins.yaml` (`workflows`). Algoritmanya sama
dengan kontrak ltx25: `sha256(json.dumps(d, sort_keys=True, separators=(',', ':')).encode())`.
Graph LTX untuk worker ini adalah tiga graph yang sama dengan kontrak ltx25. Graph H3
berstatus `pending` sampai diekspor dari ComfyUI pada versi image (WP3.3), dan tidak boleh
ditulis tangan.

## 9. Batas yang tidak boleh dilanggar

- Graph dari request publik tidak pernah diteruskan. Hanya codec senai yang membangun graph.
- Output base64 tidak pernah diterima. Output diklasifikasi dari `media_type`, bukan dari
  kunci history.
- Credential (R2, HF, RunPod) tidak pernah masuk ke `trace`, `message`, log, atau
  `provider_context`.
- 200 dari `/run` bukan bukti sukses. Hanya `/status` + `output.status == "success"` yang
  lolos §6.2.
- Worker tidak pernah mengulang render sendiri, dan tidak pernah mengunduh bobot di jalur job.
- Endpoint vavo `338flghj3ra4qj` dan template-nya tidak disentuh oleh kontrak ini.

## 10. Yang belum terbukti (menentukan naik ke 1.0.0)

| Klaim | Diverifikasi di |
| --- | --- |
| Di sisi server RunPod, `output` dengan `status: "error"` + `failure` tetap utuh bersama outer `COMPLETED`. Perilaku SDK sudah terbukti lokal (§4), sisi server belum. | G1 (stub CPU) |
| `refresh_worker` benar-benar memensiunkan worker; `progress_update` terlihat di `/status` | G1 |
| `/cancel` pada `IN_PROGRESS` menghentikan handler | G1 |
| Gaya host URL presign R2 cocok dengan `allowed_output_hosts` | G2 |
| Mount cached model RunPod `/runpod/model-store/huggingface/<org>/<repo>/<commit>/` memuat struktur berkas repo apa adanya, dan ukuran = metadata LFS (mountPath sudah terlihat di `model-status` 2026-09-23; isi mount belum) | G4 |
| Cold start, VRAM puncak, durasi per tahap di GPU 48 GB | G5, G7 |
