from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_MARKER = "# Install Python runtime dependencies for the handler"
APP_END_MARKER = 'ARG CUSTOM_NODE_MANIFESTS="senfers1.yaml"'
GENERIC_APP_END_MARKER = "# Legacy opt-in targets used by existing release jobs; not built by default."


def read(name):
    return (ROOT / name).read_text()


def _system(generic):
    assert generic.count(SYSTEM_MARKER) == 1
    system, _ = generic.split(SYSTEM_MARKER, 1)
    return system


def test_senfers1_repeats_system_verbatim():
    generic = read("Dockerfile")
    system = _system(generic)
    senfers1 = read("Dockerfile.senfers1")
    assert senfers1.startswith(system)


def test_senfers1_is_a_single_stage():
    senfers1 = read("Dockerfile.senfers1")
    stages = [line for line in senfers1.splitlines() if line.startswith("FROM ")]
    assert len(stages) == 1
    assert stages[0] == "FROM ${BASE_IMAGE} AS base"


def test_senfers1_app_body_matches_generic_except_manifest_default():
    generic = read("Dockerfile")
    senfers1 = read("Dockerfile.senfers1")

    _, generic_rest = generic.split(SYSTEM_MARKER, 1)
    generic_app, _ = generic_rest.split(GENERIC_APP_END_MARKER, 1)
    generic_app = (SYSTEM_MARKER + generic_app).rstrip("\n")

    _, senfers1_rest = senfers1.split(SYSTEM_MARKER, 1)
    senfers1_app = (SYSTEM_MARKER + senfers1_rest).rstrip("\n")

    generic_lines = generic_app.splitlines()
    senfers1_lines = senfers1_app.splitlines()
    generic_arg_idx = next(
        i for i, line in enumerate(generic_lines) if line.startswith('ARG CUSTOM_NODE_MANIFESTS=')
    )
    senfers1_arg_idx = next(
        i for i, line in enumerate(senfers1_lines) if line.startswith('ARG CUSTOM_NODE_MANIFESTS=')
    )
    assert generic_lines[generic_arg_idx] == 'ARG CUSTOM_NODE_MANIFESTS=""'
    assert senfers1_lines[senfers1_arg_idx] == 'ARG CUSTOM_NODE_MANIFESTS="senfers1.yaml"'
    # Everything else in the app body (before and after the ARG default line)
    # must be byte-for-byte identical between the two Dockerfiles.
    assert generic_lines[:generic_arg_idx] == senfers1_lines[:senfers1_arg_idx]
    assert generic_lines[generic_arg_idx + 1 :] == senfers1_lines[senfers1_arg_idx + 1 :]


def test_senfers1_bakes_no_model_weights():
    senfers1 = read("Dockerfile.senfers1")
    assert "https://huggingface.co/" not in senfers1
    assert "sha256sum -c" not in senfers1


def test_senfers1_matches_pinned_comfyui_version():
    generic = read("Dockerfile")
    senfers1 = read("Dockerfile.senfers1")
    generic_version = next(
        line for line in generic.splitlines() if line.startswith("ARG COMFYUI_VERSION=")
    )
    senfers1_version = next(
        line for line in senfers1.splitlines() if line.startswith("ARG COMFYUI_VERSION=")
    )
    assert generic_version == senfers1_version == "ARG COMFYUI_VERSION=0.38.0"


TORCH_ARG_DEFAULTS = {
    "TORCH_INDEX_URL": "https://download.pytorch.org/whl/cu128",
    "TORCH_VERSION": "2.11.0",
    "TORCHVISION_VERSION": "0.26.0",
    "TORCHAUDIO_VERSION": "2.11.0",
}


def test_torch_build_args_default_to_cu128_in_both_dockerfiles():
    # cu128 must stay the default: RunPod hosts run driver 570/575, where a
    # cu130 torch fails CUDA init. cu130 is opt-in via --build-arg only.
    for name in ("Dockerfile", "Dockerfile.senfers1"):
        lines = read(name).splitlines()
        for arg, default in TORCH_ARG_DEFAULTS.items():
            assert lines.count(f"ARG {arg}={default}") == 1, (name, arg)
        text = read(name)
        assert "--index-url ${TORCH_INDEX_URL}" in text
        assert "torch==${TORCH_VERSION}" in text
        assert "torchvision==${TORCHVISION_VERSION}" in text
        assert "torchaudio==${TORCHAUDIO_VERSION}" in text
