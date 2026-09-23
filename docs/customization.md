# Customization

This guide covers methods for adding your own models, custom nodes, and static input files into a custom `worker-comfyui`.

> [!TIP]
>
> **Looking for the easiest way to deploy custom workflows?**
>
> [ComfyUI-to-API](https://comfy.getrunpod.io) automatically generates a custom Dockerfile and GitHub repository from your ComfyUI workflow, eliminating the manual setup described below. See the [ComfyUI-to-API Documentation](https://docs.runpod.io/community-solutions/comfyui-to-api/overview) for details.
>
> Use the manual methods below only if you need fine-grained control or prefer to manage everything yourself.

---

There are two primary methods for **manual** customization:

1.  **Custom Dockerfile (recommended for manual setup):** Create your own `Dockerfile` starting `FROM` one of the official `worker-comfyui` base images. This allows you to bake specific custom nodes, models, and input files directly into your image using `comfy-cli` commands. **This method does not require forking the `worker-comfyui` repository.**
2.  **Network Volume:** Store models on a persistent network volume attached to your RunPod endpoint. This is useful if you frequently change models or have very large models you don't want to include in the image build process.

## Method 1: Custom Dockerfile

> [!NOTE]
>
> This method does NOT require forking the `worker-comfyui` repository.

This is the most flexible and recommended approach for creating reproducible, customized worker environments.

1.  **Create a `Dockerfile`:** In your own project directory, create a file named `Dockerfile`.
2.  **Start with a Base Image:** Begin your `Dockerfile` by referencing one of the official base images. Using the `-base` tag is recommended as it provides a clean ComfyUI install with necessary tools like `comfy-cli` but without pre-packaged models.
    ```Dockerfile
    # start from a clean base image (replace <version> with the desired [release](https://github.com/runpod-workers/worker-comfyui/releases))
    FROM runpod/worker-comfyui:<version>-base
    ```
3.  **Install Custom Nodes:** Use the `comfy-node-install` (we had introduce our own cli tool here, as there is a [problem with comfy-cli not showing errors during installation](https://github.com/Comfy-Org/comfy-cli/pull/275)) command to add custom nodes by their name or URL, see [Comfy Registry](https://registry.comfy.org) to find the correct name. You can list multiple nodes.
    ```Dockerfile
    # install custom nodes using comfy-cli
    RUN comfy-node-install comfyui-kjnodes comfyui-ic-light
    ```
4.  **Download Models:** Use the `comfy model download` command to fetch models and place them in the correct ComfyUI directories.

    ```Dockerfile
    # download models using comfy-cli
    RUN comfy model download --url https://huggingface.co/KamCastle/jugg/resolve/main/juggernaut_reborn.safetensors --relative-path models/checkpoints --filename juggernaut_reborn.safetensors
    ```

> [!NOTE]
>
> Ensure you use the correct `--relative-path` corresponding to ComfyUI's model directory structure (starting with `models/<folder>`):
>
> checkpoints, clip, clip_vision, configs, controlnet, diffusers, embeddings, gligen, hypernetworks, loras, style_models, unet, upscale_models, vae, vae_approx, animatediff_models, animatediff_motion_lora, ipadapter, photomaker, sams, insightface, facerestore_models, facedetection, mmdets, instantid

5.  **Add Static Input Files (Optional):** If your workflows consistently require specific input images, masks, videos, etc., you can copy them directly into the image.

- Create an `input/` directory in the same folder as your `Dockerfile`.
- Place your static files inside this `input/` directory.
- Add a `COPY` command to your `Dockerfile`:

  ```Dockerfile
  # Copy local static input files into the ComfyUI input directory
  COPY input/ /comfyui/input/
  ```

- These files can then be referenced in your workflow using a "Load Image" (or similar) node pointing to the filename (e.g.,`my_static_image.png`).

Once you have created your custom `Dockerfile`, refer to the [Deployment Guide](deployment.md#deploying-custom-setups) for instructions on how to build, push and deploy your custom image to RunPod.

### Complete Custom `Dockerfile` Example

```Dockerfile
# start from a clean base image (replace <version> with the desired release)
FROM runpod/worker-comfyui:5.1.0-base

# install custom nodes using comfy-cli
RUN comfy-node-install comfyui-kjnodes comfyui-ic-light comfyui_ipadapter_plus comfyui_essentials ComfyUI-Hangover-Nodes

# download models using comfy-cli
# the "--filename" is what you use in your ComfyUI workflow
RUN comfy model download --url https://huggingface.co/KamCastle/jugg/resolve/main/juggernaut_reborn.safetensors --relative-path models/checkpoints --filename juggernaut_reborn.safetensors
RUN comfy model download --url https://huggingface.co/h94/IP-Adapter/resolve/main/models/ip-adapter-plus_sd15.bin --relative-path models/ipadapter --filename ip-adapter-plus_sd15.bin
RUN comfy model download --url https://huggingface.co/shiertier/clip_vision/resolve/main/SD15/model.safetensors --relative-path models/clip_vision --filename models.safetensors
RUN comfy model download --url https://huggingface.co/lllyasviel/ic-light/resolve/main/iclight_sd15_fcon.safetensors --relative-path models/diffusion_models --filename iclight_sd15_fcon.safetensors

# Copy local static input files into the ComfyUI input directory (delete if not needed)
# Assumes you have an 'input' folder next to your Dockerfile
COPY input/ /comfyui/input/
```

## Method 2: Network Volume

Using a Network Volume is primarily useful if you want to manage **models** separately from your worker image, especially if they are large or change often.

1.  **Create a Network Volume**:
    - Follow the [RunPod Network Volumes guide](https://docs.runpod.io/pods/storage/create-network-volumes) to create a volume in the same region as your endpoint.
2.  **Populate the Volume with Models**:
    - Use one of the methods described in the RunPod guide (e.g., temporary Pod + `wget`, direct upload, or the S3-compatible API) to place your model files into the correct ComfyUI directory structure **within the volume**.
    - For **serverless endpoints**, the network volume is mounted at `/runpod-volume`, and ComfyUI expects models under `/runpod-volume/models/...`. See [Network Volumes & Model Paths](network-volumes.md) for the exact structure and debugging tips.
      ```bash
      # Example structure inside the Network Volume (serverless worker view):
      # /runpod-volume/models/checkpoints/your_model.safetensors
      # /runpod-volume/models/loras/your_lora.pt
      # /runpod-volume/models/vae/your_vae.safetensors
      ```
    - **Important:** Ensure models are placed in the correct subdirectories (e.g., checkpoints in `models/checkpoints`, LoRAs in `models/loras`). If models are not detected, enable `NETWORK_VOLUME_DEBUG` as described in [Network Volumes & Model Paths](network-volumes.md).
3.  **Configure Your Endpoint**:
    - Use the Network Volume in your endpoint configuration:
      - Either create a new endpoint or update an existing one (see [Deployment Guide](deployment.md)).
      - In the endpoint configuration, under `Advanced > Select Network Volume`, select your Network Volume.

> [!NOTE]
>
> - When a Network Volume is correctly attached, ComfyUI running inside the worker container will automatically detect and load models from the standard directories (`/runpod-volume/models/...`) within that volume (for serverless workers). For directory mapping details and troubleshooting, see [Network Volumes & Model Paths](network-volumes.md).
> - This method is **not suitable for installing custom nodes**; use the Custom Dockerfile method for that.

## Generic workflow selection and model cache

The default Docker image contains no model weights and selects no workflow.
Adding a workflow does not add a Docker stage or download unrelated models.
`WORKFLOWS` selects comma-separated YAML manifests or editor JSON files from
`WORKFLOW_DIR` (default `/workflow`). `WORKFLOW_MANIFESTS` remains a legacy alias;
`WORKFLOWS` takes precedence. Files elsewhere in the directory are not scanned.

```dotenv
WORKFLOWS=minimax-h3.yaml
# Alternatively: WORKFLOWS=video_minimax_h3_r2v.json
```

MiniMax's editor JSON already contains `properties.models` with names, URLs and
model directories. The generic loader reads these (also inside subgraphs).
`minimax-h3.yaml` references that JSON without duplicating its model catalog.
This selects five MiniMax assets and no LTX weights. For API JSONs without model
metadata, use explicit model entries as in `ltx25.yaml`:

```yaml
version: 1
workflows:
  - my-api-workflow.json
models:
  - path: diffusion_models/my-model.safetensors
    url: https://huggingface.co/org/repo/resolve/COMMIT/my-model.safetensors
    # sha256: <64 hexadecimal characters>
```

Paths are relative to `COMFY_MODEL_ROOT`; any category or nested filename is
supported. Model references ending in `.safetensors`, `.gguf`, `.ckpt`, `.pt`,
`.pth`, or `.bin` must have download metadata. Other auxiliary files must be
listed explicitly. Missing metadata fails before downloading anything.

Editor JSON is used for **asset discovery only**. `handler.py` still requires
an API-format graph in `input.workflow`; export it from ComfyUI for execution.
The MiniMax reference images must also be supplied for actual inference.

### Prepare once, reuse on every cold start

When `/runpod-volume` exists, startup defaults to `/runpod-volume/models`;
otherwise it uses `/comfyui/models`. An explicit `COMFY_MODEL_ROOT` overrides it.
The generated extra-model-path configuration registers selected categories.

- `PREPARE_MODELS_ONLY=true`: populate the cache and exit, without a GPU check,
  ComfyUI or the Runpod handler. Use this in a separate preparation job/container.
- `MODEL_DOWNLOAD_POLICY=missing` (default): download missing/stale assets.
- `MODEL_DOWNLOAD_POLICY=cache-only`: require previously prepared cache entries;
  fail immediately if any are absent/stale, with no model network calls.
- `MODEL_DOWNLOAD_CONCURRENCY=4`: bounded parallel downloads (1–16).
- `MODEL_DOWNLOAD_CHECK_ONLY=true`: print the selected checklist and exit,
  without downloads or startup.

Downloads use HTTPS, bounded retries, per-model file locks, temporary files and
atomic replacement. Workers sharing a volume serialize downloads of the same
asset; different assets can download concurrently. `HF_TOKEN` or
`HUGGINGFACE_ACCESS_TOKEN` is sent only to Hugging Face.

Successful preparation records source URL, optional SHA256, size and modification
time in adjacent `.worker-cache.json` receipts. Subsequent starts compare metadata
without rereading tens of GB to hash them. Changed URLs/checksums invalidate the
receipt. Changed file size/mtime also invalidate it. This is a trusted-volume
optimization, not tamper-proof verification. Files without receipts are adopted
only during preparation; optional SHA256 is checked once. Use pinned revisions
and SHA256 for integrity; `main` URLs cannot reveal upstream changes automatically.

### Custom nodes remain build-time dependencies

Core-node workflows such as the supplied MiniMax JSON need no custom-node install.
For workflows that require extra packages, declare pinned dependencies in YAML:

```yaml
custom_nodes:
  - name: ComfyUI-LTXVideo
    repo: https://github.com/Lightricks/ComfyUI-LTXVideo.git
    revision: ac4d99839020b983e956a8ab67ec38aec1b6e65a
```

Build with `--build-arg CUSTOM_NODE_MANIFESTS=ltx25.yaml` to include those selected
dependencies. This does not select runtime workflows or download weights.
A model/graph change needs no rebuild when using a mounted `WORKFLOW_DIR`; new
Python/custom-node dependencies do require a compatible image. Manager stays offline.

### Handler and Senai output

Senai traffic is dispatched by `input.protocol == "senai-worker/1"` on each
request, not by an env flag — the old `OUTPUT_FORMAT=senai` switch has been
removed along with the handler code path it selected. Outputs always upload to
S3/R2 (`AWS_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_ENDPOINT_URL`, `AWS_DEFAULT_REGION=auto` for R2) and are reported as
presigned `https://` URLs with content-derived `media_type`, dimensions,
duration, fps and audio flags — base64 is never sent on this path, and a
missing bucket boots the worker into an explicit unready state instead of
falling back to base64. See
[Configuration Guide](configuration.md#senai-worker1-protocol) for the full
env table, protocol summary and known limitations. Callers that don't set
`input.protocol` keep the existing upstream image response format, but only
when `LEGACY_UPSTREAM_INPUT=true`; it is never enabled on a Senai endpoint.
