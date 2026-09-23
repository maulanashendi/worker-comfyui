# worker-comfyui

> [ComfyUI](https://github.com/comfyanonymous/ComfyUI) as a serverless API on [RunPod](https://www.runpod.io/)

<p align="center">
  <img src="assets/worker_sitting_in_comfy_chair.jpg" title="Worker sitting in comfy chair" />
</p>

[![RunPod](https://api.runpod.io/badge/runpod-workers/worker-comfyui)](https://www.runpod.io/console/hub/runpod-workers/worker-comfyui)

---

This project allows you to run ComfyUI workflows as a serverless API endpoint on the RunPod platform. Submit workflows via API calls and receive generated images as base64 strings or S3 URLs.

## Table of Contents

- [Alur worker generik: dari workflow sampai endpoint](#alur-worker-generik-dari-workflow-sampai-endpoint)
- [Quickstart upstream](#quickstart-upstream)
- [Available Docker Images](#available-docker-images)
- [API Specification](#api-specification)
- [Usage](#usage)
- [Getting the Workflow JSON](#getting-the-workflow-json)
- [Further Documentation](#further-documentation)

---

## Alur worker generik: dari workflow sampai endpoint

Fork ini melayani permintaan Senai lewat protokol **`senai-worker/1`**
(`input.protocol == "senai-worker/1"`), bukan lagi lewat env `OUTPUT_FORMAT=senai`
yang sudah dicabut. Kontrak kabel lengkap ada di `contract/senai-worker-1/`
(salinan dari `contracts/runpod-senai-worker/` repo senai) dan detail alur
internal worker ada di [`docs/senai-worker-internals.md`](docs/senai-worker-internals.md).
Bagian di bawah ini menjelaskan bagian yang masih sama untuk kedua protokol
(pemilihan workflow/model), lalu [§6](#6-siapkan-image-dan-konfigurasi-endpoint)
menjelaskan env spesifik protokol senai.

Alur yang digunakan repo ini:

**Pilih workflow → tes lokal → build image → checklist model → isi cache sekali → jalankan endpoint `cache-only` → hasil dikirim → worker dihentikan.**

Image default berisi runtime ComfyUI dan handler, tanpa bobot model dan tanpa
workflow aktif. Menambahkan workflow tidak memerlukan target Docker baru.
Semua perintah berikut dijalankan dari direktori `worker-comfyui/`.

### 1. Pilih workflow yang akan dilayani

| Workflow | Nilai `WORKFLOWS` | Sumber daftar model | Dependency tambahan saat build |
| --- | --- | --- | --- |
| MiniMax H3 R2V | `minimax-h3.yaml` | Metadata di `video_minimax_h3_r2v.json`; 5 aset | Tidak ada custom node dalam graph yang disediakan |
| LTX 2.5 Senai | `ltx25.yaml` | Enam aset untuk graph T2V, I2V, dan FLF | `CUSTOM_NODE_MANIFESTS=ltx25.yaml` |

Contoh berikut memakai MiniMax:

```bash
export WORKFLOWS=minimax-h3.yaml
export WORKER_IMAGE=worker-comfyui:generic
```

`WORKFLOWS=video_minimax_h3_r2v.json` juga valid untuk membaca metadata model
langsung. File lain dalam folder `workflow/` tidak otomatis diunduh.
`WORKFLOW_MANIFESTS` tetap didukung sebagai alias lama; `WORKFLOWS` diprioritaskan.

**Bedakan dua format:** JSON editor MiniMax dipakai untuk menemukan aset model.
Handler tetap menerima **JSON API** dalam `input.workflow`; export melalui
ComfyUI sebelum mengirim job. Pemilihan env menyiapkan aset, bukan otomatis
menjalankan graph atau mengubah format editor menjadi API.

### 2. Jalankan tes lokal tanpa GPU dan tanpa download model

```bash
docker build --platform linux/amd64 \
  -f tests/Dockerfile -t worker-comfyui-tests .
docker run --rm worker-comfyui-tests
```

Tes mencakup pemilihan aset, cache, download dengan data tiruan, respons handler,
SDK Runpod `stopPod`, serta cleanup proses ketika selesai, crash, dan SIGTERM.
Hasil terakhir: **49 tes lulus**. Layanan ComfyUI/GPU, download dan S3 dimock;
ini belum membuktikan inference atau penghentian worker di cloud.

### 3. Build image runtime generik

Untuk MiniMax:

```bash
docker build --platform linux/amd64 -t "$WORKER_IMAGE" .
```

Untuk LTX, gunakan Dockerfile yang sama dengan dependency terpilih:

```bash
export WORKFLOWS=ltx25.yaml
export WORKER_IMAGE=worker-comfyui:with-ltx-nodes
docker build --platform linux/amd64 \
  --build-arg CUSTOM_NODE_MANIFESTS=ltx25.yaml \
  -t "$WORKER_IMAGE" .
```

Build arg memasang custom node yang dipin dalam YAML, bukan bobot model atau
pilihan workflow runtime. Tidak ada target `--target ltx25`. Perubahan dependency
Python/custom node membutuhkan rebuild; perubahan model tidak.

### 4. Periksa checklist model terlebih dahulu

```bash
docker run --rm \
  -e WORKFLOWS="$WORKFLOWS" \
  -e MODEL_DOWNLOAD_CHECK_ONLY=true \
  "$WORKER_IMAGE"
```

Container mencetak aset terpilih lalu berhenti, tanpa GPU check, download,
ComfyUI, atau handler. MiniMax harus menampilkan lima aset MiniMax saja.

**Untuk tahap validasi tanpa bobot nyata, berhenti di sini.** Langkah pengisian
cache berikut memang melakukan download; lanjutkan ketika siap mengunduh model.

### 5. Isi cache sekali, terpisah dari worker yang melayani job

Contoh cache lokal:

```bash
mkdir -p model-cache
docker run --rm \
  -v "$PWD/model-cache:/runpod-volume" \
  -e WORKFLOWS="$WORKFLOWS" \
  -e PREPARE_MODELS_ONLY=true \
  -e MODEL_DOWNLOAD_POLICY=missing \
  -e MODEL_DOWNLOAD_CONCURRENCY=4 \
  "$WORKER_IMAGE"
```

Untuk model yang memerlukan autentikasi, isi `HF_TOKEN` di lingkungan shell dan
tambahkan `-e HF_TOKEN` pada perintah tersebut. Proses preparation tidak memerlukan
GPU dan berhenti setelah selesai. Bobot tersimpan di `model-cache/models/`.

Download menggunakan file sementara, penggantian atomik, dan lock per model.
Receipt `.worker-cache.json` disimpan di samping bobot. Pertahankan receipt ketika
menggunakan kembali cache; `cache-only` memeriksa receipt, sumber, ukuran, dan
mtime tanpa membaca seluruh bobot untuk menghitung ulang hash.

Untuk Runpod, isi **network volume yang sama** dengan yang akan dipasang pada
endpoint. Folder Docker lokal di atas bukan network volume Runpod. Jalankan image
preparation pada compute sementara yang memasang volume tersebut dan atur
`COMFY_MODEL_ROOT` ke lokasi aktualnya. Contoh: bila volume pada Pod terpasang di
`/workspace`, gunakan `/workspace/models`; isi volume itu nantinya terlihat sebagai
`/runpod-volume/models` pada serverless. Hentikan compute preparation setelah selesai.

Jika cache dipindahkan dan mtime berubah, jalankan preparation lagi terhadap
lokasi tujuan sebelum serving. Untuk unduhan yang dapat direproduksi, pin revision
URL dan isi SHA256 dalam manifest; contoh awal masih memakai URL upstream `main`.

### 6. Siapkan image dan konfigurasi endpoint

Push image hasil build ke registry milik Anda:

```bash
export REGISTRY_IMAGE=ghcr.io/your-account/worker-comfyui:generic-v1
docker login ghcr.io
docker tag "$WORKER_IMAGE" "$REGISTRY_IMAGE"
docker push "$REGISTRY_IMAGE"
```

Ganti `your-account` dan tag sesuai registry tujuan. Di Runpod, buat template
serverless menggunakan image tersebut, lalu endpoint queue-based dengan:

- Network volume yang sudah diisi pada langkah 5.
- GPU dan kapasitas disk yang sesuai dengan workflow.
- Active Workers **0**, Max Workers **1** untuk pengujian awal.
- Idle timeout **5 detik** dan satu job per worker pada satu waktu.

Isi env endpoint (contoh MiniMax):

```dotenv
WORKFLOWS=minimax-h3.yaml
MODEL_DOWNLOAD_POLICY=cache-only
COMFY_MODEL_ROOT=/runpod-volume/models
REFRESH_WORKER=true
```

Jangan aktifkan `PREPARE_MODELS_ONLY`, `MODEL_DOWNLOAD_CHECK_ONLY`, atau
`SERVE_API_LOCALLY` pada endpoint serving. Cache yang belum siap akan menghasilkan
error yang meminta preparation; worker tidak diam-diam mengunduh saat cold start.

**Endpoint yang melayani Senai** memakai protokol `senai-worker/1`, bukan lagi
env `OUTPUT_FORMAT=senai` (dicabut). Tidak ada env pemilih protokol di sisi
worker — dispatch ditentukan oleh `input.protocol` pada tiap request; endpoint
hanya perlu env berikut selain env pemilihan workflow di atas:

```dotenv
HF_CACHE_ROOT=/runpod-volume/huggingface-cache/hub   # default, sesuaikan bila beda
AWS_BUCKET_NAME=your-output-bucket
AWS_ACCESS_KEY_ID=***
AWS_SECRET_ACCESS_KEY=***
AWS_DEFAULT_REGION=auto
AWS_ENDPOINT_URL=https://your-account.r2.cloudflarestorage.com
INPUT_ALLOWED_HOSTS=your-senai-asset-bucket.example.com
REFRESH_WORKER=dirty
```

Untuk deployment Senai, bobot **bukan** diisi lewat langkah 4–5 di atas (network
volume yang disiapkan manual): produksi memakai fitur **cached model Hugging
Face RunPod** — repo HF yang dipilih di template endpoint, dicache RunPod di
`HF_CACHE_ROOT/models--<org>--<repo>/snapshots/<revision>/<file>`. Boot
memverifikasi keberadaan tiap file dengan `stat` (ukuran cocok manifest), tanpa
menghitung ulang hash. Cache yang kurang tidak membuat worker crash-loop;
worker tetap boot dalam **mode unready** (`ready:false`, kode
`MODEL_CACHE_MISSING`/`OUTPUT_NOT_CONFIGURED`) dan menjawab tiap job dengan
kode itu, supaya operator melihat penyebabnya lewat `/health` alih-alih retry
tanpa akhir. Detail env lengkap, nilai produksi, dan penjelasan protokol ada di
[Configuration Guide](docs/configuration.md#senai-worker1-protocol).

### 7. Kirim job API dan verifikasi sampai worker berhenti

Contoh di bawah memakai payload upstream (`input.workflow` + `input.images`),
berlaku hanya ketika `LEGACY_UPSTREAM_INPUT=true` dan tidak dipakai di endpoint
Senai. Untuk endpoint Senai, kirim envelope `senai-worker/1`
(`input.protocol`, `input.workflow`, `input.inputs[]`, `input.trace`,
`input.limits`) seperti dicontohkan di
[Configuration Guide](docs/configuration.md#senai-worker1-protocol); jangan
mengirim JSON editor MiniMax langsung sebagai payload pada kedua jalur.

Siapkan `request.json` dengan envelope `{"input":{"workflow": ...}}` berisi
graph API lengkap. Untuk R2V/I2V, tambahkan `input.images` berisi nama dan base64
gambar referensi yang cocok dengan node `LoadImage`.

Dengan `RUNPOD_API_KEY` dan `RUNPOD_ENDPOINT_ID` sudah diisi pada shell:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @request.json \
  "https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/run"
```

Salin `id` dari respons, kemudian periksa status sampai terminal:

```bash
export RUNPOD_JOB_ID=replace-with-returned-job-id
curl --fail-with-body \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  "https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/status/$RUNPOD_JOB_ID"
```

Pastikan hasil video tersedia, URL storage tetap dapat dibaca setelah worker
berhenti, dan jumlah worker aktif/idle kembali nol saat antrean kosong.
`REFRESH_WORKER=true` meminta retirement melalui SDK setelah hasil dikembalikan;
Runpod yang melakukan penghentian cloud. `start.sh` membersihkan proses anak pada
exit/SIGTERM. Verifikasi ini tetap diperlukan di staging.

Cache menghilangkan download dan hashing seluruh bobot saat cold start, tetapi
image pull, startup ComfyUI, dan pemuatan bobot ke GPU tetap membutuhkan waktu.
Build produksi GPU, download bobot nyata, dan latensi cloud belum diverifikasi
oleh tes CPU di atas.

### 8. Tambahkan atau ganti workflow berikutnya

1. Tambahkan JSON ke `workflow/`. Jika editor JSON memiliki `properties.models`,
   buat YAML yang merujuk file itu, seperti [`minimax-h3.yaml`](workflow/minimax-h3.yaml).
   Untuk graph tanpa URL model, tulis `models` secara eksplisit seperti
   [`ltx25.yaml`](workflow/ltx25.yaml).
2. Pasang definisi workflow melalui volume dengan `WORKFLOW_DIR` yang sesuai,
   atau rebuild image untuk menyertakan file baru di `/workflow`.
3. Jika ada custom node baru, deklarasikan repo dan commit dalam YAML, lalu build
   dengan `CUSTOM_NODE_MANIFESTS` yang sesuai. Dependency tidak dipasang saat boot.
4. Jalankan checklist dan preparation untuk workflow baru pada cache yang sama.
5. Ubah `WORKFLOWS` pada endpoint dan tetap gunakan `cache-only`. Beberapa workflow
   bisa dipilih dengan koma; hanya pilih yang memang akan dilayani.

Detail schema dan cache ada di [Customization Guide](docs/customization.md#generic-workflow-selection-and-model-cache),
dan checklist staging ada di [Deployment Guide](docs/deployment.md#generic-worker-prepare-assets-separately-from-serving).

## Quickstart upstream

Bagian berikut menjelaskan image upstream yang sudah dipublikasikan. Untuk build
repo ini dan alur cache baru, gunakan panduan worker generik di atas.

1.  🐳 Choose one of the [available Docker images](#available-docker-images) for your serverless endpoint (e.g., `runpod/worker-comfyui:<version>-sd3`).
2.  📄 Follow the [Deployment Guide](docs/deployment.md) to set up your RunPod template and endpoint.
3.  ⚙️ Optionally configure the worker (e.g., for S3 upload) using environment variables - see the full [Configuration Guide](docs/configuration.md).
4.  🧪 Pick an example workflow from [`test_resources/workflows/`](./test_resources/workflows/) or [get your own](#getting-the-workflow-json).
5.  🚀 Follow the [Usage](#usage) steps below to interact with your deployed endpoint.

## Available Docker Images

These images are available on Docker Hub under `runpod/worker-comfyui`:

- **`runpod/worker-comfyui:<version>-base`**: Clean ComfyUI install with no models.
- **`runpod/worker-comfyui:<version>-flux1-schnell`**: Includes checkpoint, text encoders, and VAE for [FLUX.1 schnell](https://huggingface.co/black-forest-labs/FLUX.1-schnell).
- **`runpod/worker-comfyui:<version>-flux1-dev`**: Includes checkpoint, text encoders, and VAE for [FLUX.1 dev](https://huggingface.co/black-forest-labs/FLUX.1-dev).
- **`runpod/worker-comfyui:<version>-sdxl`**: Includes checkpoint and VAEs for [Stable Diffusion XL](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0).
- **`runpod/worker-comfyui:<version>-sd3`**: Includes checkpoint for [Stable Diffusion 3 medium](https://huggingface.co/stabilityai/stable-diffusion-3-medium).

Replace `<version>` with the current release tag, check the [releases page](https://github.com/runpod-workers/worker-comfyui/releases) for the latest version.

## API Specification

The worker exposes standard RunPod serverless endpoints (`/run`, `/runsync`, `/health`). By default, images are returned as base64 strings. You can configure the worker to upload images to an S3 bucket instead by setting specific environment variables (see [Configuration Guide](docs/configuration.md)).

Use the `/runsync` endpoint for synchronous requests that wait for the job to complete and return the result directly. Use the `/run` endpoint for asynchronous requests that return immediately with a job ID; you'll need to poll the `/status` endpoint separately to get the result.

### Input

```json
{
  "input": {
    "workflow": {
      "6": {
        "inputs": {
          "text": "a ball on the table",
          "clip": ["30", 1]
        },
        "class_type": "CLIPTextEncode",
        "_meta": {
          "title": "CLIP Text Encode (Positive Prompt)"
        }
      }
    },
    "images": [
      {
        "name": "input_image_1.png",
        "image": "data:image/png;base64,iVBOR..."
      }
    ]
  }
}
```

The following tables describe the fields within the `input` object:

| Field Path                | Type   | Required | Description                                                                                                                                |
| ------------------------- | ------ | -------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `input`                   | Object | Yes      | Top-level object containing request data.                                                                                                  |
| `input.workflow`          | Object | Yes      | The ComfyUI workflow exported in the [required format](#getting-the-workflow-json).                                                        |
| `input.images`            | Array  | No       | Optional array of input images. Each image is uploaded to ComfyUI's `input` directory and can be referenced by its `name` in the workflow. |
| `input.comfy_org_api_key` | String | No       | Optional per-request Comfy.org API key for API Nodes. Overrides the `COMFY_ORG_API_KEY` environment variable if both are set.              |

#### `input.images` Object

Each object within the `input.images` array must contain:

| Field Name | Type   | Required | Description                                                                                                                       |
| ---------- | ------ | -------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `name`     | String | Yes      | Filename used to reference the image in the workflow (e.g., via a "Load Image" node). Must be unique within the array.            |
| `image`    | String | Yes      | Base64 encoded string of the image. A data URI prefix (e.g., `data:image/png;base64,`) is optional and will be handled correctly. |

> [!NOTE]
>
> **Size Limits:** RunPod endpoints have request size limits (e.g., 10MB for `/run`, 20MB for `/runsync`). Large base64 input images can exceed these limits. See [RunPod Docs](https://docs.runpod.io/docs/serverless-endpoint-urls).

### Output

> [!WARNING]
>
> **Breaking Change in Output Format (5.0.0+)**
>
> Versions `< 5.0.0` returned the primary image data (S3 URL or base64 string) directly within an `output.message` field.
> Starting with `5.0.0`, the output format has changed significantly, see below

```json
{
  "id": "sync-uuid-string",
  "status": "COMPLETED",
  "output": {
    "images": [
      {
        "filename": "ComfyUI_00001_.png",
        "type": "base64",
        "data": "iVBORw0KGgoAAAANSUhEUg..."
      }
    ]
  },
  "delayTime": 123,
  "executionTime": 4567
}
```

| Field Path      | Type             | Required | Description                                                                                                 |
| --------------- | ---------------- | -------- | ----------------------------------------------------------------------------------------------------------- |
| `output`        | Object           | Yes      | Top-level object containing the results of the job execution.                                               |
| `output.images` | Array of Objects | No       | Present if the workflow generated images. Contains a list of objects, each representing one output image.   |
| `output.errors` | Array of Strings | No       | Present if non-fatal errors or warnings occurred during processing (e.g., S3 upload failure, missing data). |

#### `output.images`

Each object in the `output.images` array has the following structure:

| Field Name | Type   | Description                                                                                     |
| ---------- | ------ | ----------------------------------------------------------------------------------------------- |
| `filename` | String | The original filename assigned by ComfyUI during generation.                                    |
| `type`     | String | Indicates the format of the data. Either `"base64"` or `"s3_url"` (if S3 upload is configured). |
| `data`     | String | Contains either the base64 encoded image string or the S3 URL for the uploaded image file.      |

> [!NOTE]
> The `output.images` field provides a list of all generated images (excluding temporary ones).
>
> - If S3 upload is **not** configured (default), `type` will be `"base64"` and `data` will contain the base64 encoded image string.
> - If S3 upload **is** configured, `type` will be `"s3_url"` and `data` will contain the S3 URL. See the [Configuration Guide](docs/configuration.md#example-s3-response) for an S3 example response.
> - Clients interacting with the API need to handle this list-based structure under `output.images`.

## Usage

To interact with your deployed RunPod endpoint:

1.  **Get API Key:** Generate a key in RunPod [User Settings](https://www.runpod.io/console/serverless/user/settings) (`API Keys` section).
2.  **Get Endpoint ID:** Find your endpoint ID on the [Serverless Endpoints](https://www.runpod.io/console/serverless/user/endpoints) page or on the `Overview` page of your endpoint.

### Generate Image (Sync Example)

Send a workflow to the `/runsync` endpoint (waits for completion). Replace `<api_key>` and `<endpoint_id>`. The `-d` value should contain the [JSON input described above](#input).

```bash
curl -X POST \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{"input":{"workflow":{... your workflow JSON ...}}}' \
  https://api.runpod.ai/v2/<endpoint_id>/runsync
```

You can also use the `/run` endpoint for asynchronous jobs and then poll the `/status` to see when the job is done. Or you [add a `webhook` into your request](https://docs.runpod.io/serverless/endpoints/send-requests#webhook-notifications) to be notified when the job is done.

Refer to [`test_input.json`](./test_input.json) for a complete input example.

## Getting the Workflow JSON

To get the correct `workflow` JSON for the API:

1.  Open ComfyUI in your browser.
2.  In the top navigation, select `Workflow > Export (API)`
3.  A `workflow.json` file will be downloaded. Use the content of this file as the value for the `input.workflow` field in your API requests.

## SSH Access

To enable SSH access to the worker, set the `PUBLIC_KEY` environment variable to your SSH public key. The worker will start an SSH server automatically. Make sure to expose **port 22** in your RunPod template so you can connect.

## Further Documentation

- **[Deployment Guide](docs/deployment.md):** Detailed steps for deploying on RunPod.
- **[Configuration Guide](docs/configuration.md):** Full list of environment variables (including S3 setup).
- **[Customization Guide](docs/customization.md):** Adding custom models and nodes (Network Volumes, Docker builds).
- **[Development Guide](docs/development.md):** Setting up a local environment for development & testing
- **[CI/CD Guide](docs/ci-cd.md):** Information about the automated Docker build and publish workflows.
- **[Acknowledgments](docs/acknowledgments.md):** Credits and thanks
