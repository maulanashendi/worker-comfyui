"""LTX 2.5 Ingredients (IC-LoRA) set: manifest, guard and boot staging."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import guard
from senai_errors import WorkerError
import workflow_models as models

WORKFLOW_DIR = ROOT / 'workflow'
GRAPH = json.loads((WORKFLOW_DIR / 'ltx25-ingredients-v1.json').read_text())
LORA = 'ltx-2.5-22b-ic-lora-ingredients-0.9.safetensors'
LORA_REPO = 'Lightricks/LTX-2.5-22b-IC-LoRA-Ingredients'
LORA_REV = '12040e4091ac2008d3906a594e31a7fb1ab9d546'
LTX_REV = '5e6e71018ee1756ed329b697a7b4aedc934dfce9'
INGREDIENTS_FILES = [
    f'models--Lightricks--LTX-2.5-22b-IC-LoRA-Ingredients/snapshots/{LORA_REV}/{LORA}',
    f'models--Lightricks--LTX-2.5/snapshots/{LTX_REV}/diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors',
    f'models--Lightricks--LTX-2.5/snapshots/{LTX_REV}/text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors',
    f'models--Lightricks--LTX-2.5/snapshots/{LTX_REV}/vae/ltx-2.5-audio-vae-bf16.safetensors',
    f'models--Lightricks--LTX-2.5/snapshots/{LTX_REV}/vae/ltx-2.5-video-vae-bf16.safetensors',
]


def sparse_cache(hf_cache_root, selection, model_root):
    """Sparse (size-only) HF snapshot files for every model of `selection`."""
    plan = models.load_plan(selection, WORKFLOW_DIR, model_root)
    for item in plan.values():
        hf = item['hf']
        org, name = hf['repo'].split('/', 1)
        path = hf_cache_root / f'models--{org}--{name}' / 'snapshots' / hf['revision'] / hf['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('wb') as stream:
            stream.truncate(item.get('bytes') or 0)
    return plan


def verify(tmp_path, monkeypatch, selection, hf_cache_root):
    monkeypatch.setenv('WORKFLOWS', selection)
    monkeypatch.setenv('WORKFLOW_DIR', str(WORKFLOW_DIR))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(tmp_path / 'comfy-models'))
    monkeypatch.setenv('HF_CACHE_ROOT', str(hf_cache_root))
    monkeypatch.setenv('AWS_BUCKET_NAME', 'staging')
    monkeypatch.setenv('SENAI_WORKER_STATE', str(tmp_path / 'state.json'))
    monkeypatch.setenv('WORKFLOW_MODEL_PATHS', str(tmp_path / 'paths.yaml'))
    return models.run_verify()


# --- manifest ---------------------------------------------------------------

def test_manifest_declares_int8_models_and_root_level_lora(tmp_path):
    manifests = models.load_manifests('ltx25-ingredients.yaml', WORKFLOW_DIR, tmp_path)
    paths = sorted(item['path'] for item in manifests.plan.values())
    assert paths == [
        'diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors',
        f'loras/{LORA}',
        'text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors',
        'vae/ltx-2.5-audio-vae-bf16.safetensors',
        'vae/ltx-2.5-video-vae-bf16.safetensors',
    ]
    assert not any('transformer-bf16' in p or 'gemma4_e2b' in p or 'upscaler' in p for p in paths)
    lora = next(i for i in manifests.plan.values() if i['path'].startswith('loras/'))
    assert lora['hf'] == {'repo': LORA_REPO, 'revision': LORA_REV, 'file': LORA}
    assert lora['url'] == f'https://huggingface.co/{LORA_REPO}/resolve/{LORA_REV}/{LORA}'
    assert lora['bytes'] == 1308787472
    assert manifests.custom_nodes == ['ComfyUI-LTXVideo']
    ltx25 = yaml.safe_load((WORKFLOW_DIR / 'ltx25.yaml').read_text())
    ingredients = yaml.safe_load((WORKFLOW_DIR / 'ltx25-ingredients.yaml').read_text())
    assert ingredients['custom_nodes'] == ltx25['custom_nodes']


def test_graph_avoids_forbidden_nodes_and_bf16_transformer():
    class_types = {node['class_type'] for node in GRAPH.values()}
    assert {'LTXICLoRALoaderModelOnly', 'LTXAddVideoICLoRAGuide', 'LTXVCropGuides',
            'RepeatImageBatch', 'CFGGuider', 'SaveVideo'} <= class_types
    assert not class_types & {'LTXVQ8LoraModelLoader', 'GemmaAPITextEncode', 'LTXVLatentUpsampler',
                              'TextGenerateLTX2Prompt', 'LatentUpscaleModelLoader'}
    assert GRAPH['398:384']['inputs']['unet_name'].endswith('comfy-int8-convrot.safetensors')
    assert GRAPH['398:387']['inputs']['clip_name'].endswith('comfy-int8-convrot.safetensors')
    assert GRAPH['395']['inputs']['image'] == 'reference.png'
    assert len([v for v in GRAPH['398:397']['inputs']['sigmas'].split(',')]) == 9  # 8 steps


def test_reference_path_matches_upstream_example():
    # Lightricks/ComfyUI-LTXVideo@5722b53 Ingredients example and the LoRA card: the sheet is a
    # *static video* (still repeated to the clip length) at the output size, added as one
    # IC-LoRA guide at frame 0 with strength 1, and the guide frames are cropped before decode.
    inp = lambda nid: GRAPH[nid]['inputs']
    assert inp('398:351')['input'] == ['395', 0]
    assert [inp('398:351')['resize_type.width'], inp('398:351')['resize_type.height']] == [['398:372', 0], ['398:360', 0]]
    assert inp('398:901') == {'image': ['398:351', 0], 'amount': ['398:378', 1]}
    assert inp('398:356')['length'] == ['398:378', 1]
    guide = inp('398:902')
    assert guide['image'] == ['398:901', 0] and guide['latent'] == ['398:356', 0]
    assert guide['frame_idx'] == 0 and guide['strength'] == 1.0 and guide['crop'] == 'disabled'
    assert guide['latent_downscale_factor'] == ['398:900', 1]
    assert inp('398:900')['strength_model'] == 1.0
    assert inp('398:903')['model'] == ['398:900', 0]
    assert inp('398:903')['positive'] == ['398:902', 0] and inp('398:903')['cfg'] == 1.0
    assert inp('398:377')['video_latent'] == ['398:902', 2]
    assert inp('398:904')['latent'] == ['398:367', 0] and inp('398:374')['samples'] == ['398:904', 2]
    assert inp('398:352')['sampler_name'] == 'euler_ancestral_cfg_pp'
    prompt = inp('398:376')['value']
    assert prompt.startswith('Reference sheet: ') and '\n\nGenerated video: ' in prompt


def test_combined_with_ltx25_shares_models_without_conflict(tmp_path):
    manifests = models.load_manifests('ltx25.yaml,ltx25-ingredients.yaml', WORKFLOW_DIR, tmp_path)
    assert len(manifests.plan) == 7
    assert LORA in manifests.declared_model_names


def test_hf_category_dir():
    assert models.hf_category_dir('loras/x.safetensors', 'x.safetensors') == '.'
    assert models.hf_category_dir('vae/x.safetensors', 'vae/x.safetensors') == 'vae'
    assert models.hf_category_dir('loras/x.safetensors', 'weights/x.safetensors') == 'weights'
    assert models.hf_category_dir('loras/a/x.safetensors', 'b/a/x.safetensors') == 'b'
    for bad in ('y.safetensors', 'vae/y.safetensors', '../x.safetensors', '/x.safetensors'):
        with pytest.raises(ValueError):
            models.hf_category_dir('loras/x.safetensors', bad)


def test_resolve_hf_model_rejects_name_mismatch():
    with pytest.raises(ValueError):
        models.resolve_hf_model({'path': 'loras/a.safetensors', 'hf': {'repo': 'o/r', 'file': 'b.safetensors'}})


# --- verify + extra_model_paths ------------------------------------------------

def comfy_search_paths(config):
    """Mirror ComfyUI v0.38.0 utils/extra_config.load_extra_path_config."""
    result = {}
    for conf in config.values():
        conf = dict(conf)
        base = conf.pop('base_path', None)
        for category, value in conf.items():
            for line in value.split('\n'):
                if line:
                    result.setdefault(category, []).append(os.path.normpath(os.path.join(base, line)))
    return result


@pytest.mark.parametrize('selection', ['ltx25-ingredients.yaml', 'ltx25.yaml,ltx25-ingredients.yaml'])
def test_verify_ready_and_lora_visible_under_loras(tmp_path, monkeypatch, selection):
    hf_cache_root = tmp_path / 'hf-cache'
    plan = sparse_cache(hf_cache_root, selection, tmp_path / 'comfy-models')
    state = verify(tmp_path, monkeypatch, selection, hf_cache_root)
    assert state['ready'] is True, state
    assert state['models']['present'] == len(plan)
    assert LORA in state['declared_model_names']

    search = comfy_search_paths(yaml.safe_load((tmp_path / 'paths.yaml').read_text()))
    for item in plan.values():
        category, name = item['path'].split('/', 1)
        assert any((Path(folder) / name).is_file() for folder in search[category]), item['path']
    lora_snapshot = hf_cache_root / 'models--Lightricks--LTX-2.5-22b-IC-LoRA-Ingredients' / 'snapshots' / LORA_REV
    assert str(lora_snapshot) in search['loras']


def test_existing_ltx25_paths_unchanged(tmp_path, monkeypatch):
    hf_cache_root = tmp_path / 'hf-cache'
    sparse_cache(hf_cache_root, 'ltx25.yaml', tmp_path / 'comfy-models')
    verify(tmp_path, monkeypatch, 'ltx25.yaml', hf_cache_root)
    config = yaml.safe_load((tmp_path / 'paths.yaml').read_text())
    section = config['hf_lightricks_ltx_2_5']
    assert section['diffusion_models'] == 'diffusion_models/'
    assert section['vae'] == 'vae/'


# --- guard -------------------------------------------------------------------

def booted_state(tmp_path, monkeypatch):
    hf_cache_root = tmp_path / 'hf-cache'
    sparse_cache(hf_cache_root, 'ltx25-ingredients.yaml', tmp_path / 'comfy-models')
    state = verify(tmp_path, monkeypatch, 'ltx25-ingredients.yaml', hf_cache_root)
    return frozenset(state['allowed_class_types']), frozenset(state['declared_model_names'])


def test_graph_passes_guard(tmp_path, monkeypatch):
    allowed, declared = booted_state(tmp_path, monkeypatch)
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    envelope = guard.parse_envelope({
        'protocol': 'senai-worker/1',
        'workflow': GRAPH,
        'inputs': [{'name': 'reference.png', 'media_type': 'image/png', 'url': 'https://example.com/r.png'}],
        'trace': {'generation_id': 'g', 'attempt': 1, 'binding_alias': 'b', 'binding_revision': 'r',
                  'adapter': 'a', 'workflow_id': 'ltx25-ingredients-v1', 'graph_sha256': '0' * 64},
        'limits': {'deadline_at': (now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')},
    }, now=now)
    guard.check_allowlist(envelope.workflow, allowed)
    guard.check_model_references(envelope.workflow, declared)


def test_guard_rejects_q8_loader_and_bf16_transformer(tmp_path, monkeypatch):
    allowed, declared = booted_state(tmp_path, monkeypatch)
    q8 = json.loads(json.dumps(GRAPH))
    q8['398:900']['class_type'] = 'LTXVQ8LoraModelLoader'
    with pytest.raises(WorkerError) as exc:
        guard.check_allowlist(q8, allowed)
    assert exc.value.code == 'NODE_NOT_ALLOWED'

    bf16 = json.loads(json.dumps(GRAPH))
    bf16['398:384']['inputs']['unet_name'] = 'ltx-2.5-22b-distilled-transformer-bf16.safetensors'
    with pytest.raises(WorkerError) as exc:
        guard.check_model_references(bf16, declared)
    assert exc.value.code == 'MODEL_NOT_IN_MANIFEST'


def test_ingredients_only_set_rejects_i2v_enhancer_graph(tmp_path, monkeypatch):
    allowed, _ = booted_state(tmp_path, monkeypatch)
    i2v = json.loads((WORKFLOW_DIR / 'ltx25-i2v-v1.json').read_text())
    with pytest.raises(WorkerError) as exc:
        guard.check_allowlist(i2v, allowed)
    assert exc.value.code == 'NODE_NOT_ALLOWED'


# --- staging ------------------------------------------------------------------

def test_stage_files_lists_only_selected_set(tmp_path):
    hf_cache_root = tmp_path / 'hf-cache'
    sparse_cache(hf_cache_root, 'ltx25.yaml,ltx25-ingredients.yaml', tmp_path / 'm')
    plan = models.load_plan('ltx25-ingredients.yaml', WORKFLOW_DIR, tmp_path / 'm')
    assert models.stage_files(plan, hf_cache_root) == INGREDIENTS_FILES
    full = models.load_plan('ltx25.yaml,ltx25-ingredients.yaml', WORKFLOW_DIR, tmp_path / 'm')
    assert len(models.stage_files(full, hf_cache_root)) == 7


def test_stage_files_skips_missing_and_includes_branch_ref(tmp_path):
    hf_cache_root = tmp_path / 'hf-cache'
    repo = hf_cache_root / 'models--o--r'
    (repo / 'refs').mkdir(parents=True)
    (repo / 'refs' / 'main').write_text('a' * 40)
    snapshot = repo / 'snapshots' / ('a' * 40) / 'vae'
    snapshot.mkdir(parents=True)
    (snapshot / 'x.safetensors').write_bytes(b'1')
    plan = {
        Path('/m/vae/x.safetensors'): {'path': 'vae/x.safetensors', 'hf': {'repo': 'o/r', 'revision': 'main', 'file': 'vae/x.safetensors'}},
        Path('/m/vae/y.safetensors'): {'path': 'vae/y.safetensors', 'hf': {'repo': 'o/r', 'revision': 'main', 'file': 'vae/y.safetensors'}},
        Path('/m/vae/z.safetensors'): {'path': 'vae/z.safetensors', 'url': 'https://example.com/z.safetensors'},
    }
    assert models.stage_files(plan, hf_cache_root) == [
        'models--o--r/refs/main', f'models--o--r/snapshots/{"a" * 40}/vae/x.safetensors']


def stage_list_cli(env):
    return subprocess.run([sys.executable, str(ROOT / 'src/workflow_models.py'), '--stage-list'],
                          env={**os.environ, **env}, capture_output=True, text=True)


def test_stage_list_cli(tmp_path):
    hf_cache_root = tmp_path / 'hf-cache'
    sparse_cache(hf_cache_root, 'ltx25-ingredients.yaml', tmp_path / 'm')
    env = {'WORKFLOWS': 'ltx25-ingredients.yaml', 'WORKFLOW_DIR': str(WORKFLOW_DIR),
           'HF_CACHE_ROOT': str(hf_cache_root), 'COMFY_MODEL_ROOT': str(tmp_path / 'm')}
    result = stage_list_cli(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == INGREDIENTS_FILES
    assert stage_list_cli({**env, 'HF_CACHE_ROOT': str(tmp_path / 'empty')}).returncode != 0
    assert stage_list_cli({**env, 'WORKFLOWS': 'nope.yaml'}).returncode != 0


STAGING_FAKE_PYTHON = '''#!/usr/bin/env python3
import os,sys,time
from pathlib import Path
if '-c' in sys.argv:
    print('OK: mock GPU'); sys.exit(0)
if any('workflow_models.py' in arg for arg in sys.argv):
    if '--stage-list' in sys.argv:
        os.execv(os.environ['REAL_PYTHON'], [os.environ['REAL_PYTHON'], os.environ['REAL_WORKFLOW_MODELS'], '--stage-list'])
    if '--verify' in sys.argv:
        Path(os.environ['TEST_ROOT'], 'verify_hf_root').write_text(os.environ['HF_CACHE_ROOT'])
    sys.exit(0)
role = 'comfy' if any('main.py' in arg for arg in sys.argv) else 'handler'
Path(os.environ['TEST_ROOT'], role+'.pid').write_text(str(os.getpid()))
if role == 'handler':
    time.sleep(.3); sys.exit(0)
time.sleep(60)
'''


def run_start_staging(tmp_path, workflows):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'python'
    fake.write_text('#!' + sys.executable + '\n' + STAGING_FAKE_PYTHON.split('\n', 1)[1])
    fake.chmod(0o755)
    (bin_dir / 'python3').symlink_to(fake)
    hf_cache_root = tmp_path / 'bucket' / 'hub'
    # The bucket holds both sets plus an unrelated repo; tiny files keep the copy fast.
    plan = models.load_plan('ltx25.yaml,ltx25-ingredients.yaml', WORKFLOW_DIR, tmp_path / 'm')
    for item in plan.values():
        hf = item['hf']
        org, name = hf['repo'].split('/', 1)
        path = hf_cache_root / f'models--{org}--{name}' / 'snapshots' / hf['revision'] / hf['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
    other = hf_cache_root / 'models--Comfy-Org--MiniMax-H3' / 'snapshots' / ('b' * 40) / 'h3.safetensors'
    other.parent.mkdir(parents=True)
    other.write_bytes(b'x')
    stage_dir = tmp_path / 'stage' / 'hub'
    env = {
        **os.environ, 'PATH': str(bin_dir) + ':' + os.environ['PATH'], 'TEST_ROOT': str(tmp_path),
        'REAL_PYTHON': sys.executable, 'REAL_WORKFLOW_MODELS': str(ROOT / 'src/workflow_models.py'),
        'PUBLIC_KEY': '', 'SENAI_TRANSPORT': 'cloudrun', 'WORKFLOWS': workflows,
        'WORKFLOW_DIR': str(WORKFLOW_DIR), 'COMFY_MODEL_ROOT': str(tmp_path / 'm'),
        'HF_CACHE_ROOT': str(hf_cache_root), 'SENAI_HF_STAGE_DIR': str(stage_dir),
        'COMFY_PID_FILE': str(tmp_path / 'comfyui.pid'),
        'SENAI_BOOT_TIMELINE': str(tmp_path / 'timeline'),
        'SENAI_WORKER_STATE': str(tmp_path / 'state.json'),
        'WORKFLOW_MODEL_PATHS': str(tmp_path / 'paths.yaml'),
    }
    proc = subprocess.run(['bash', str(ROOT / 'src/start.sh')], env=env, capture_output=True, text=True, timeout=30)
    staged = sorted(str(p.relative_to(stage_dir)) for p in stage_dir.rglob('*') if p.is_file())
    return proc, staged, (tmp_path / 'verify_hf_root').read_text()


def test_start_stages_only_selected_set(tmp_path):
    proc, staged, verify_root = run_start_staging(tmp_path, 'ltx25-ingredients.yaml')
    assert 'staging 5 manifest files' in proc.stdout, proc.stdout + proc.stderr
    assert staged == INGREDIENTS_FILES
    assert verify_root == str(tmp_path / 'stage' / 'hub')


def test_start_falls_back_to_copy_all_when_list_fails(tmp_path):
    proc, staged, verify_root = run_start_staging(tmp_path, 'missing.yaml')
    assert 'manifest file list unavailable' in proc.stderr, proc.stdout + proc.stderr
    assert len(staged) == 8  # 7 LTX files + the unrelated repo
    assert verify_root == str(tmp_path / 'stage' / 'hub')
