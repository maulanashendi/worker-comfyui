# Deployment

This guide explains how to deploy the `worker-comfyui` as a serverless endpoint on RunPod, covering both pre-built official images and custom-built images.

## Deploying Pre-Built Official Images

This is the simplest method if the official images meet your needs.

### Create your template (optional)

- Create a [new template](https://runpod.io/console/serverless/user/templates) by clicking on `New Template`
- In the dialog, configure:
  - Template Name: `worker-comfyui` (or your preferred name)
  - Template Type: serverless (change template type to "serverless")
  - Container Image: Use one of the official tags, e.g., `runpod/worker-comfyui:<version>-sd3`. (Refer to the main [README.md](../README.md#available-docker-images) for available image tags and the current version).
  - Container Registry Credentials: Leave as default (images are public).
  - Container Disk: Adjust based on the chosen image tag, see [GPU Recommendations](#gpu-recommendations).
  - (optional) Environment Variables: Configure S3 or other settings (see [Configuration Guide](configuration.md)).
    - Note: If you don't configure S3, images are returned as base64. For persistent storage across jobs without S3, consider using a [Network Volume](customization.md#method-2-network-volume-alternative-for-models). If models on your network volume are not being detected, see [Network Volumes & Model Paths](network-volumes.md) for troubleshooting steps.
- Click on `Save Template`

### Create your endpoint

- Navigate to [`Serverless > Endpoints`](https://www.runpod.io/console/serverless/user/endpoints) and click on `New Endpoint`
- In the dialog, configure:

  - Endpoint Name: `comfy` (or your preferred name)
  - Worker configuration: Select a GPU that can run the model included in your chosen image (see [GPU recommendations](#gpu-recommendations)).
  - Active Workers: `0` (Scale as needed based on expected load).
  - Max Workers: `3` (Set a limit based on your budget and scaling needs).
  - GPUs/Worker: `1`
  - Idle Timeout: `5` (Default is usually fine, adjust if needed).
  - Flash Boot: `enabled` (Recommended for faster worker startup).
  - Select Template: `worker-comfyui` (or the name you gave your template).
  - (optional) Advanced: If you are using a Network Volume, select it under `Select Network Volume`. See the [Customization Guide](customization.md#method-2-network-volume-alternative-for-models). For detailed model path layout and debugging tips, see [Network Volumes & Model Paths](network-volumes.md).

- Click `deploy`
- Your endpoint will be created. You can click on it to view the dashboard and find its ID.

### GPU recommendations (for Official Images)

| Model                     | Image Tag Suffix | Minimum VRAM Required | Recommended Container Size |
| ------------------------- | ---------------- | --------------------- | -------------------------- |
| Stable Diffusion XL       | `sdxl`           | 8 GB                  | 15 GB                      |
| Stable Diffusion 3 Medium | `sd3`            | 5 GB                  | 20 GB                      |
| FLUX.1 Schnell            | `flux1-schnell`  | 24 GB                 | 30 GB                      |
| FLUX.1 dev                | `flux1-dev`      | 24 GB                 | 30 GB                      |
| Base (No models)          | `base`           | N/A                   | 5 GB                       |

_Note: Container sizes are approximate and might vary slightly. Custom images will vary based on included models/nodes._

## Deploying Custom Setups

If you have created a custom environment using the methods in the [Customization Guide](customization.md), here's how to deploy it.

> [!TIP] > **Want to skip the manual setup?**
>
> [ComfyUI-to-API](https://comfy.getrunpod.io) automatically generates a GitHub repository with a custom Dockerfile from your ComfyUI workflow. You can then deploy it using [Method 2: GitHub Integration](#method-2-deploying-via-runpod-github-integration) below with no manual Docker building required. See the [ComfyUI-to-API Documentation](https://docs.runpod.io/community-solutions/comfyui-to-api/overview) for details.

### Method 1: Manual Build, Push, and Deploy

This method involves building your custom Docker image locally, pushing it to a registry, and then deploying that image on RunPod.

1.  **Write your Dockerfile:** Follow the instructions in the [Customization Guide](customization.md#method-1-custom-dockerfile-recommended) to create your `Dockerfile` specifying the base image, nodes, models, and any static files.
2.  **Build the Docker image:** Navigate to the directory containing your `Dockerfile` and run:
    ```bash
    # Replace <your-image-name>:<tag> with your desired name and tag
    docker build --platform linux/amd64 -t <your-image-name>:<tag> .
    ```
    - **Crucially**, always include `--platform linux/amd64` for RunPod compatibility.
3.  **Tag the image for your registry:** Replace `<your-registry-username>` and `<your-image-name>:<tag>` accordingly.
    ```bash
    # Example for Docker Hub:
    docker tag <your-image-name>:<tag> <your-registry-username>/<your-image-name>:<tag>
    ```
4.  **Log in to your container registry:**
    ```bash
    # Example for Docker Hub:
    docker login
    ```
5.  **Push the image:**
    ```bash
    # Example for Docker Hub:
    docker push <your-registry-username>/<your-image-name>:<tag>
    ```
6.  **Deploy on RunPod:**
    - Follow the steps in [Create your template](#create-your-template-optional) above, but for the `Container Image` field, enter the full name of the image you just pushed (e.g., `<your-registry-username>/<your-image-name>:<tag>`).
    - If your registry is private, you will need to provide [Container Registry Credentials](https://docs.runpod.io/serverless/templates#container-registry-credentials).
    - Adjust the `Container Disk` size based on your custom image contents.
    - Follow the steps in [Create your endpoint](#create-your-endpoint) using the template you just created.

### Method 2: Deploying via RunPod GitHub Integration

RunPod offers a seamless way to deploy directly from your GitHub repository containing the `Dockerfile`. RunPod handles the build and deployment.

1.  **Prepare your GitHub Repository:** Ensure your repository contains the custom `Dockerfile` (as described in the [Customization Guide](customization.md#method-1-custom-dockerfile-recommended)) at the root or a specified path.
2.  **Connect GitHub to RunPod:** Authorize RunPod to access your repository via your RunPod account settings or when creating a new endpoint.
3.  **Create a New Serverless Endpoint:** In RunPod, navigate to Serverless -> `+ New Endpoint` and select the **"Start from GitHub Repo"** option.
4.  **Configure:**
    - Select the GitHub repository and branch you want to deploy (e.g., `main`).
    - Specify the **Context Path** (usually `/` if the Dockerfile is at the root).
    - Specify the **Dockerfile Path** (usually `Dockerfile`).
    - Configure your desired compute resources (GPU type, workers, etc.).
    - Configure any necessary [Environment Variables](configuration.md).
5.  **Deploy:** RunPod will clone the repository, build the image from your specified branch and Dockerfile, push it to a temporary registry, and deploy the endpoint.

Every `git push` to the configured branch will automatically trigger a new build and update your RunPod endpoint. For more details, refer to the [RunPod GitHub Integration Documentation](https://docs.runpod.io/serverless/github-integration).

## Generic worker: prepare assets separately from serving

Build the model-free image once (MiniMax uses only core nodes):

```bash
docker build --platform linux/amd64 -t worker-comfyui:generic .
```

For extra node dependencies, use the same Dockerfile with
`--build-arg CUSTOM_NODE_MANIFESTS=ltx25.yaml`. There is no `ltx25` target and no
hardcoded LTX runtime environment. Default `docker buildx bake` builds only base.
Historical baked-model release targets remain explicit opt-ins; adding another
workflow does not require adding one of these targets.

Inspect MiniMax's model checklist without a GPU or download:

```bash
docker run --rm -e WORKFLOWS=minimax-h3.yaml \
  -e MODEL_DOWNLOAD_CHECK_ONLY=true worker-comfyui:generic
```

Populate a shared volume once, outside serving workers:

```bash
docker run --rm -v "$PWD/model-cache:/runpod-volume" \
  -e WORKFLOWS=minimax-h3.yaml -e PREPARE_MODELS_ONLY=true \
  -e MODEL_DOWNLOAD_POLICY=missing worker-comfyui:generic
```

On Runpod, perform equivalent preparation against the **same network volume**
attached to the endpoint; a local Docker directory is not that remote volume.
Then configure the serving endpoint:

```dotenv
WORKFLOWS=minimax-h3.yaml
MODEL_DOWNLOAD_POLICY=cache-only
COMFY_MODEL_ROOT=/runpod-volume/models
REFRESH_WORKER=true
# For Senai consumers, plus AWS storage credentials:
OUTPUT_FORMAT=senai
```

Switch to `WORKFLOWS=ltx25.yaml` for LTX after preparing its cache and ensuring
its node dependencies are installed. Mounted workflow definitions can change
without a rebuild. Select only workflows the endpoint actually serves.

`cache-only` removes download and whole-file hashing from cold start, but does
not remove image pull, Python/ComfyUI initialization or model-to-GPU loading.
No GPU latency measurements have been made locally.

`REFRESH_WORKER=true` requests retirement after each success or error response.
The SDK sends the result with `stopPod`; `start.sh` cleans up child processes on
exit/SIGTERM. Set endpoint Active Workers = 0, Max Workers = 1 initially and idle
timeout = 5 seconds. Cloud retirement still needs a live acceptance check.

### Local verification and staging checklist

```bash
docker build --platform linux/amd64 -f tests/Dockerfile -t worker-comfyui-tests .
docker run --rm worker-comfyui-tests
```

Tests use the real startup script, handler and Runpod SDK, with external services
mocked. They cover selected-only downloads, MiniMax editor metadata, all three
Senai LTX graphs, cache-only/no-network startup, cache invalidation, shared-volume
locking, handler responses, SDK `stopPod`, crash/SIGTERM and process cleanup.

- [ ] Download real weights and pin source revisions/checksums.
- [ ] Build the production GPU image and validate required node registrations.
- [ ] Prepare the remote network volume, then boot with `cache-only`.
- [ ] Submit API-format workflows and reference assets for actual GPU inference.
- [ ] Verify storage URLs remain readable after retirement.
- [ ] Observe workers scale to zero and measure cold start on the staging endpoint.

Real model downloads remain skipped. No endpoint is created by the local tests.

Local verification (2026-09-22): **49 tests passed** in the Python 3.12 CPU
container. MiniMax checklist selects five assets only. `docker buildx bake
--print` selects only `base`; startup syntax and whitespace checks passed.
Production GPU build, actual weight downloads and cloud cold-start timing have
not been measured.
