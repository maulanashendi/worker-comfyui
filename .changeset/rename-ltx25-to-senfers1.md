---
"worker-comfyui": minor
---

Rename the self-hosted LTX 2.5 manifest/workflow set from `ltx25*` to `senfers1*`
(Senfers 1.0, user decision 2026-10-03): `workflow/ltx25*.yaml` and
`workflow/ltx25-*.json` are now `workflow/senfers1*.yaml` /
`workflow/senfers1-*.json`, `Dockerfile.ltx25` is now `Dockerfile.senfers1`, and
`SaveVideo.filename_prefix` in those graphs moved from `video/LTX-2.5_*` to
`video/Senfers1_*`. `WORKFLOWS=ltx25.yaml` and the other old manifest names
still resolve to the renamed files for one release
(`workflow_models.resolve_manifest_name`), so an image built before a
deployment's `WORKFLOWS`/`CUSTOM_NODE_MANIFESTS` env vars are flipped to the
new names keeps booting. Higgsfield's `ltx-2.5-fast`/`ltx-2.5-pro` and the
unrelated vavo `runpod-ltx25` worker/contract are untouched.
