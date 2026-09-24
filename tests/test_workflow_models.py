import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import workflow_models as models

ROOT = Path(__file__).resolve().parents[1]


def write_snapshot_file(hf_cache_root, hf, size):
    """A sparse (size-only, no real content) HF cache snapshot file for one `hf` model pin.

    Mirrors workflow_models.resolve_snapshot_dir: builds the path from `hf['revision']`
    as declared, adding a self-pointing `refs/<rev>` file when that revision isn't
    already a 40-hex commit (so the ref-indirection code path is always exercised too,
    the same way it would be for an unpinned `revision: main` in a real manifest).
    """
    org, name = hf['repo'].split('/', 1)
    repo_dir = hf_cache_root / f'models--{org}--{name}'
    revision = hf.get('revision') or 'main'
    if not re.fullmatch(r'[0-9a-fA-F]{40}', revision):
        ref_file = repo_dir / 'refs' / revision
        ref_file.parent.mkdir(parents=True, exist_ok=True)
        ref_file.write_text(revision)
    path = repo_dir / 'snapshots' / revision / hf['file']
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as stream:
        if size:
            stream.truncate(size)
    return path


def write_store_file(store_root, hf, size):
    """A sparse RunPod cached-model mount file for one `hf` model pin: <store_root>/<org>/<repo>/<revision>/<hf.file>."""
    path = models.resolve_model_store_dir(store_root, hf['repo'], hf.get('revision')) / hf['file']
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as stream:
        if size:
            stream.truncate(size)
    return path


def write_manifest_snapshots(hf_cache_root, plan):
    """Sparse HF cache snapshots for every hf-sourced item in a resolved `plan`,
    sized to each item's declared `bytes` (0 -> empty file, size check skipped)."""
    for item in plan.values():
        hf = item.get('hf')
        if hf:
            write_snapshot_file(hf_cache_root, hf, item.get('bytes') or 0)


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
    monkeypatch.setenv('MODEL_DOWNLOAD_POLICY', 'missing')
    monkeypatch.setattr(sys, 'argv', ['workflow_models.py'])
    with patch.object(models, 'download_all') as download_all:
        models.main()
        assert download_all.call_count == 1
        assert len(download_all.call_args.args[0]) == 6
    config = yaml.safe_load((tmp_path / 'paths.yaml').read_text())['workflow_models']
    assert config['base_path'] == str(tmp_path / 'persistent-models')
    assert config['latent_upscale_models'] == 'latent_upscale_models/'
    assert config['text_encoders'] == 'text_encoders/'


def test_minimax_h3_manifest_v2_eight_models_two_repos(tmp_path):
    plan = models.load_plan('minimax-h3.yaml', ROOT / 'workflow', tmp_path)
    assert len(plan) == 8
    repos = {item['hf']['repo'] for item in plan.values()}
    assert repos == {'Comfy-Org/MiniMax-H3', 'Comfy-Org/SDPose'}
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
    item = {'path': 'vae/model.pt', 'url': 'https://example.com/model.pt', 'sha256': None}
    response = MagicMock()
    response.__enter__.return_value = response
    response.headers = {}
    response.iter_content.return_value = [b'weights']
    target = tmp_path / 'vae/model.pt'
    with patch.object(models.requests, 'get', return_value=response) as get:
        threads = [threading.Thread(target=models.download, args=(target, item)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert get.call_count == 1
    assert models.cached(target, item)


# --- --verify -----------------------------------------------------------

def test_verify_ltx_set_ready_with_hf_cache(tmp_path, monkeypatch):
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    plan = models.load_plan('ltx25.yaml', ROOT / 'workflow', model_root)
    write_manifest_snapshots(hf_cache_root, plan)

    monkeypatch.setenv('WORKFLOWS', 'ltx25.yaml')
    monkeypatch.setenv('WORKFLOW_DIR', str(ROOT / 'workflow'))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(model_root))
    monkeypatch.setenv('HF_CACHE_ROOT', str(hf_cache_root))
    monkeypatch.setenv('AWS_BUCKET_NAME', 'staging')
    monkeypatch.setenv('SENAI_WORKER_STATE', str(tmp_path / 'state.json'))
    monkeypatch.setenv('WORKFLOW_MODEL_PATHS', str(tmp_path / 'paths.yaml'))

    state = models.run_verify()

    assert state['ready'] is True
    assert state['unready_code'] is None
    assert state['models']['declared'] == 6
    assert state['models']['present'] == 6
    assert state['models']['missing'] == []
    assert len(state['declared_model_names']) == 6
    assert not any('minimax' in n.lower() for n in state['declared_model_names'])

    config = yaml.safe_load((tmp_path / 'paths.yaml').read_text())
    hf_sections = [v for k, v in config.items() if k.startswith('hf_')]
    assert len(hf_sections) == 2
    assert 'workflow_models' in config


def test_verify_h3_set_eight_models_two_repos_no_ltx(tmp_path, monkeypatch):
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    plan = models.load_plan('minimax-h3.yaml', ROOT / 'workflow', model_root)
    write_manifest_snapshots(hf_cache_root, plan)

    monkeypatch.setenv('WORKFLOWS', 'minimax-h3.yaml')
    monkeypatch.setenv('WORKFLOW_DIR', str(ROOT / 'workflow'))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(model_root))
    monkeypatch.setenv('HF_CACHE_ROOT', str(hf_cache_root))
    monkeypatch.setenv('AWS_BUCKET_NAME', 'staging')
    monkeypatch.setenv('SENAI_WORKER_STATE', str(tmp_path / 'state.json'))
    monkeypatch.setenv('WORKFLOW_MODEL_PATHS', str(tmp_path / 'paths.yaml'))

    state = models.run_verify()

    assert state['models']['declared'] == 8
    assert not any('ltx' in name.lower() for name in state['declared_model_names'])
    config = yaml.safe_load((tmp_path / 'paths.yaml').read_text())
    hf_sections = {k: v for k, v in config.items() if k.startswith('hf_')}
    assert len(hf_sections) == 2


def test_verify_wrong_size_marks_missing(tmp_path, monkeypatch):
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    manifest_path = tmp_path / 'm.yaml'
    manifest_path.write_text(yaml.safe_dump({
        'version': 2, 'set': 'x', 'requires_comfyui': '>=0.36.0', 'custom_nodes': [],
        'workflows': [], 'allowed_class_types_extra': [], 'limits': {}, 'warmup_graph': None,
        'models': [
            {'path': 'vae/a.safetensors', 'hf': {'repo': 'Org/Repo', 'revision': 'main', 'file': 'vae/a.safetensors'},
             'sha256': None, 'bytes': 999},
        ],
    }))
    write_snapshot_file(hf_cache_root, {'repo': 'Org/Repo', 'revision': 'main', 'file': 'vae/a.safetensors'}, size=5)

    monkeypatch.setenv('WORKFLOWS', 'm.yaml')
    monkeypatch.setenv('WORKFLOW_DIR', str(tmp_path))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(model_root))
    monkeypatch.setenv('HF_CACHE_ROOT', str(hf_cache_root))
    monkeypatch.setenv('AWS_BUCKET_NAME', 'staging')
    monkeypatch.setenv('SENAI_WORKER_STATE', str(tmp_path / 'state.json'))
    monkeypatch.setenv('WORKFLOW_MODEL_PATHS', str(tmp_path / 'paths.yaml'))

    state = models.run_verify()

    assert state['ready'] is False
    assert state['unready_code'] == 'MODEL_CACHE_MISSING'
    assert 'vae/a.safetensors' in state['models']['missing']


def test_verify_resolves_ref_file_to_snapshot_commit(tmp_path, monkeypatch):
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    manifest_path = tmp_path / 'm.yaml'
    manifest_path.write_text(yaml.safe_dump({
        'version': 2, 'set': 'x', 'requires_comfyui': '>=0.36.0', 'custom_nodes': [],
        'workflows': [], 'allowed_class_types_extra': [], 'limits': {}, 'warmup_graph': None,
        'models': [
            {'path': 'vae/a.safetensors', 'hf': {'repo': 'Org/Repo', 'revision': 'main', 'file': 'vae/a.safetensors'},
             'sha256': None, 'bytes': 0},
        ],
    }))
    commit = 'a' * 40
    repo_dir = hf_cache_root / 'models--Org--Repo'
    (repo_dir / 'refs').mkdir(parents=True)
    (repo_dir / 'refs' / 'main').write_text(commit)
    path = repo_dir / 'snapshots' / commit / 'vae/a.safetensors'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'weights')

    monkeypatch.setenv('WORKFLOWS', 'm.yaml')
    monkeypatch.setenv('WORKFLOW_DIR', str(tmp_path))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(model_root))
    monkeypatch.setenv('HF_CACHE_ROOT', str(hf_cache_root))
    monkeypatch.setenv('AWS_BUCKET_NAME', 'staging')
    monkeypatch.setenv('SENAI_WORKER_STATE', str(tmp_path / 'state.json'))
    monkeypatch.setenv('WORKFLOW_MODEL_PATHS', str(tmp_path / 'paths.yaml'))

    state = models.run_verify()
    assert state['ready'] is True


def test_verify_h3_completes_fast_without_reading_file_contents(tmp_path, monkeypatch):
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    plan = models.load_plan('minimax-h3.yaml', ROOT / 'workflow', model_root)
    write_manifest_snapshots(hf_cache_root, plan)

    monkeypatch.setenv('WORKFLOWS', 'minimax-h3.yaml')
    monkeypatch.setenv('WORKFLOW_DIR', str(ROOT / 'workflow'))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(model_root))
    monkeypatch.setenv('HF_CACHE_ROOT', str(hf_cache_root))
    monkeypatch.setenv('AWS_BUCKET_NAME', 'staging')
    monkeypatch.setenv('SENAI_WORKER_STATE', str(tmp_path / 'state.json'))
    monkeypatch.setenv('WORKFLOW_MODEL_PATHS', str(tmp_path / 'paths.yaml'))

    real_open = open
    opened_model_files = []

    def tracking_open(file, *args, **kwargs):
        path = Path(file)
        if path.suffix == '.safetensors':
            opened_model_files.append(path)
        return real_open(file, *args, **kwargs)

    with patch.object(models.hashlib, 'file_digest') as digest, \
         patch('builtins.open', side_effect=tracking_open):
        start = time.monotonic()
        state = models.run_verify()
        elapsed = time.monotonic() - start
        digest.assert_not_called()
    assert opened_model_files == []
    assert elapsed < 2.0
    assert state['ready'] is True
    assert state['models']['declared'] == 8
    assert state['models']['missing'] == []
    assert state['allowed_class_types']
    for class_type in ('MiniMaxH3ReferenceToVideo', 'MiniMaxH3FunControlNetApply', 'LoadVideo', 'PreviewImage'):
        assert class_type in state['allowed_class_types']


def test_missing_policy_stops_within_download_budget(tmp_path, monkeypatch):
    manifest_path = tmp_path / 'm.yaml'
    manifest_path.write_text(yaml.safe_dump({
        'version': 2, 'set': 'x', 'requires_comfyui': '>=0.36.0', 'custom_nodes': [],
        'workflows': [], 'allowed_class_types_extra': [], 'limits': {}, 'warmup_graph': None,
        'models': [
            {'path': 'vae/a.safetensors', 'hf': {'repo': 'Org/Repo', 'revision': 'main', 'file': 'vae/a.safetensors'},
             'sha256': None, 'bytes': 0},
        ],
    }))
    model_root = tmp_path / 'models'

    def slow_get(*args, **kwargs):
        time.sleep(10)
        raise AssertionError('should never return within the test')

    monkeypatch.setenv('WORKFLOWS', 'm.yaml')
    monkeypatch.setenv('WORKFLOW_DIR', str(tmp_path))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(model_root))
    monkeypatch.setenv('MODEL_DOWNLOAD_POLICY', 'missing')
    monkeypatch.setenv('MODEL_DOWNLOAD_BUDGET_SEC', '1')
    monkeypatch.setattr(sys, 'argv', ['workflow_models.py'])

    with patch.object(models.requests, 'get', side_effect=slow_get):
        start = time.monotonic()
        with pytest.raises(RuntimeError, match='(?i)budget'):
            models.main()
        elapsed = time.monotonic() - start
    assert elapsed < 3.0


def test_missing_policy_lock_timeout_does_not_hang(tmp_path, monkeypatch):
    target = tmp_path / 'vae' / 'a.safetensors'
    target.parent.mkdir(parents=True)
    item = {'path': 'vae/a.safetensors', 'url': 'https://example.com/a.safetensors', 'sha256': None}
    monkeypatch.setenv('MODEL_LOCK_TIMEOUT_SEC', '1')

    lock_path = target.with_name(target.name + '.lock')
    holder = lock_path.open('a')
    import fcntl
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            models.download(target, item)
        elapsed = time.monotonic() - start
    finally:
        holder.close()
    assert elapsed < 3.0


def test_verify_output_not_configured_without_bucket(tmp_path, monkeypatch):
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    plan = models.load_plan('ltx25.yaml', ROOT / 'workflow', model_root)
    write_manifest_snapshots(hf_cache_root, plan)

    monkeypatch.setenv('WORKFLOWS', 'ltx25.yaml')
    monkeypatch.setenv('WORKFLOW_DIR', str(ROOT / 'workflow'))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(model_root))
    monkeypatch.setenv('HF_CACHE_ROOT', str(hf_cache_root))
    monkeypatch.delenv('AWS_BUCKET_NAME', raising=False)
    monkeypatch.setenv('SENAI_WORKER_STATE', str(tmp_path / 'state.json'))
    monkeypatch.setenv('WORKFLOW_MODEL_PATHS', str(tmp_path / 'paths.yaml'))

    state = models.run_verify()
    assert state['ready'] is False
    assert state['unready_code'] == 'OUTPUT_NOT_CONFIGURED'


def test_verify_corrupt_manifest_still_yields_hex_manifest_sha256(tmp_path, monkeypatch):
    manifest_path = tmp_path / 'broken.yaml'
    manifest_path.write_text('workflows: [unterminated\n')  # invalid YAML flow sequence

    monkeypatch.setenv('WORKFLOWS', 'broken.yaml')
    monkeypatch.setenv('WORKFLOW_DIR', str(tmp_path))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(tmp_path / 'models'))
    monkeypatch.setenv('AWS_BUCKET_NAME', 'staging')
    monkeypatch.setenv('SENAI_WORKER_STATE', str(tmp_path / 'state.json'))
    monkeypatch.setenv('WORKFLOW_MODEL_PATHS', str(tmp_path / 'paths.yaml'))

    state = models.run_verify()

    assert state['ready'] is False
    assert state['unready_code'] == 'MODEL_CACHE_MISSING'
    assert state['unready_message']
    assert re.fullmatch(r'[0-9a-f]{64}', state['manifest_sha256'])
    assert state['manifest_sha256'] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def test_verify_missing_workflows_file_still_yields_hex_manifest_sha256(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKFLOWS', 'does-not-exist.yaml')
    monkeypatch.setenv('WORKFLOW_DIR', str(tmp_path))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(tmp_path / 'models'))
    monkeypatch.setenv('AWS_BUCKET_NAME', 'staging')
    monkeypatch.setenv('SENAI_WORKER_STATE', str(tmp_path / 'state.json'))
    monkeypatch.setenv('WORKFLOW_MODEL_PATHS', str(tmp_path / 'paths.yaml'))

    state = models.run_verify()

    assert state['ready'] is False
    assert state['unready_code'] == 'MODEL_CACHE_MISSING'
    assert state['unready_message']
    assert state['manifest_sha256'] == hashlib.sha256(b'').hexdigest()


def test_default_download_policy_is_cache_only(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKFLOWS', 'minimax-h3.yaml')
    monkeypatch.setenv('WORKFLOW_DIR', str(ROOT / 'workflow'))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(tmp_path))
    monkeypatch.setenv('WORKFLOW_MODEL_PATHS', str(tmp_path / 'paths.yaml'))
    monkeypatch.delenv('MODEL_DOWNLOAD_POLICY', raising=False)
    monkeypatch.setattr(sys, 'argv', ['workflow_models.py'])

    with patch.object(models, 'download_all') as download_all, patch.object(models.requests, 'get') as get, \
         pytest.raises(RuntimeError, match='Prepare model cache'):
        models.main()
    download_all.assert_not_called()
    get.assert_not_called()


# --- model-store (RunPod cached-model mount) candidate ------------------

def _store_item():
    hf = {'repo': 'Org/Repo', 'revision': 'a' * 40, 'file': 'vae/a.safetensors'}
    return {'path': 'vae/a.safetensors', 'hf': hf, 'sha256': None, 'bytes': 7}


def test_find_model_file_prefers_model_store(tmp_path):
    item = _store_item()
    store_root = tmp_path / 'store'
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    store_path = write_store_file(store_root, item['hf'], size=7)

    found = models.find_model_file(item, hf_cache_root, model_root, model_store_root=store_root)
    assert found == store_path


def test_find_model_file_model_store_wins_over_hf_snapshot(tmp_path):
    item = _store_item()
    store_root = tmp_path / 'store'
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    store_path = write_store_file(store_root, item['hf'], size=7)
    write_snapshot_file(hf_cache_root, item['hf'], size=7)

    found = models.find_model_file(item, hf_cache_root, model_root, model_store_root=store_root)
    assert found == store_path


def test_find_model_file_falls_back_to_hf_snapshot(tmp_path):
    item = _store_item()
    store_root = tmp_path / 'store'
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    snapshot_path = write_snapshot_file(hf_cache_root, item['hf'], size=7)

    found = models.find_model_file(item, hf_cache_root, model_root, model_store_root=store_root)
    assert found == snapshot_path


def test_find_model_file_falls_back_to_model_root(tmp_path):
    item = _store_item()
    store_root = tmp_path / 'store'
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    target = models.contained(model_root, item['path'])
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('wb') as stream:
        stream.truncate(7)

    found = models.find_model_file(item, hf_cache_root, model_root, model_store_root=store_root)
    assert found == target


def test_write_model_paths_uses_model_store_when_present(tmp_path):
    item = _store_item()
    store_root = tmp_path / 'store'
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    write_store_file(store_root, item['hf'], size=7)
    store_dir = models.resolve_model_store_dir(store_root, item['hf']['repo'], item['hf']['revision'])
    plan = {models.contained(model_root, item['path']): item}
    paths_path = tmp_path / 'paths.yaml'

    models.write_model_paths(plan, model_root, hf_cache_root, paths_path, model_store_root=store_root)

    config = yaml.safe_load(paths_path.read_text())
    section = config['hf_org_repo']
    assert section['base_path'] == str(store_dir)


def _verify_env(monkeypatch, tmp_path, workflow_dir, manifest, model_root, hf_cache_root, aws_bucket='staging'):
    monkeypatch.setenv('WORKFLOWS', manifest)
    monkeypatch.setenv('WORKFLOW_DIR', str(workflow_dir))
    monkeypatch.setenv('COMFY_MODEL_ROOT', str(model_root))
    monkeypatch.setenv('HF_CACHE_ROOT', str(hf_cache_root))
    if aws_bucket is None:
        monkeypatch.delenv('AWS_BUCKET_NAME', raising=False)
    else:
        monkeypatch.setenv('AWS_BUCKET_NAME', aws_bucket)
    monkeypatch.setenv('SENAI_WORKER_STATE', str(tmp_path / 'state.json'))
    monkeypatch.setenv('WORKFLOW_MODEL_PATHS', str(tmp_path / 'paths.yaml'))


def _single_model_manifest(tmp_path, item):
    manifest_path = tmp_path / 'm.yaml'
    manifest_path.write_text(yaml.safe_dump({
        'version': 2, 'set': 'x', 'requires_comfyui': '>=0.36.0', 'custom_nodes': [],
        'workflows': [], 'allowed_class_types_extra': [], 'limits': {}, 'warmup_graph': None,
        'models': [item],
    }))
    return manifest_path


def test_verify_ready_with_model_store_only(tmp_path, monkeypatch):
    store_root = tmp_path / 'store'
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    item = _store_item()
    _single_model_manifest(tmp_path, item)
    write_store_file(store_root, item['hf'], size=item['bytes'])

    _verify_env(monkeypatch, tmp_path, tmp_path, 'm.yaml', model_root, hf_cache_root)
    monkeypatch.setattr(models, 'MODEL_STORE_ROOT', store_root)

    state = models.run_verify()

    assert state['ready'] is True
    assert state['models']['present'] == state['models']['declared'] == 1
    assert state['models']['missing'] == []


def test_verify_missing_everywhere(tmp_path, monkeypatch):
    store_root = tmp_path / 'store'
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    item = _store_item()
    _single_model_manifest(tmp_path, item)

    _verify_env(monkeypatch, tmp_path, tmp_path, 'm.yaml', model_root, hf_cache_root)
    monkeypatch.setattr(models, 'MODEL_STORE_ROOT', store_root)

    state = models.run_verify()

    assert state['ready'] is False
    assert state['unready_code'] == 'MODEL_CACHE_MISSING'
    assert item['path'] in state['models']['missing']


def test_verify_model_store_wrong_size_marks_missing(tmp_path, monkeypatch):
    store_root = tmp_path / 'store'
    hf_cache_root = tmp_path / 'hf-cache'
    model_root = tmp_path / 'comfy-models'
    item = _store_item()
    _single_model_manifest(tmp_path, item)
    write_store_file(store_root, item['hf'], size=1)

    _verify_env(monkeypatch, tmp_path, tmp_path, 'm.yaml', model_root, hf_cache_root)
    monkeypatch.setattr(models, 'MODEL_STORE_ROOT', store_root)

    state = models.run_verify()

    assert state['ready'] is False
    assert item['path'] in state['models']['missing']


def test_ltx_workflow_canonical_sha256_matches_contract_pins():
    pins = yaml.safe_load((ROOT / 'contract/senai-worker-1/pins.yaml').read_text())
    pinned = {w['id']: w['sha256_canonical'] for w in pins['workflows'] if w['status'] == 'pinned'}
    files = {
        'ltx25-t2v-v1': 'ltx25-t2v-v1.json',
        'ltx25-i2v-v1': 'ltx25-i2v-v1.json',
        'ltx25-flf-v1': 'ltx25-flf-v1.json',
    }
    for workflow_id, filename in files.items():
        graph = json.loads((ROOT / 'workflow' / filename).read_text())
        digest = hashlib.sha256(json.dumps(graph, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert digest == pinned[workflow_id], workflow_id
