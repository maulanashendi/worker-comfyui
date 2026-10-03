# Changelog — kontrak runpod-senai-worker

## 0.4.0 — 2026-10-03

Rename only, wire format unchanged: self-hosted LTX 2.5 is "Senfers 1.0" everywhere senai
owns the name (user decision 2026-10-03). Higgsfield `ltx-2.5-fast`/`ltx-2.5-pro` and vavo's
`runpod-ltx25` contract are untouched.

- adapters: `ltx25_senai_worker_v1` -> `senfers1_senai_worker_v1`.
- sets/manifests: `ltx25.yaml` -> `senfers1.yaml`.
- workflow ids/paths: `ltx25-{t2v,i2v,flf}-v1` -> `senfers1-{t2v,i2v,flf}-v1`, paths under
  `backend/src/senai/modules/providers/runpod/workflows/` renamed to match.
- `sha256_canonical` for the three renamed workflows is **unverified** pending recompute: the
  graphs' `SaveVideo.filename_prefix` also moved from `video/LTX-2.5_*` to `video/Senfers1_*`,
  which changes the canonical bytes. Recompute against the real senai-side graphs once that
  rename lands there, in the same change that fills in `pins.yaml`.

## 0.3.0 — 2026-09-24

Tambahan opsional (minor), dari G8 yang diputus RunPod pada 605 s tanpa diagnosis:

- §3.1 request `limits.max_execution_sec`: anggaran eksekusi dari mulai job, diisi sama dengan
  `policy.executionTimeout` / 1000. Worker berhenti 30 s sebelumnya dengan `EXECUTION_DEADLINE`
  (pesan menyebut node yang sedang jalan), sehingga error bertipe terkirim sebelum RunPod
  memutus job. Sebelumnya deadline worker selalu jatuh setelah `executionTimeout`.
- §4.1 response `timings.node_sec`: detik per node yang dieksekusi, di output sukses dan error.

## 0.2.1 — 2026-09-23

Klarifikasi dari review implementasi, tanpa perubahan bentuk kabel:

- §7: rujukan model hanya string tanpa whitespace, supaya prompt yang berakhiran `.pt`/`.bin`
  tidak salah ditolak `MODEL_NOT_IN_MANIFEST`.
- §4.2: flag yang berbeda dari tabel §5 dicatat (log + `raw.flag_mismatch`), job tidak gagal.
- §6.1: output error dengan `protocol` salah dan sukses tanpa `trace` → `PROVIDER_CONTRACT_ERROR`.
- §4.3: `manifest_sha256` selalu diisi, juga saat unready.

## 0.2.0 — 2026-09-23

Breaking (masih draft, belum ada deployment):

- Objek error protokol pindah dari `output.error` ke **`output.failure`**. SDK RunPod
  (`rp_job.run_job`) menjalankan `pop("error")` dan `pop("refresh_worker")` pada return
  handler, sehingga `output.error` berubah menjadi job `FAILED` tanpa detail. Temuan executor
  WP A1 dengan `run_job` SDK asli, diverifikasi koordinator di kode SDK 1.7.x dan 1.12.0.
- Schema output menolak kunci `error` dan `refresh_worker`, karena keduanya dicadangkan SDK.
- `pins.yaml`: `output.error_object_key`, `output.sdk_reserved_keys`.

## 0.1.0 — 2026-09-23

Draft awal protokol `senai-worker/1`. Ditulis sebelum implementasi, sebagai spesifikasi
bersama untuk worker (`maulanashendi/worker-comfyui`, basis `094dbbe`) dan adapter senai.
Rancangannya ada di `docs/design/2026-09-23-runpod-comfyui-pipeline/`.

- Envelope request: `protocol`, graph format API dari senai, `inputs[]` (URL R2 atau base64
  gambar kecil), `trace` yang wajib dan di-echo balik, dan `limits.deadline_at`.
- Envelope response: sukses dengan metadata konten (ffprobe) dan key R2 deterministik; error
  bertipe di dalam `output`, bukan `error` top-level; health dengan status manifest.
- Taksonomi 22 kode error dengan flag `gpu_work`/`infra`/`retryable`/`refresh` (§5), yang juga
  ada mesin-terbaca di `pins.yaml`.
- Set workflow dipilih `WORKFLOWS`; bobot dari cached model Hugging Face (K1); GPU minimum
  48 GB (K2).
- Graph LTX di-pin sama dengan `runpod-ltx25`. Graph H3 berstatus `pending` sampai diekspor
  dari ComfyUI v0.36.0.
- Klaim yang belum terbukti di GPU tercatat di §10 dan menentukan kenaikan ke 1.0.0.
