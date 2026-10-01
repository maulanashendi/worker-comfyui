# Workflow manifests

Manifests are **v2** (`version: 2`): every model is declared explicitly, with
an `hf: {repo, revision, file}` pin used both to build the download URL and to
locate the model in a RunPod Hugging Face cached-model snapshot at boot
(`workflow_models.py --verify`, see `docs/senai-worker-internals.md` §5 and
`contract/senai-worker-1/CONTRACT.md` §7). `version: 1` manifests are still
accepted for backward compatibility, but no set here uses that shape anymore.

`WORKFLOWS=ltx25.yaml` selects the Senai LTX 2.5 stack.

The three JSON API graphs were copied unchanged from
`senai/backend/src/senai/modules/providers/runpod/workflows/` on 2026-09-22.
Their upstream contract pins `vavo/LTX2.5-serverless` revision
`5e6a88f116c6611420a7073a74cea684870ef01c`. Models come from two Hugging Face
repos: `Lightricks/LTX-2.5` (5 files) and `Comfy-Org/gemma-4` (1 file).

The YAML prepares assets; the request's `input.workflow` remains the execution
graph. Adding a workflow requires its API JSON and a manifest listing all model
assets, plus installing any required custom nodes in the image. Multiple
manifests can share a model only when its URL and checksum agree.

Revisions are pinned to `main` and `sha256`/`bytes` are unset until the
coordinator runs `scripts/pin-hf-manifest.py` against the real Hugging Face
repos. See
[customization](../docs/customization.md#generic-workflow-selection-and-model-cache) and
[deployment](../docs/deployment.md#generic-worker-prepare-assets-separately-from-serving).

## Additional workflows

`WORKFLOWS=minimax-h3.yaml` declares the 8 MiniMax H3 model assets (6 from
`Comfy-Org/MiniMax-H3`, 2 from `Comfy-Org/SDPose`) explicitly — `workflows: []`
because no API graph has been exported for this set yet. The reference editor
export this was derived from lives at
`reference/video_minimax_h3_r2v.editor.json`; it is not a handler payload and
is not read by `workflow_models.py` (editor JSON needs an exported API graph
before it can be sent through the protocol). No custom node repository is
required for this set. See the generic cache preparation instructions in the
deployment guide.

`WORKFLOWS=ltx25-ingredients.yaml` selects the LTX 2.5 Ingredients IC-LoRA set
(`ltx25-ingredients-v1.json`): reference sheet + prompt, int8 transformer and
text encoder plus the root-level LoRA from
`Lightricks/LTX-2.5-22b-IC-LoRA-Ingredients`. An `hf.file` may sit in a
different folder than `path`'s category (here the repo root vs `loras/`) as
long as both end in the same model name; see `docs/cloudrun.md`.

`WORKFLOWS=ltx25-motion.yaml` selects LTX 2.5 motion transfer
(`ltx25-motion-v1.json`): driving video + start image + prompt. DWPose pose frames
guide the Union Control IC-LoRA on the int8 transformer. It needs the
`comfyui_controlnet_aux` custom node in the image
(`CUSTOM_NODE_MANIFESTS=ltx25.yaml,ltx25-motion.yaml`). That node's
`pip:` list replaces its requirements.txt. The `annotators/` models are linked
for controlnet_aux at boot; see `docs/cloudrun.md`.
