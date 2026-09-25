<!-- Do not edit or remove this section -->
<!-- This document exists for non-obvious, error-prone shortcomings in the codebase, the model, or the tooling that an agent cannot figure out by reading the code alone. No architecture overviews, file trees, build commands, or standard behavior. When you encounter something that belongs here, first consider whether a code change could eliminate it and suggest that to the user. Only document it here if it can't be reasonably fixed. -->

---

## Phase rule: prove the model runs first, build the harness later

Pick the architecture by phase. If it is unclear which phase a task is in, or whether a model has graduated, ask the user instead of guessing.

- **Before R&D**: check whether the model is already sold through an API (e.g. Higgsfield), and compare its price at the *same* resolution. Self-hosting pays off only in two cases: the API lacks a capability we need (custom graph, LoRA, fine-tune), or volume is steady enough that the savings cover the fixed costs (active workers, billed failures, maintenance). On 2026-09-25, H3 and LTX failed this test (senai `docs/journal/2026-09-25-self-host-h3-ltx-kalah-dari-higgsfield.md`).

- **R&D phase**: a new model, workflow or custom node that has not yet produced a correct output on a RunPod GPU. The only goal is to show whether it runs. Use the shortest path: a GPU Pod, or a worker from the generic image with `MODEL_DOWNLOAD_POLICY=missing` that downloads weights to container disk at boot.
  - Do not use a network volume, baked weights, an HF mirror or RunPod cached models, and do not pin manifests, change the contract or tune `cache-only` readiness. Each of these turns one smoke test into a rebuild or an infra setup.
  - Traps: `cache-only` is the default, so set `missing` explicitly. Without `AWS_BUCKET_NAME`, the worker boots unready (`OUTPUT_NOT_CONFIGURED`) even when the models are fine.
- **Harness phase**: begins only after the real workflow has run repeatedly on the target GPU class with no errors and no manual fixes between runs, *and* the user has confirmed it is stable. Only then choose weight storage (cached model, HF mirror, network volume or baked), pin `sha256`/`bytes`, and switch to `cache-only` behind the contract.

## Non-obvious constraints

- **No hot-reload**: handler.py, start.sh, and network_volume.py are `ADD`ed into the Docker image at build time (to `/`). Any change requires a full `docker build` before testing with docker-compose.
- **Platform mismatch**: Always build with `--platform linux/amd64` for Runpod deployment. Omitting this on ARM hosts (Apple Silicon) produces images that silently fail on Runpod.
- **No linter or formatter configured**: Follow PEP 8 by convention; there are no pre-commit hooks or CI lint checks.
- **ComfyUI-Manager forced offline**: `start.sh` calls `comfy-manager-set-mode offline` on every boot. Custom nodes cannot be installed at runtime through the Manager UI — they must be baked into the Docker image.
- **Network volume mount point**: Models on a network volume must match the directory structure in `src/extra_model_paths.yaml`. The volume is expected at `/runpod-volume` with a `comfyui/models/` subtree.

## Model type detection (for workflow parsing)

Node types map to model directories — this is ComfyUI domain knowledge not encoded in handler code:

- `UpscaleModelLoader` → `upscale_models`
- `VAELoader` → `vae`
- `UNETLoader`, `UnetLoaderGGUF`, `Hy3DModelLoader` → `diffusion_models`
- `DualCLIPLoader`, `TripleCLIPLoader` → `text_encoders`
- `LoraLoader` → `loras`

## Custom node compatibility

Some custom nodes have dependency conflicts that only surface at runtime:

- **ComfyUI-BrushNet**: Requires `diffusers>=0.29.0`, `accelerate>=0.29.0,<0.32.0`, and `peft>=0.7.0`. Without these exact ranges, you get silent import errors.
- **General pattern**: When a custom node fails with import errors, check its dependency chain and pin versions in the Dockerfile with `uv pip install`.
