import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import workflow_models as models

ROOT = Path(__file__).resolve().parents[1]


def test_senai_all_modes_six_models(tmp_path):
    plan = models.load_plan('ltx25.yaml', ROOT / 'workflow', tmp_path)
    assert len(plan) == 6
    assert {p.parent.name for p in plan} == {'diffusion_models', 'text_encoders', 'vae', 'latent_upscale_models'}


def test_manifest_missing_model_and_escape(tmp_path):
    (tmp_path / 'graph.json').write_text(json.dumps({'1': {'class_type': 'NewLoader', 'inputs': {'model': 'missing.gguf'}}}))
    manifest = {'version': 1, 'workflows': ['graph.json'], 'models': [{'path': 'vae/a.pt', 'url': 'https://example.com/a.pt'}]}
    (tmp_path / 'm.yaml').write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match='missing from manifest'):
        models.load_plan('m.yaml', tmp_path, tmp_path / 'models')
    manifest['models'][0]['path'] = '../escape.pt'
    (tmp_path / 'm.yaml').write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match='escapes'):
        models.load_plan('m.yaml', tmp_path, tmp_path / 'models')


def test_download_atomic_cached_and_auth(tmp_path, monkeypatch):
    monkeypatch.setenv('HF_TOKEN', 'secret')
    content = b'mock model weights'
    item = {'path': 'vae/a.pt', 'url': 'https://huggingface.co/a/b/resolve/main/a.pt', 'sha256': hashlib.sha256(content).hexdigest()}
    response = MagicMock()
    response.__enter__.return_value = response
    response.headers = {'Content-Length': str(len(content))}
    response.iter_content.return_value = [content]
    target = tmp_path / 'vae/a.pt'
    with patch.object(models.requests, 'get', return_value=response) as get:
        models.download(target, item)
        models.download(target, item)
        assert get.call_count == 1
        assert get.call_args.kwargs['headers']['Authorization'] == 'Bearer secret'
    assert target.read_bytes() == content
    assert not list(target.parent.glob('*.part'))


def test_failed_download_preserves_existing_and_cleans_partial(tmp_path):
    target = tmp_path / 'a.pt'
    target.write_bytes(b'previous')
    item = {'path': 'a.pt', 'url': 'https://example.com/a.pt', 'sha256': '0' * 64}
    response = MagicMock()
    response.__enter__.return_value = response
    response.headers = {}
    response.iter_content.return_value = [b'corrupt']
    with patch.object(models.requests, 'get', return_value=response), pytest.raises(RuntimeError):
        models.download(target, item, attempts=1)
    assert target.read_bytes() == b'previous'
    assert not list(tmp_path.glob('*.part'))


def test_check_does_not_download_or_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('WORKFLOW_MANIFESTS', 'ltx25.yaml')
    monkeypatch.setenv('WORKFLOW_DIR', str(ROOT / 'workflow'))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(tmp_path / 'models'))
    monkeypatch.setattr(sys, 'argv', ['workflow_models.py', '--check'])
    with patch.object(models.requests, 'get') as get:
        models.main()
        get.assert_not_called()
    assert capsys.readouterr().out.count('[ ]') == 6
    assert not (tmp_path / 'models').exists()


def test_multiple_manifests_deduplicate_and_reject_conflicts(tmp_path):
    (tmp_path / 'graph.json').write_text(json.dumps({'1': {'class_type': 'NewLoader', 'inputs': {'model': 'a.pt'}}}))
    manifest = {'version': 1, 'workflows': ['graph.json'], 'models': [{'path': 'custom/a.pt', 'url': 'https://example.com/a.pt'}]}
    for name in ('a.yaml', 'b.yaml'):
        (tmp_path / name).write_text(yaml.safe_dump(manifest))
    assert len(models.load_plan('a.yaml,b.yaml', tmp_path, tmp_path / 'models')) == 1
    manifest['models'][0]['url'] = 'https://example.com/different.pt'
    (tmp_path / 'b.yaml').write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match='Conflicting'):
        models.load_plan('a.yaml,b.yaml', tmp_path, tmp_path / 'models')


def test_custom_model_root_is_registered_with_comfy(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKFLOW_MANIFESTS', 'ltx25.yaml')
    monkeypatch.setenv('WORKFLOW_DIR', str(ROOT / 'workflow'))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(tmp_path / 'persistent-models'))
    monkeypatch.setenv('WORKFLOW_MODEL_PATHS', str(tmp_path / 'paths.yaml'))
    monkeypatch.setattr(sys, 'argv', ['workflow_models.py'])
    with patch.object(models, 'download') as download:
        models.main()
        assert download.call_count == 6
    config = yaml.safe_load((tmp_path / 'paths.yaml').read_text())['workflow_models']
    assert config['base_path'] == str(tmp_path / 'persistent-models')
    assert config['latent_upscale_models'] == 'latent_upscale_models/'
    assert config['text_encoders'] == 'text_encoders/'


def test_minimax_editor_metadata_does_not_include_ltx(tmp_path):
    plan = models.load_plan('minimax-h3.yaml', ROOT / 'workflow', tmp_path)
    direct = models.load_plan('video_minimax_h3_r2v.json', ROOT / 'workflow', tmp_path)
    assert plan == direct
    assert len(plan) == 5
    assert all('MiniMax-H3' in item['url'] for item in plan.values())
    assert not any('ltx' in item['path'].lower() for item in plan.values())


def test_cache_only_no_network_no_hash_and_detects_changed_file(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKFLOWS', 'minimax-h3.yaml')
    monkeypatch.setenv('WORKFLOW_DIR', str(ROOT / 'workflow'))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(tmp_path))
    monkeypatch.setenv('WORKFLOW_MODEL_PATHS', str(tmp_path / 'paths.yaml'))
    monkeypatch.setenv('MODEL_DOWNLOAD_POLICY', 'cache-only')
    monkeypatch.setattr(sys, 'argv', ['workflow_models.py'])
    plan = models.load_plan('minimax-h3.yaml', ROOT / 'workflow', tmp_path)
    for path, item in plan.items():
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'prepared')
        models.record_cache(path, item)
    with patch.object(models.requests, 'get') as get, patch.object(models.hashlib, 'file_digest') as digest:
        models.main()
        get.assert_not_called()
        digest.assert_not_called()
    next(iter(plan)).write_bytes(b'modified')
    with patch.object(models.requests, 'get') as get, pytest.raises(RuntimeError, match='Prepare model cache'):
        models.main()
    get.assert_not_called()


def test_cache_identity_changes_when_source_changes(tmp_path):
    path = tmp_path / 'model.pt'
    path.write_bytes(b'weights')
    item = {'path': 'vae/model.pt', 'url': 'https://example.com/v1', 'sha256': None}
    models.record_cache(path, item)
    assert models.cached(path, item)
    assert not models.cached(path, {**item, 'url': 'https://example.com/v2'})


def test_concurrent_workers_download_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    item = {'path': 'vae/model.pt', 'url': 'https://example.com/model.pt', 'sha256': None}
    response = MagicMock()
    response.__enter__.return_value = response
    response.headers = {}
    response.iter_content.return_value = [b'weights']
    target = tmp_path / 'vae/model.pt'
    with patch.object(models.requests, 'get', return_value=response) as get:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: models.download(target, item), range(2)))
        assert get.call_count == 1
    assert models.cached(target, item)
