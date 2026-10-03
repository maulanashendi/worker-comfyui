"""LTX 2.5 motion transfer (IC-LoRA Union Control, pose) set: manifest, guard,
video input, annotator links, custom-node pip list and boot staging."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import guard
from senai_errors import WorkerError
import workflow_models as models

from tests.test_senfers1_ingredients import STAGING_FAKE_PYTHON, comfy_search_paths, sparse_cache, verify

WORKFLOW_DIR = ROOT / 'workflow'
GRAPH = json.loads((WORKFLOW_DIR / 'senfers1-motion-v1.json').read_text())
LTX_REV = '5e6e71018ee1756ed329b697a7b4aedc934dfce9'
LORA = 'ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors'
LORA_REPO = 'Lightricks/LTX-2.3-22b-IC-LoRA-Union-Control'
LORA_REV = 'b4d1c4d8c9e544e9bbbd6811bb4363708b6093ff'
POSE = ('hr16/DWPose-TorchScript-BatchSize5', '359d662a9b33b73f6d0f21732baf8845f17bb4be', 'dw-ll_ucoco_384_bs5.torchscript.pt')
BBOX = ('hr16/yolox-onnx', 'a124b32c3b7c5cebda1c7cd96178f0f9d2050125', 'yolox_l.torchscript.pt')
CNAUX_REV = '0cd290477128d42cdc3e76a826a402d866e8c684'
MOTION_FILES = [
    f'models--Lightricks--LTX-2.3-22b-IC-LoRA-Union-Control/snapshots/{LORA_REV}/{LORA}',
    f'models--Lightricks--LTX-2.5/snapshots/{LTX_REV}/diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors',
    f'models--Lightricks--LTX-2.5/snapshots/{LTX_REV}/text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors',
    f'models--Lightricks--LTX-2.5/snapshots/{LTX_REV}/vae/ltx-2.5-audio-vae-bf16.safetensors',
    f'models--Lightricks--LTX-2.5/snapshots/{LTX_REV}/vae/ltx-2.5-video-vae-bf16.safetensors',
    f'models--hr16--DWPose-TorchScript-BatchSize5/snapshots/{POSE[1]}/{POSE[2]}',
    f'models--hr16--yolox-onnx/snapshots/{BBOX[1]}/{BBOX[2]}',
]
MOTION_BYTES = 39721315609
MP4_HEAD = b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'


# --- manifest + graph ---------------------------------------------------------

def test_manifest_declares_int8_models_lora_and_annotators(tmp_path):
    manifests = models.load_manifests('senfers1-motion.yaml', WORKFLOW_DIR, tmp_path)
    paths = sorted(item['path'] for item in manifests.plan.values())
    assert paths == [
        f'annotators/{POSE[2]}',
        f'annotators/{BBOX[2]}',
        'diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors',
        f'loras/{LORA}',
        'text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors',
        'vae/ltx-2.5-audio-vae-bf16.safetensors',
        'vae/ltx-2.5-video-vae-bf16.safetensors',
    ]
    assert not any('bf16' in p for p in paths if not p.startswith('vae/'))
    assert sum(item['bytes'] for item in manifests.plan.values()) == MOTION_BYTES
    by_file = {item['hf']['file']: item['hf'] for item in manifests.plan.values()}
    assert by_file[LORA] == {'repo': LORA_REPO, 'revision': LORA_REV, 'file': LORA}
    assert by_file[POSE[2]] == dict(zip(('repo', 'revision', 'file'), POSE))
    assert by_file[BBOX[2]] == dict(zip(('repo', 'revision', 'file'), BBOX))
    assert manifests.custom_nodes == ['ComfyUI-LTXVideo', 'comfyui_controlnet_aux']
    doc = yaml.safe_load((WORKFLOW_DIR / 'senfers1-motion.yaml').read_text())
    senfers1 = yaml.safe_load((WORKFLOW_DIR / 'senfers1.yaml').read_text())
    assert doc['custom_nodes'][0] == senfers1['custom_nodes'][0]
    aux = doc['custom_nodes'][1]
    assert aux['revision'] == CNAUX_REV
    assert not any(s.startswith(('onnxruntime', 'mediapipe', 'opencv-contrib')) for s in aux['pip'])


def test_graph_shape_and_forbidden_nodes():
    class_types = {node['class_type'] for node in GRAPH.values()}
    assert {'LoadVideo', 'Video Slice', 'GetVideoComponents', 'ImageFromBatch', 'DWPreprocessor',
            'LTXICLoRALoaderModelOnly', 'LTXAddVideoICLoRAGuide', 'LTXVImgToVideoInplace',
            'LTXVCropGuides', 'CFGGuider', 'SaveVideo'} <= class_types
    assert not class_types & {'LTXVQ8LoraModelLoader', 'GemmaAPITextEncode', 'LTXVLatentUpsampler',
                              'TextGenerateLTX2Prompt', 'LatentUpscaleModelLoader', 'VHS_LoadVideo'}
    assert GRAPH['398:384']['inputs']['unet_name'].endswith('comfy-int8-convrot.safetensors')
    assert GRAPH['398:387']['inputs']['clip_name'].endswith('comfy-int8-convrot.safetensors')
    assert GRAPH['396']['inputs']['file'] == 'driving.mp4'
    assert GRAPH['395']['inputs']['image'] == 'start.png'
    dw = GRAPH['398:913']['inputs']
    assert (dw['bbox_detector'], dw['pose_estimator']) == (BBOX[2], POSE[2])
    # The pose guide never has more frames than the latent: decode is cut to
    # length/fps seconds and the frame batch is capped at `length`.
    assert GRAPH['398:911']['inputs']['length'] == ['398:378', 1]
    assert GRAPH['398:910']['inputs']['video'] == ['398:915', 0]
    assert GRAPH['398:902']['inputs']['image'] == ['398:913', 0]
    assert GRAPH['398:902']['inputs']['latent'] == ['398:357', 0]
    assert GRAPH['398:902']['inputs']['latent_downscale_factor'] == ['398:900', 1]
    assert len(GRAPH['398:397']['inputs']['sigmas'].split(',')) == 9  # 8 steps


def test_combined_with_other_ltx_sets(tmp_path):
    manifests = models.load_manifests('senfers1.yaml,senfers1-ingredients.yaml,senfers1-motion.yaml', WORKFLOW_DIR, tmp_path)
    assert len(manifests.plan) == 10
    assert manifests.custom_nodes.count('ComfyUI-LTXVideo') == 3


# --- verify, extra_model_paths, annotator links ---------------------------------

def test_verify_ready_and_links_annotators(tmp_path, monkeypatch):
    hf_cache_root = tmp_path / 'hf-cache'
    aux = tmp_path / 'aux'
    monkeypatch.setenv('AUX_ANNOTATOR_CKPTS_PATH', str(aux))
    plan = sparse_cache(hf_cache_root, 'senfers1-motion.yaml', tmp_path / 'comfy-models')
    state = verify(tmp_path, monkeypatch, 'senfers1-motion.yaml', hf_cache_root)
    assert state['ready'] is True, state
    assert state['models']['present'] == len(plan) == 7
    assert state['custom_nodes'] == ['ComfyUI-LTXVideo', 'comfyui_controlnet_aux']
    assert {LORA, POSE[2], BBOX[2]} <= set(state['declared_model_names'])
    for repo, rev, name in (POSE, BBOX):
        org, repo_name = repo.split('/')
        link = aux / repo / name  # comfyui_controlnet_aux custom_hf_download layout
        assert link.is_symlink()
        assert link.resolve() == (hf_cache_root / f'models--{org}--{repo_name}' / 'snapshots' / rev / name).resolve()
    search = comfy_search_paths(yaml.safe_load((tmp_path / 'paths.yaml').read_text()))
    lora_snapshot = hf_cache_root / 'models--Lightricks--LTX-2.3-22b-IC-LoRA-Union-Control' / 'snapshots' / LORA_REV
    assert str(lora_snapshot) in search['loras']


def test_verify_relinks_on_reboot_and_reports_missing_annotator(tmp_path, monkeypatch):
    hf_cache_root = tmp_path / 'hf-cache'
    aux = tmp_path / 'aux'
    monkeypatch.setenv('AUX_ANNOTATOR_CKPTS_PATH', str(aux))
    sparse_cache(hf_cache_root, 'senfers1-motion.yaml', tmp_path / 'comfy-models')
    verify(tmp_path, monkeypatch, 'senfers1-motion.yaml', hf_cache_root)
    assert verify(tmp_path, monkeypatch, 'senfers1-motion.yaml', hf_cache_root)['ready'] is True
    org, name = BBOX[0].split('/')
    (hf_cache_root / f'models--{org}--{name}' / 'snapshots' / BBOX[1] / BBOX[2]).unlink()
    state = verify(tmp_path, monkeypatch, 'senfers1-motion.yaml', hf_cache_root)
    assert state['ready'] is False
    assert state['models']['missing'] == [f'annotators/{BBOX[2]}']


def test_link_annotators_ignores_other_categories(tmp_path):
    plan = models.load_plan('senfers1-ingredients.yaml', WORKFLOW_DIR, tmp_path / 'm')
    assert models.link_annotators(plan, tmp_path / 'hf', tmp_path / 'm', tmp_path / 'aux') == []
    assert not (tmp_path / 'aux').exists()


# --- guard + inputs ----------------------------------------------------------------

def booted_state(tmp_path, monkeypatch):
    hf_cache_root = tmp_path / 'hf-cache'
    monkeypatch.setenv('AUX_ANNOTATOR_CKPTS_PATH', str(tmp_path / 'aux'))
    sparse_cache(hf_cache_root, 'senfers1-motion.yaml', tmp_path / 'comfy-models')
    state = verify(tmp_path, monkeypatch, 'senfers1-motion.yaml', hf_cache_root)
    return frozenset(state['allowed_class_types']), frozenset(state['declared_model_names'])


R2 = '0c65aed74e6e0d8447b486e52bbdcb91.r2.cloudflarestorage.com'


def envelope():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return guard.parse_envelope({
        'protocol': 'senai-worker/1',
        'workflow': GRAPH,
        'inputs': [
            {'name': 'driving.mp4', 'media_type': 'video/mp4', 'url': f'https://{R2}/b/driving.mp4'},
            {'name': 'start.png', 'media_type': 'image/png', 'url': f'https://{R2}/b/start.png'},
        ],
        'trace': {'generation_id': 'g', 'attempt': 1, 'binding_alias': 'b', 'binding_revision': 'r',
                  'adapter': 'a', 'workflow_id': 'senfers1-motion-v1', 'graph_sha256': '0' * 64},
        'limits': {'deadline_at': (now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')},
    }, now=now)


def test_graph_passes_guard(tmp_path, monkeypatch):
    allowed, declared = booted_state(tmp_path, monkeypatch)
    env = envelope()
    guard.check_allowlist(env.workflow, allowed)
    guard.check_model_references(env.workflow, declared)


def test_guard_rejects_q8_loader_bf16_and_api_encoder(tmp_path, monkeypatch):
    allowed, declared = booted_state(tmp_path, monkeypatch)
    for node_id, class_type in (('398:900', 'LTXVQ8LoraModelLoader'), ('398:364', 'GemmaAPITextEncode')):
        bad = json.loads(json.dumps(GRAPH))
        bad[node_id]['class_type'] = class_type
        with pytest.raises(WorkerError) as exc:
            guard.check_allowlist(bad, allowed)
        assert exc.value.code == 'NODE_NOT_ALLOWED'
    bf16 = json.loads(json.dumps(GRAPH))
    bf16['398:384']['inputs']['unet_name'] = 'ltx-2.5-22b-distilled-transformer-bf16.safetensors'
    with pytest.raises(WorkerError) as exc:
        guard.check_model_references(bf16, declared)
    assert exc.value.code == 'MODEL_NOT_IN_MANIFEST'
    onnx = json.loads(json.dumps(GRAPH))
    onnx['398:913']['inputs']['pose_estimator'] = 'dw-ll_ucoco_384.onnx'
    onnx['398:913']['inputs']['bbox_detector'] = 'yolox_l.onnx'
    # .onnx is not a model suffix the guard tracks, but the annotator is never
    # staged/linked, so the pack would try to download it: keep the graph on torchscript.
    assert onnx['398:913']['inputs']['pose_estimator'] not in declared


class _Response:
    status_code = 200
    ok = True
    is_redirect = False

    def __init__(self, body):
        self.body = body
        self.headers = {'Content-Length': str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size):
        yield self.body


class _Session:
    def __init__(self, bodies):
        self.bodies = bodies

    def get(self, url, **kwargs):
        return _Response(self.bodies[url.rsplit('/', 1)[1]])


def test_video_input_fetched_and_rewritten_for_load_video(tmp_path):
    env = envelope()
    png = b'\x89PNG\r\n\x1a\n' + b'0' * 32
    written = guard.fetch_inputs(env.inputs, tmp_path / 'senai' / 'job1', allowed_hosts=frozenset({R2}),
                                 max_bytes=200 * 1024 * 1024, inline_max_bytes=1024,
                                 session=_Session({'driving.mp4': MP4_HEAD + b'0' * 64, 'start.png': png}))
    assert written['driving.mp4'].read_bytes().startswith(MP4_HEAD)
    graph = guard.rewrite_input_names(env.workflow, {s.name: f'senai/job1/{s.name}' for s in env.inputs})
    assert graph['396']['inputs']['file'] == 'senai/job1/driving.mp4'
    assert graph['395']['inputs']['image'] == 'senai/job1/start.png'


def test_video_input_host_must_be_allow_listed(tmp_path):
    with pytest.raises(WorkerError) as exc:
        guard.fetch_inputs(envelope().inputs, tmp_path, allowed_hosts=frozenset({'example.com'}),
                           max_bytes=1024, inline_max_bytes=1024, session=_Session({}))
    assert exc.value.code == 'INPUT_HOST_REJECTED'


# --- custom node install --------------------------------------------------------------

def load_installer():
    spec = importlib.util.spec_from_file_location('install_workflow_nodes', ROOT / 'scripts/install-workflow-nodes.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installer_uses_pip_list_instead_of_requirements(tmp_path, monkeypatch):
    installer = load_installer()
    calls = []
    comfy = tmp_path / 'comfyui'

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ['git', 'clone']:
            Path(cmd[-1]).mkdir(parents=True)
            (Path(cmd[-1]) / 'requirements.txt').write_text('onnxruntime-gpu\nmediapipe\n')

    monkeypatch.setattr(installer.subprocess, 'run', fake_run)
    monkeypatch.setenv('WORKFLOW_DIR', str(WORKFLOW_DIR))
    monkeypatch.setenv('COMFY_ROOT', str(comfy))
    installed = installer.main(['--manifests', 'senfers1.yaml,senfers1-motion.yaml'])
    assert sorted(installed) == ['ComfyUI-LTXVideo', 'comfyui_controlnet_aux']
    pip_calls = [c for c in calls if c[:3] == ['uv', 'pip', 'install']]
    assert pip_calls == [
        ['uv', 'pip', 'install', '-r', str(comfy / 'custom_nodes/ComfyUI-LTXVideo/requirements.txt')],
        ['uv', 'pip', 'install', 'opencv-python-headless>=4.7.0.72,<5', 'matplotlib', 'scikit-image'],
    ]
    assert ['git', '-C', str(comfy / 'custom_nodes/comfyui_controlnet_aux'), 'checkout', CNAUX_REV] in calls


@pytest.mark.parametrize('specs', [['-r', 'x.txt'], ['--index-url=https://evil'], ['https://x/y.whl'], ['../pkg'], 'matplotlib'])
def test_installer_rejects_unsafe_pip_specs(specs):
    with pytest.raises(ValueError):
        load_installer().pip_specs({'pip': specs})


# --- staging ------------------------------------------------------------------------

def test_stage_files_lists_motion_set(tmp_path):
    hf_cache_root = tmp_path / 'hf-cache'
    sparse_cache(hf_cache_root, 'senfers1.yaml,senfers1-ingredients.yaml,senfers1-motion.yaml', tmp_path / 'm')
    plan = models.load_plan('senfers1-motion.yaml', WORKFLOW_DIR, tmp_path / 'm')
    assert models.stage_files(plan, hf_cache_root) == MOTION_FILES
    env = {**os.environ, 'WORKFLOWS': 'senfers1-motion.yaml', 'WORKFLOW_DIR': str(WORKFLOW_DIR),
           'HF_CACHE_ROOT': str(hf_cache_root), 'COMFY_MODEL_ROOT': str(tmp_path / 'm')}
    result = subprocess.run([sys.executable, str(ROOT / 'src/workflow_models.py'), '--stage-list'],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == MOTION_FILES


def test_start_stages_motion_set_and_exports_aux_path(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'python'
    body = STAGING_FAKE_PYTHON.split('\n', 1)[1].replace(
        "    if '--verify' in sys.argv:\n",
        "    if '--verify' in sys.argv:\n"
        "        Path(os.environ['TEST_ROOT'], 'verify_aux').write_text(os.environ.get('AUX_ANNOTATOR_CKPTS_PATH', ''))\n")
    fake.write_text('#!' + sys.executable + '\n' + body)
    fake.chmod(0o755)
    (bin_dir / 'python3').symlink_to(fake)
    hf_cache_root = tmp_path / 'bucket' / 'hub'
    plan = models.load_plan('senfers1.yaml,senfers1-ingredients.yaml,senfers1-motion.yaml', WORKFLOW_DIR, tmp_path / 'm')
    for item in plan.values():
        hf = item['hf']
        org, name = hf['repo'].split('/', 1)
        path = hf_cache_root / f'models--{org}--{name}' / 'snapshots' / hf['revision'] / hf['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
    stage_dir = tmp_path / 'stage' / 'hub'
    env = {k: v for k, v in os.environ.items() if k != 'AUX_ANNOTATOR_CKPTS_PATH'}
    env.update({
        'PATH': str(bin_dir) + ':' + os.environ['PATH'], 'TEST_ROOT': str(tmp_path),
        'REAL_PYTHON': sys.executable, 'REAL_WORKFLOW_MODELS': str(ROOT / 'src/workflow_models.py'),
        'PUBLIC_KEY': '', 'SENAI_TRANSPORT': 'cloudrun', 'WORKFLOWS': 'senfers1-motion.yaml',
        'WORKFLOW_DIR': str(WORKFLOW_DIR), 'COMFY_MODEL_ROOT': str(tmp_path / 'm'),
        'HF_CACHE_ROOT': str(hf_cache_root), 'SENAI_HF_STAGE_DIR': str(stage_dir),
        'COMFY_PID_FILE': str(tmp_path / 'comfyui.pid'), 'SENAI_BOOT_TIMELINE': str(tmp_path / 'timeline'),
        'SENAI_WORKER_STATE': str(tmp_path / 'state.json'), 'WORKFLOW_MODEL_PATHS': str(tmp_path / 'paths.yaml'),
    })
    proc = subprocess.run(['bash', str(ROOT / 'src/start.sh')], env=env, capture_output=True, text=True, timeout=30)
    assert 'staging 7 manifest files' in proc.stdout, proc.stdout + proc.stderr
    staged = sorted(str(p.relative_to(stage_dir)) for p in stage_dir.rglob('*') if p.is_file())
    assert staged == MOTION_FILES
    assert (tmp_path / 'verify_aux').read_text() == '/tmp/aux-annotator-ckpts'
