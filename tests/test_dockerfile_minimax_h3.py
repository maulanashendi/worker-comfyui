from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_MARKER = "# Install Python runtime dependencies for the handler"
APP_END_MARKER = "# Legacy opt-in targets used by existing release jobs; not built by default."


def read(name):
    return (ROOT / name).read_text()


def _system_and_app(generic):
    assert generic.count(SYSTEM_MARKER) == 1
    assert generic.count(APP_END_MARKER) == 1
    system, rest = generic.split(SYSTEM_MARKER, 1)
    app_body, _ = rest.split(APP_END_MARKER, 1)
    # Normalize the trailing newline so a stray blank line at the join point
    # (present in the generic Dockerfile right before its next stage marker)
    # doesn't make an otherwise-verbatim copy fail a strict endswith check.
    app = (SYSTEM_MARKER + app_body).rstrip("\n") + "\n"
    return system, app


def test_h3_repeats_system_and_app_verbatim():
    generic = read("Dockerfile")
    system, app = _system_and_app(generic)
    h3 = read("Dockerfile.minimax-h3")
    assert h3.startswith(system)
    assert h3.endswith(app)


def test_h3_is_a_single_stage():
    h3 = read("Dockerfile.minimax-h3")
    stages = [line for line in h3.splitlines() if line.startswith("FROM ")]
    assert len(stages) == 1
    assert stages[0] == "FROM ${BASE_IMAGE} AS base"


def test_model_weights_are_baked_before_worker_code():
    h3 = read("Dockerfile.minimax-h3")
    lines = h3.splitlines()
    copy_requirements_idx = next(i for i, line in enumerate(lines) if line.startswith("COPY requirements.txt"))
    wget_indices = [i for i, line in enumerate(lines) if line.lstrip().startswith("RUN wget")]
    assert wget_indices, "expected at least one model RUN wget line"
    assert all(i < copy_requirements_idx for i in wget_indices)


def test_bakes_exactly_the_manifest_models():
    h3 = read("Dockerfile.minimax-h3")
    models = yaml.safe_load(read("workflow/minimax-h3.yaml"))["models"]
    assert h3.count("https://huggingface.co/") == len(models)
    for model in models:
        hf = model["hf"]
        assert f"https://huggingface.co/{hf['repo']}/resolve/{hf['revision']}/{hf['file']}" in h3
        assert f"{model['sha256']}  {model['path']}" in h3
