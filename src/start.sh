#!/usr/bin/env bash

set -eo pipefail

WORKER_ROOT="${WORKER_ROOT:-/}"
COMFY_ROOT="${COMFY_ROOT:-/comfyui}"
if [ -d /runpod-volume ]; then
    default_model_root=/runpod-volume/models
else
    default_model_root="$COMFY_ROOT/models"
fi
export COMFY_MODEL_ROOT="${COMFY_MODEL_ROOT:-$default_model_root}"
export COMFY_PID_FILE="${COMFY_PID_FILE:-/tmp/comfyui.pid}"
export REFRESH_WORKER="${REFRESH_WORKER:-true}"

# A checklist is an explicit standalone mode: never start a worker without models.
if [ "${MODEL_DOWNLOAD_CHECK_ONLY:-false}" = "true" ]; then
    exec python3 "$WORKER_ROOT/workflow_models.py" --check
fi

if [ "${PREPARE_MODELS_ONLY:-false}" = "true" ]; then
    exec python3 "$WORKER_ROOT/workflow_models.py"
fi

comfy_pid=""
handler_pid=""
cleanup() {
    trap - EXIT TERM INT
    for pid in "$handler_pid" "$comfy_pid"; do
        [ -z "$pid" ] || kill -TERM "$pid" 2>/dev/null || true
    done
    # Bound shutdown even if a child ignores SIGTERM.
    for attempt in {1..50}; do
        alive=false
        for pid in "$handler_pid" "$comfy_pid"; do
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then alive=true; fi
        done
        [ "$alive" = true ] || break
        sleep 0.1
    done
    for pid in "$handler_pid" "$comfy_pid"; do
        [ -z "$pid" ] || kill -KILL "$pid" 2>/dev/null || true
        [ -z "$pid" ] || wait "$pid" 2>/dev/null || true
    done
    rm -f "$COMFY_PID_FILE"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# Start SSH server if PUBLIC_KEY is set (enables remote access and dev-sync.sh)
if [ -n "$PUBLIC_KEY" ]; then
    mkdir -p ~/.ssh
    echo "$PUBLIC_KEY" > ~/.ssh/authorized_keys
    chmod 700 ~/.ssh
    chmod 600 ~/.ssh/authorized_keys

    # Generate host keys if they don't exist (removed during image build for security)
    for key_type in rsa ecdsa ed25519; do
        key_file="/etc/ssh/ssh_host_${key_type}_key"
        if [ ! -f "$key_file" ]; then
            ssh-keygen -t "$key_type" -f "$key_file" -q -N ''
        fi
    done

    service ssh start && echo "worker-comfyui: SSH server started" || echo "worker-comfyui: SSH server could not be started" >&2
fi

# Use libtcmalloc for better memory management
TCMALLOC="$(ldconfig -p | grep -Po "libtcmalloc.so.\d" | head -n 1 || true)"
if [ -n "$TCMALLOC" ]; then export LD_PRELOAD="${TCMALLOC}"; fi

# ---------------------------------------------------------------------------
# GPU pre-flight check
# Verify that the GPU is accessible before starting ComfyUI. If PyTorch
# cannot initialize CUDA the worker will never be able to process jobs,
# so we fail fast with an actionable error message.
# ---------------------------------------------------------------------------
echo "worker-comfyui: Checking GPU availability..."
if ! GPU_CHECK=$(python3 -c "
import torch
try:
    torch.cuda.init()
    name = torch.cuda.get_device_name(0)
    cap = torch.cuda.get_device_capability(0)
    # Launch a real kernel. The driver-only calls above succeed even when this
    # PyTorch build has no compiled kernels for the GPU architecture (e.g. an
    # older torch on a newer GPU). Without this, the worker boots, ComfyUI dies
    # on the first GPU op, and it surfaces as the misleading 'server not
    # reachable' error instead of a clear cause here.
    _ = (torch.zeros(8, device='cuda') + 1).sum().item()
    torch.cuda.synchronize()
    print(f'OK: {name} (sm_{cap[0]}{cap[1]}), torch {torch.__version__}, cuda {torch.version.cuda}')
except Exception as e:
    print(f'FAIL: {e}')
    exit(1)
" 2>&1); then
    echo "worker-comfyui: GPU is not available or incompatible with this PyTorch build:"
    echo "worker-comfyui: $GPU_CHECK"
    echo "worker-comfyui: A 'no kernel image is available' error means this torch build"
    echo "worker-comfyui: lacks kernels for this GPU. Otherwise the GPU may not be"
    echo "worker-comfyui: properly initialized — please contact RunPod support."
    exit 1
fi
echo "worker-comfyui: GPU available — $GPU_CHECK"

python3 "$WORKER_ROOT/workflow_models.py" &
handler_pid=$!
wait "$handler_pid"
handler_pid=""

# Ensure ComfyUI-Manager runs in offline network mode inside the container
comfy-manager-set-mode offline || echo "worker-comfyui - Could not set ComfyUI-Manager network_mode" >&2

echo "worker-comfyui: Starting ComfyUI"

# Allow operators to tweak verbosity; default is DEBUG.
: "${COMFY_LOG_LEVEL:=DEBUG}"

comfy_args=(--disable-auto-launch --disable-metadata --verbose "$COMFY_LOG_LEVEL" --log-stdout)
if [ -n "${WORKFLOWS:-${WORKFLOW_MANIFESTS:-}}" ]; then
    comfy_args+=(--extra-model-paths-config "${WORKFLOW_MODEL_PATHS:-/tmp/workflow_model_paths.yaml}")
fi
handler_args=()
if [ "${SERVE_API_LOCALLY:-false}" = "true" ]; then
    comfy_args+=(--listen)
    handler_args+=(--rp_serve_api --rp_api_host=0.0.0.0)
fi
python -u "$COMFY_ROOT/main.py" "${comfy_args[@]}" &
comfy_pid=$!
echo "$comfy_pid" > "$COMFY_PID_FILE"
echo "worker-comfyui: Starting RunPod Handler"
python -u "$WORKER_ROOT/handler.py" "${handler_args[@]}" "$@" &
handler_pid=$!

# Exit when either service exits; the EXIT trap stops the other child.
status=0
finished=""
wait -n -p finished "$comfy_pid" "$handler_pid" || status=$?
if [ "$finished" = "$comfy_pid" ] && [ "$status" = 0 ]; then status=1; fi
exit "$status"
