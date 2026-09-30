from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_MARKER = "# Install Python runtime dependencies for the handler"
APP_END_MARKER = 'ARG CUSTOM_NODE_MANIFESTS="ltx25.yaml"'
GENERIC_APP_END_MARKER = "# Legacy opt-in targets used by existing release jobs; not built by default."


def read(name):
    return (ROOT / name).read_text()


def _system(generic):
    assert generic.count(SYSTEM_MARKER) == 1
    system, _ = generic.split(SYSTEM_MARKER, 1)
    return system


def test_ltx25_repeats_system_verbatim():
    generic = read("Dockerfile")
    system = _system(generic)
    ltx25 = read("Dockerfile.ltx25")
    assert ltx25.startswith(system)


def test_ltx25_is_a_single_stage():
    ltx25 = read("Dockerfile.ltx25")
    stages = [line for line in ltx25.splitlines() if line.startswith("FROM ")]
    assert len(stages) == 1
    assert stages[0] == "FROM ${BASE_IMAGE} AS base"


def test_ltx25_app_body_matches_generic_except_manifest_default():
    generic = read("Dockerfile")
    ltx25 = read("Dockerfile.ltx25")

    _, generic_rest = generic.split(SYSTEM_MARKER, 1)
    generic_app, _ = generic_rest.split(GENERIC_APP_END_MARKER, 1)
    generic_app = (SYSTEM_MARKER + generic_app).rstrip("\n")

    _, ltx25_rest = ltx25.split(SYSTEM_MARKER, 1)
    ltx25_app = (SYSTEM_MARKER + ltx25_rest).rstrip("\n")

    generic_lines = generic_app.splitlines()
    ltx25_lines = ltx25_app.splitlines()
    generic_arg_idx = next(
        i for i, line in enumerate(generic_lines) if line.startswith('ARG CUSTOM_NODE_MANIFESTS=')
    )
    ltx25_arg_idx = next(
        i for i, line in enumerate(ltx25_lines) if line.startswith('ARG CUSTOM_NODE_MANIFESTS=')
    )
    assert generic_lines[generic_arg_idx] == 'ARG CUSTOM_NODE_MANIFESTS=""'
    assert ltx25_lines[ltx25_arg_idx] == 'ARG CUSTOM_NODE_MANIFESTS="ltx25.yaml"'
    # Everything else in the app body (before and after the ARG default line)
    # must be byte-for-byte identical between the two Dockerfiles.
    assert generic_lines[:generic_arg_idx] == ltx25_lines[:ltx25_arg_idx]
    assert generic_lines[generic_arg_idx + 1 :] == ltx25_lines[ltx25_arg_idx + 1 :]


def test_ltx25_bakes_no_model_weights():
    ltx25 = read("Dockerfile.ltx25")
    assert "https://huggingface.co/" not in ltx25
    assert "sha256sum -c" not in ltx25


def test_ltx25_matches_pinned_comfyui_version():
    generic = read("Dockerfile")
    ltx25 = read("Dockerfile.ltx25")
    generic_version = next(
        line for line in generic.splitlines() if line.startswith("ARG COMFYUI_VERSION=")
    )
    ltx25_version = next(
        line for line in ltx25.splitlines() if line.startswith("ARG COMFYUI_VERSION=")
    )
    assert generic_version == ltx25_version == "ARG COMFYUI_VERSION=0.38.0"
