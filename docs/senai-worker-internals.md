# senai-worker/1 — antarmuka internal worker (mengikat untuk executor)

Status: mengikat untuk implementasi paralel Fase 2, 2026-09-23.

Kontrak kabel (request/response, kode error) ada di `contract/senai-worker-1/`: salinan
`contracts/runpod-senai-worker/` dari repo senai, v0.1.0. Salinan itu **tidak boleh diedit**
di sini. Kalau isinya salah, laporkan ke koordinator. Dokumen ini hanya menetapkan pembagian
modul, nama fungsi, dan bentuk data **di dalam** worker, supaya empat executor bisa bekerja
paralel tanpa saling menebak.

Semua modul baru ada di `src/` dan diimpor datar (`import guard`), sama seperti
`workflow_models` dan `media_output` sekarang (`Dockerfile` menyalin `src/*.py` ke `/`).
Test menambahkan `src/` ke `sys.path` seperti `tests/test_workflow_models.py`.

## 1. Kepemilikan berkas

| WP | Pemilik | Berkas |
| --- | --- | --- |
| A1 | inti protokol | `handler.py` (dispatch saja), `src/senai_worker.py` (baru), `src/senai_errors.py` (baru), `src/comfy_client.py` (baru), `tests/fake_comfy.py` (baru), `tests/test_senai_worker.py` (baru), `tests/test_serverless_lifecycle.py` |
| A2 | guard | `src/guard.py` (baru), `tests/test_guard.py` (baru) |
| A3 | output | `src/media_output.py`, `tests/test_media_output.py` (baru) |
| A4 | boot & model | `src/start.sh`, `src/workflow_models.py`, `Dockerfile`, `docker-bake.hcl`, `.github/workflows/test.yml`, `tests/Dockerfile`, `workflow/**`, `scripts/pin-hf-manifest.py` (baru), `tests/test_workflow_models.py`, `tests/test_pin_hf_manifest.py` (baru) |
| koordinator | — | `contract/**`, `docs/senai-worker-internals.md`, `README.md`, `docs/*.md` lain |

Jangan menyentuh berkas milik WP lain. Kalau butuh perubahan di sana, laporkan.
`tests/test_handler.py` (test upstream) dan fungsi upstream di `handler.py` (`validate_input`,
`upload_images`, dan seterusnya) dipertahankan, karena jalur legacy tetap ada di balik
`LEGACY_UPSTREAM_INPUT=true`.

## 2. `src/senai_errors.py` (A1)

```python
PROTOCOL = "senai-worker/1"

@dataclass(frozen=True)
class ErrorSpec:
    type: str; stage: str; gpu_work: bool; infra: bool; retryable: bool; refresh: bool

ERROR_TABLE: dict[str, ErrorSpec]   # 22 kode, identik dengan contract/senai-worker-1/pins.yaml `error_codes`

class WorkerError(Exception):
    def __init__(self, code: str, message: str, *, node_id: str | None = None,
                 class_type: str | None = None, node_errors: dict | None = None) -> None: ...
    code: str; message: str; node_id: str | None; class_type: str | None; node_errors: dict | None
    @property
    def spec(self) -> ErrorSpec: ...           # KeyError pada kode yang tidak dikenal = bug
    def to_error(self) -> dict: ...            # objek error kontrak §4.2, message lewat scrub().
                                               # Ditaruh di output["failure"], JANGAN output["error"]:
                                               # SDK RunPod pop("error")/pop("refresh_worker") dari
                                               # return handler (kontrak 0.2.0 §4).

def scrub(message: str) -> str: ...  # buang query string URL (?X-Amz-...), header Authorization,
                                     # token hf_...; potong ke 500 karakter
```

Semua modul lain melempar `WorkerError` dengan kode dari tabel. Tidak ada modul yang membuat
dict error sendiri.

## 3. `src/guard.py` (A2)

```python
@dataclass(frozen=True)
class InputSpec:
    name: str; media_type: str
    url: str | None = None; data: str | None = None
    bytes: int | None = None; sha256: str | None = None

@dataclass(frozen=True)
class Envelope:
    kind: Literal["workflow", "health"]
    workflow: dict | None
    inputs: tuple[InputSpec, ...]
    trace: dict | None                   # persis seperti di request
    deadline_at: datetime | None         # timezone-aware UTC
    no_progress_sec: int | None
    no_progress_load_sec: int | None

def parse_envelope(job_input: dict, *, now: datetime) -> Envelope
    # UNSUPPORTED_PROTOCOL: protocol hilang/salah
    # EDITOR_FORMAT: workflow punya "nodes"/"links" atau entri tanpa class_type
    # INVALID_ENVELOPE: kunci tak dikenal, trace/limits tidak lengkap, inputs salah bentuk
    # DEADLINE_PASSED: deadline_at <= now

def check_allowlist(workflow: dict, allowed_class_types: frozenset[str]) -> None
    # NODE_NOT_ALLOWED (node_id, class_type pertama yang ditolak, urutan node_id terurut)

def check_model_references(workflow: dict, declared_model_names: frozenset[str]) -> None
    # nilai input string berakhiran .safetensors/.gguf/.ckpt/.pt/.pth/.bin harus ada di
    # declared_model_names (path manifest tanpa kategori pertama) -> MODEL_NOT_IN_MANIFEST

def fetch_inputs(inputs: Sequence[InputSpec], dest: Path, *, allowed_hosts: frozenset[str],
                 max_bytes: int, inline_max_bytes: int, timeout_sec: float = 60.0,
                 session: requests.Session | None = None) -> dict[str, Path]
    # return {name: path_berkas}; dest dibuat bila perlu
    # INPUT_HOST_REJECTED: host url tidak ada di allowed_hosts (tanpa request keluar)
    # INPUT_TOO_LARGE: Content-Length / byte terbaca > max_bytes, atau data decoded > inline_max_bytes
    # INPUT_INVALID: base64 rusak, sniff != media_type, bytes/sha256 tidak cocok
    # INPUT_FETCH_FAILED: HTTP non-2xx, timeout, error koneksi

def sniff_media_type(head: bytes) -> str | None
    # png, jpeg, webp, gif, mp4/mov (ftyp), webm/mkv (1A45DFA3), wav, mp3, flac, ogg

def rewrite_input_names(workflow: dict, mapping: dict[str, str]) -> dict
    # salinan dalam; setiap nilai string di inputs yang PERSIS sama dengan kunci mapping
    # diganti nilainya (mis. "start.png" -> "senai/<rp_job_id>/start.png"); graph asli tidak diubah
```

## 4. `src/media_output.py` (A3)

Fungsi lama `collect_senai_outputs` **tidak dihapus** oleh A3; jalur `OUTPUT_FORMAT=senai`
dicabut dari handler oleh A1, dan fungsi mati dibersihkan koordinator sesudahnya.

```python
def probe_media(path: Path, *, runner=subprocess.run) -> dict
    # ffprobe -v error -print_format json -show_format -show_streams
    # return {"media_type", "width"?, "height"?, "duration_sec"?, "fps"?, "has_audio"?}
    # media_type dari konten (format_name/codec + sniff), bukan ekstensi

def collect_outputs(history_outputs: dict, *, resolve_path: Callable[[str, str, str], Path],
                    trace: dict, rp_job_id: str, s3_client=None, bucket: str | None = None,
                    prefix: str = "renders", presign_ttl_sec: int = 86400) -> list[dict]
    # history_outputs = history[prompt_id]["outputs"] (node_id -> {images|videos|gifs|audio: [..]})
    # lewati entri type == "temp"; urutan: node_id terurut lalu urutan item
    # key = f"{prefix}/{trace['generation_id']}/{trace['attempt']}/{NN:02d}-{basename}"
    #       (bila trace None: f"{prefix}/{rp_job_id}/0/{NN:02d}-{basename}")
    # upload put_object(ContentType=media_type), lalu generate_presigned_url(ExpiresIn=presign_ttl_sec)
    # return entri kontrak §4.1 (url, bucket, key, filename, node_id, media_type, bytes, sha256, ...)
    # OUTPUT_EMPTY bila tidak ada entri; UPLOAD_FAILED bila upload gagal setelah 3 percobaan
    # (backoff 1s,2s; total <= 60s)

def make_s3_client() -> tuple[object | None, str | None]
    # (boto3.client("s3"), AWS_BUCKET_NAME) bila AWS_BUCKET_NAME ada, selain itu (None, None).
    # endpoint diambil botocore dari AWS_ENDPOINT_URL (R2), jangan di-hardcode.
```

## 5. Boot state (A4 menulis, A1 membaca)

A4 menulis dua berkas saat boot; A1 membacanya saat import handler dan pada tiap job.

**Timeline**: `${SENAI_BOOT_TIMELINE:-/tmp/senai-boot-timeline}`, satu baris per tahap
`<stage> <epoch_seconds_float>`, ditulis `start.sh`. Urutannya: `start`, `gpu_check`,
`model_verify`, `comfy_start` (proses ComfyUI diluncurkan). A1 menambahkan `ready`, `warmup`,
dan `serverless_start` ke berkas yang sama.

**State**: `${SENAI_WORKER_STATE:-/tmp/senai-worker-state.json}`, ditulis
`workflow_models.py --verify` (mode baru, dipanggil `start.sh` pada tahap `model_verify`):

```json
{
  "protocol": "senai-worker/1",
  "workflows": "ltx25.yaml",
  "manifest_sha256": "<sha256 kanonik isi manifest yang dipilih>",
  "comfyui": "v0.36.0",
  "ready": true,
  "unready_code": null,
  "unready_message": null,
  "models": {"declared": 6, "present": 6, "missing": [], "bytes_manifest": 0, "bytes_visible": 0},
  "declared_model_names": ["ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors", "..."],
  "allowed_class_types": ["LoadImage", "..."],
  "custom_nodes": ["ComfyUI-LTXVideo"],
  "limits": {"no_progress_sec": 120, "no_progress_load_sec": 600, "execution_ceiling_sec": 1800},
  "warmup_graph": null
}
```

- `ready=false` + `unready_code` ∈ {`MODEL_CACHE_MISSING`, `OUTPUT_NOT_CONFIGURED`}, diisi A4
  saat verifikasi. `start.sh` **tidak keluar** saat unready; ComfyUI tetap dijalankan bila
  memungkinkan dan handler tetap start, supaya job dijawab cepat (kontrak §4.3).
- `comfyui` dibaca dari `/comfyui/comfyui_version.py` bila ada, selain itu dari ARG build
  `COMFYUI_VERSION` yang ditulis ke `/etc/senai-comfyui-version` oleh `Dockerfile`.
- Image identity: A4 menulis `${SENAI_IMAGE_REF}` (build arg → env) ke image; A1 melaporkannya
  sebagai `trace.image`. Bila kosong, pakai `"unknown"`.

## 6. `src/senai_worker.py` (A1)

```python
def boot(*, state_path: Path, timeline_path: Path, comfy: ComfyClient, ready_timeout_sec: float) -> BootState
    # dipanggil sekali di handler sebelum runpod.serverless.start:
    # tunggu /system_stats <= ready_timeout_sec (gagal -> state unready COMFYUI_UNREACHABLE);
    # /object_info harus memuat semua allowed_class_types (kurang -> unready COMFYUI_UNREACHABLE,
    # message menyebut class yang hilang); warm-up bila WARMUP=true dan warmup_graph ada;
    # tambahkan ready/warmup/serverless_start ke timeline

def run_job(job: dict, *, boot_state: BootState, comfy: ComfyClient) -> dict
    # return nilai handler: output kontrak §4.1/§4.2/§4.3, plus refresh_worker:true bila
    # REFRESH_WORKER=always, atau =dirty dan error.spec.refresh
    # REFRESH_WORKER=never tidak pernah menambahkannya
```

Urutan `run_job` (kode error di tiap langkah dari tabel kontrak §5):

1. `guard.parse_envelope`. Untuk health → output §4.3 dari boot state.
2. `boot_state.ready` false → `WorkerError(unready_code)`.
3. `guard.check_allowlist`, lalu `guard.check_model_references`.
4. `guard.fetch_inputs` ke `${COMFY_INPUT_DIR:-/comfyui/input}/senai/<rp_job_id>/`, lalu
   `guard.rewrite_input_names` dengan `name → "senai/<rp_job_id>/<name>"`.
5. `comfy.queue_prompt(graph, client_id=rp_job_id)`. HTTP 400 → `PROMPT_REJECTED` dengan
   `node_errors` (≤ 20).
6. Pantau (websocket + `/history` sebagai rekonsiliasi saat reconnect dan setiap 10 detik):
   - `execution_error` → `CUDA_OOM` bila `exception_type`/`exception_message` memuat
     `OutOfMemoryError` atau `CUDA out of memory`, selain itu `NODE_EXCEPTION`
     (`node_id`, `node_type` → `class_type`).
   - Deadline = min(`deadline_at`, mulai + `JOB_DEADLINE_CEILING_SEC`,
     mulai + `limits.execution_ceiling_sec`) → `EXECUTION_DEADLINE`.
   - Tanpa progres: jendela `no_progress_load_sec` sampai event `progress` pertama diterima
     atau selama node yang sedang `executing` punya class_type di `LOADER_CLASS_TYPES`
     (nama berakhiran `Loader` atau `CLIPTextEncode`/`MiniMaxH3ReferenceToVideo`/`TextEncode*`).
     Di luar itu `no_progress_sec` → `NO_PROGRESS`.
   - Pelanggaran → `comfy.interrupt()` lalu `comfy.delete_queue()`, kemudian error.
   - Proses ComfyUI hilang (PID di `COMFY_PID_FILE` tidak hidup, atau koneksi ditolak
     berulang) → `COMFYUI_CRASHED`.
7. `media_output.collect_outputs(...)` dengan `resolve_path` ke
   `${COMFY_OUTPUT_DIR:-/comfyui/output}/<subfolder>/<filename>`.
8. `finally`: hapus direktori input job.

- `runpod.serverless.progress_update(job, {"node": node_id, "value": v, "max": m})` dikirim
  paling sering sekali per 5 detik.
- Log: satu baris JSON per event ke stdout
  (`{"event","rp_job_id","generation_id","attempt","prompt_id","stage",...}`), tanpa URL
  presigned.

`src/comfy_client.py` (A1): `ComfyClient(base_url)` dengan `system_stats()`, `object_info()`,
`queue_prompt(graph, client_id)`, `history(prompt_id)`, `interrupt()`, `delete_queue()`,
`ws_connect(client_id)`. Semua memakai timeout HTTP eksplisit (≤ 30 detik).

## 7. `handler.py` (A1)

`handler(job)`:
- Bila `job["input"].get("protocol")` ada → `senai_worker.run_job`.
- Bila tidak ada dan `LEGACY_UPSTREAM_INPUT=true` → jalur upstream lama.
- Selain itu → `UNSUPPORTED_PROTOCOL` (bentuk §4.2).

Tidak ada exception yang lolos dari `handler`. Exception tak dikenal → `INTERNAL` (refresh).
Jalur `OUTPUT_FORMAT=senai` dicabut.

## 8. Env yang dibaca

Nama dan default persis `contract/senai-worker-1/pins.yaml` `env`. Env baru tidak boleh
ditambah tanpa laporan ke koordinator.

Pengecualian yang disetujui koordinator (bukan konfigurasi produksi, tidak diisi di template
endpoint):
- `SENAI_HANDLER_GRACE_SEC`: knob test untuk grace supervisor `start.sh`, default 30.
- Env yang disuntik RunPod atau base image, dibaca untuk `trace`: `RUNPOD_POD_ID` → `worker_id`,
  `CUDA_VERSION` (di-set image `nvidia/cuda`) → `cuda`, `SENAI_IMAGE_REF` → `image`.

## 9a. Catatan dari implementasi (2026-09-23)

- ComfyUI v0.36.0 mengirim progres sebagai `progress_state`
  (`{"prompt_id", "nodes": {node_id: {"value", "max", "state", ...}}}`), selain `progress`.
  Keduanya dihitung sebagai sinyal progres watchdog. `progress_update` ke RunPod memakai node
  yang `state == "running"`.

## 9. Menjalankan test

```bash
cd runpod/worker-comfyui
uv run --quiet --no-project --python 3.12 --with-requirements requirements.txt --with pytest --with jsonschema python -m pytest tests -q -p no:cacheprovider
```

Baseline sebelum Fase 2: 49 passed. Test yang memeriksa bentuk output protokol wajib
memvalidasinya dengan `jsonschema` terhadap `contract/senai-worker-1/response.schema.json`
(untuk output handler, bungkus sebagai `{"id": "t", "status": "COMPLETED", "output": <output>}`).
