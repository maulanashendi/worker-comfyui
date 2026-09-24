from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MARKER = '# Legacy opt-in targets used by existing release jobs; not built by default.'


def read(name):
    return (ROOT / name).read_text()


def test_base_stage_is_repeated_verbatim_from_generic_dockerfile():
    generic = read('Dockerfile')
    assert generic.count(MARKER) == 1
    assert read('Dockerfile.minimax-h3').startswith(generic.split(MARKER)[0])


def test_last_stage_bakes_exactly_the_manifest_models():
    h3 = read('Dockerfile.minimax-h3')
    stages = [line for line in h3.splitlines() if line.startswith('FROM ')]
    assert stages[-1] == 'FROM base AS minimax-h3'

    models = yaml.safe_load(read('workflow/minimax-h3.yaml'))['models']
    assert h3.count('https://huggingface.co/') == len(models)
    for model in models:
        hf = model['hf']
        assert f"https://huggingface.co/{hf['repo']}/resolve/{hf['revision']}/{hf['file']}" in h3
        assert f"{model['sha256']}  {model['path']}" in h3
