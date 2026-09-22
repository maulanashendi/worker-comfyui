# Workflow manifests

`WORKFLOW_MANIFESTS=ltx25.yaml` selects the initial Senai LTX 2.5 stack.

The three JSON API graphs were copied unchanged from
`senai/backend/src/senai/modules/providers/runpod/workflows/` on 2026-09-22.
Their upstream contract pins `vavo/LTX2.5-serverless` revision
`5e6a88f116c6611420a7073a74cea684870ef01c`. Download locations follow that
checkout's `src/bootstrap_ltx25.sh`.

The YAML prepares assets; the request's `input.workflow` remains the execution
graph. Adding a workflow requires its API JSON and a manifest listing all model
assets, plus installing any required custom nodes in the image. Multiple
manifests can share a model only when its URL and checksum agree.

The initial URLs use upstream `main`; real downloads and hashes have not been
verified. Pin revisions and `sha256` values after commissioning. See
[customization](../docs/customization.md#generic-workflow-selection-and-model-cache) and
[deployment](../docs/deployment.md#generic-worker-prepare-assets-separately-from-serving).

## Additional workflows

`WORKFLOWS=minimax-h3.yaml` (or `video_minimax_h3_r2v.json`) reads the five model
assets directly from editor metadata. It never selects LTX assets. Editor JSON
is not a handler payload: export an API graph before inference. The YAML is
only a reference and has no duplicated model URLs. See the generic cache
preparation instructions in the deployment guide.
