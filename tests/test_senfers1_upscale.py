"""LTX 2.5 1080p -> 4K upscale set (Refine-Details IC-LoRA, paths A and B): manifest,
graph shape, guard allowlist + model preflight, limits and boot staging."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import guard
from senai_errors import WorkerError
import workflow_models as models

from tests.test_senfers1_ingredients import sparse_cache, verify

WORKFLOW_DIR = ROOT / 'workflow'
GRAPH_A = json.loads((WORKFLOW_DIR / 'senfers1-upscale-a-v1.json').read_text())
GRAPH_B = json.loads((WORKFLOW_DIR / 'senfers1-upscale-b-v1.json').read_text())
LTX_REV = '5e6e71018ee1756ed329b697a7b4aedc934dfce9'
LORA = 'ltx-2.5-22b-ic-lora-refine-details-1.0.safetensors'
LORA_REPO = 'Lightricks/LTX-2.5-22b-IC-LoRA-Refine-Details'
LORA_REV = '4912c478b35dd96b6cdcc5f7c7ff9d72c2c57929'
LORA_SHA = '771a84f70e143af89867fc714ebbf7bd6edcaa4974c0360cba4a2d292a99f0e6'
UPSCALE_FILES = [
    f'models--Lightricks--LTX-2.5-22b-IC-LoRA-Refine-Details/snapshots/{LORA_REV}/{LORA}',
    f'models--Lightricks--LTX-2.5/snapshots/{LTX_REV}/diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors',
    f'models--Lightricks--LTX-2.5/snapshots/{LTX_REV}/latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors',
    f'models--Lightricks--LTX-2.5/snapshots/{LTX_REV}/text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors',
    f'models--Lightricks--LTX-2.5/snapshots/{LTX_REV}/vae/ltx-2.5-video-vae-bf16.safetensors',
]
UPSCALE_BYTES = 40653793230
SIGMAS_8 = '1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0'
SIGMAS_3 = '0.909375, 0.725, 0.421875, 0.0'
GRAPHS = pytest.mark.parametrize('graph', [GRAPH_A, GRAPH_B], ids=['a', 'b'])


# --- manifest + graphs ----------------------------------------------------------

def test_manifest_declares_int8_models_lora_and_upscaler(tmp_path):
    manifests = models.load_manifests('senfers1-upscale.yaml', WORKFLOW_DIR, tmp_path)
    paths = sorted(item['path'] for item in manifests.plan.values())
    assert paths == [
        'diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors',
        'latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors',
        f'loras/{LORA}',
        'text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors',
        'vae/ltx-2.5-video-vae-bf16.safetensors',
    ]
    assert not any('transformer-bf16' in p or 'ltx-2.5-bf16' in p or 'gemma4_e2b' in p for p in paths)
    assert sum(item['bytes'] for item in manifests.plan.values()) == UPSCALE_BYTES
    lora = next(item for item in manifests.plan.values() if item['path'] == f'loras/{LORA}')
    assert lora['hf'] == {'repo': LORA_REPO, 'revision': LORA_REV, 'file': LORA}
    assert (lora['sha256'], lora['bytes']) == (LORA_SHA, 1308787534)
    assert manifests.custom_nodes == ['ComfyUI-LTXVideo']
    assert manifests.limits == {'no_progress_sec': 900, 'no_progress_load_sec': 900, 'execution_ceiling_sec': 3600}


@GRAPHS
def test_graph_shape_and_forbidden_nodes(graph):
    class_types = {node['class_type'] for node in graph.values()}
    assert {'LoadVideo', 'GetVideoComponents', 'LTXICLoRALoaderModelOnly', 'LTXAddVideoICLoRAGuide',
            'LTXVTiledFusionSampler', 'VAEDecodeTiled', 'CreateVideo', 'SaveVideo'} <= class_types
    assert not class_types & {'LTXVQ8LoraModelLoader', 'GemmaAPITextEncode', 'TextGenerateLTX2Prompt',
                              'SamplerCustomAdvanced', 'LTXVGetTilingSizes', 'VHS_LoadVideo'}
    assert graph['398:384']['inputs']['unet_name'].endswith('comfy-int8-convrot.safetensors')
    assert graph['398:387']['inputs']['clip_name'].endswith('comfy-int8-convrot.safetensors')
    assert graph['398:900']['inputs']['lora_name'] == LORA
    assert graph['396']['inputs']['file'] == 'source.mp4'
    assert (graph['398:372']['inputs']['value'], graph['398:360']['inputs']['value']) == (3840, 2176)
    assert graph['398:376']['class_type'] == 'PrimitiveStringMultiline'
    sampler = graph['398:920']['inputs']
    assert (sampler['tile_width'], sampler['tile_height'], sampler['overlap_frac'], sampler['cfg']) == (1024, 576, 0.5, 1.0)
    assert sampler['latents'] == ['398:902', 2] and sampler['model'] == ['398:900', 0]
    assert graph['398:352']['inputs']['sampler_name'] == 'euler'
    guide = graph['398:902']['inputs']
    assert guide['image'] == ['398:912', 0] and guide['use_tiled_encode'] is False
    # One temporal extent: streaming off on the guide and tile_frames 0 on both nodes.
    assert (guide['use_streaming'], guide['tile_frames'], sampler['tile_frames']) == (False, 0, 0)
    decode = graph['398:374']['inputs']
    assert (decode['tile_size'], decode['overlap'], decode['temporal_size']) == (1024, 128, 256)
    assert graph['398:370']['inputs']['audio'] == ['398:910', 1]  # source audio muxed


def test_path_a_samples_eight_steps_from_an_empty_canvas():
    assert GRAPH_A['398:902']['inputs']['latent'] == ['398:356', 0]
    assert GRAPH_A['398:356']['class_type'] == 'EmptyLTXVLatentVideo'
    assert GRAPH_A['398:397']['inputs']['sigmas'] == SIGMAS_8
    assert 'VAEEncode' not in {node['class_type'] for node in GRAPH_A.values()}


def test_path_b_refines_the_upsampled_source_latent_for_three_steps():
    assert GRAPH_B['398:902']['inputs']['latent'] == ['398:348', 0]
    assert GRAPH_B['398:348']['class_type'] == 'LTXVLatentUpsampler'
    assert GRAPH_B['398:348']['inputs']['samples'] == ['398:933', 0]
    assert GRAPH_B['398:933']['class_type'] == 'VAEEncode'
    assert GRAPH_B['398:371']['inputs']['model_name'] == 'ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors'
    assert GRAPH_B['398:397']['inputs']['sigmas'] == SIGMAS_3


def test_combined_with_other_ltx_sets_shares_models(tmp_path):
    manifests = models.load_manifests('senfers1.yaml,senfers1-ingredients.yaml,senfers1-motion.yaml,senfers1-upscale.yaml',
                                      WORKFLOW_DIR, tmp_path)
    assert len(manifests.plan) == 11


# --- verify + guard ----------------------------------------------------------------

def booted_state(tmp_path, monkeypatch):
    hf_cache_root = tmp_path / 'hf-cache'
    plan = sparse_cache(hf_cache_root, 'senfers1-upscale.yaml', tmp_path / 'comfy-models')
    state = verify(tmp_path, monkeypatch, 'senfers1-upscale.yaml', hf_cache_root)
    assert state['ready'] is True, state
    assert state['models']['present'] == len(plan) == 5
    assert state['limits'] == {'no_progress_sec': 900, 'no_progress_load_sec': 900, 'execution_ceiling_sec': 3600}
    return frozenset(state['allowed_class_types']), frozenset(state['declared_model_names'])


R2 = '0c65aed74e6e0d8447b486e52bbdcb91.r2.cloudflarestorage.com'


def envelope(graph, workflow_id):
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    return guard.parse_envelope({
        'protocol': 'senai-worker/1',
        'workflow': graph,
        'inputs': [{'name': 'source.mp4', 'media_type': 'video/mp4', 'url': f'https://{R2}/b/source.mp4'}],
        'trace': {'generation_id': 'g', 'attempt': 1, 'binding_alias': 'b', 'binding_revision': 'r',
                  'adapter': 'a', 'workflow_id': workflow_id, 'graph_sha256': '0' * 64},
        'limits': {'deadline_at': (now + timedelta(minutes=60)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                   'no_progress_sec': 900, 'no_progress_load_sec': 900},
    }, now=now)


def test_upscale_only_boot_passes_both_graphs(tmp_path, monkeypatch):
    allowed, declared = booted_state(tmp_path, monkeypatch)
    for graph, workflow_id in ((GRAPH_A, 'senfers1-upscale-a-v1'), (GRAPH_B, 'senfers1-upscale-b-v1')):
        env = envelope(graph, workflow_id)
        assert env.no_progress_sec == 900
        guard.check_allowlist(env.workflow, allowed)
        guard.check_model_references(env.workflow, declared)
        rewritten = guard.rewrite_input_names(env.workflow, {'source.mp4': 'senai/job1/source.mp4'})
        assert rewritten['396']['inputs']['file'] == 'senai/job1/source.mp4'


@GRAPHS
def test_guard_rejects_q8_loader_api_encoder_and_bf16(graph, tmp_path, monkeypatch):
    allowed, declared = booted_state(tmp_path, monkeypatch)
    for node_id, class_type in (('398:900', 'LTXVQ8LoraModelLoader'), ('398:364', 'GemmaAPITextEncode')):
        bad = json.loads(json.dumps(graph))
        bad[node_id]['class_type'] = class_type
        with pytest.raises(WorkerError) as exc:
            guard.check_allowlist(bad, allowed)
        assert exc.value.code == 'NODE_NOT_ALLOWED'
    for node_id, key, name in (
        ('398:384', 'unet_name', 'ltx-2.5-22b-distilled-transformer-bf16.safetensors'),
        ('398:387', 'clip_name', 'gemma4-12b-with-proj-ltx-2.5-bf16.safetensors'),
    ):
        bf16 = json.loads(json.dumps(graph))
        bf16[node_id]['inputs'][key] = name
        with pytest.raises(WorkerError) as exc:
            guard.check_model_references(bf16, declared)
        assert exc.value.code == 'MODEL_NOT_IN_MANIFEST'


def test_upscale_only_set_rejects_other_ltx_graphs(tmp_path, monkeypatch):
    allowed, declared = booted_state(tmp_path, monkeypatch)
    motion = json.loads((WORKFLOW_DIR / 'senfers1-motion-v1.json').read_text())
    with pytest.raises(WorkerError) as exc:
        guard.check_allowlist(motion, allowed)  # DWPreprocessor, SamplerCustomAdvanced, ...
    assert exc.value.code == 'NODE_NOT_ALLOWED'


# --- staging ------------------------------------------------------------------------

def test_stage_list_for_upscale_set_alone(tmp_path):
    hf_cache_root = tmp_path / 'hf-cache'
    sparse_cache(hf_cache_root, 'senfers1.yaml,senfers1-ingredients.yaml,senfers1-motion.yaml,senfers1-upscale.yaml', tmp_path / 'm')
    plan = models.load_plan('senfers1-upscale.yaml', WORKFLOW_DIR, tmp_path / 'm')
    assert models.stage_files(plan, hf_cache_root) == UPSCALE_FILES
    env = {**os.environ, 'WORKFLOWS': 'senfers1-upscale.yaml', 'WORKFLOW_DIR': str(WORKFLOW_DIR),
           'HF_CACHE_ROOT': str(hf_cache_root), 'COMFY_MODEL_ROOT': str(tmp_path / 'm')}
    result = subprocess.run([sys.executable, str(ROOT / 'src/workflow_models.py'), '--stage-list'],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == UPSCALE_FILES
