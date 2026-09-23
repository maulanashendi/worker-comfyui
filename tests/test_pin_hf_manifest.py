from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import importlib
pin_hf_manifest = importlib.import_module('pin-hf-manifest')


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """Resolves `revision` to a fixed commit and serves canned paths-info."""

    def __init__(self, commit, paths_info):
        self.commit = commit
        self.paths_info = paths_info
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(('get', url, headers))
        assert url.endswith(f'/revision/main')
        return FakeResponse({'sha': self.commit})

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(('post', url, headers, data))
        assert url.endswith(f'/paths-info/{self.commit}')
        requested = set(data['paths']) if isinstance(data['paths'], (list, tuple)) else {data['paths']}
        return FakeResponse([entry for entry in self.paths_info if entry['path'] in requested])


def write_manifest(path, models):
    path.write_text(yaml.safe_dump({
        'version': 2, 'set': 'x', 'requires_comfyui': '>=0.36.0', 'custom_nodes': [],
        'workflows': [], 'allowed_class_types_extra': [], 'limits': {}, 'warmup_graph': None,
        'models': models,
    }))


def test_pin_resolves_revision_and_fills_sha256_and_bytes(tmp_path):
    manifest_path = tmp_path / 'm.yaml'
    write_manifest(manifest_path, [
        {'path': 'vae/a.safetensors', 'hf': {'repo': 'Org/Repo', 'revision': 'main', 'file': 'vae/a.safetensors'}, 'sha256': None, 'bytes': 0},
        {'path': 'vae/b.safetensors', 'hf': {'repo': 'Org/Repo', 'revision': 'main', 'file': 'vae/b.safetensors'}, 'sha256': None, 'bytes': 0},
    ])
    session = FakeSession('deadbeef' * 5, [
        {'path': 'vae/a.safetensors', 'size': 111, 'lfs': {'oid': 'a' * 64, 'size': 111}},
        {'path': 'vae/b.safetensors', 'size': 222, 'lfs': {'oid': 'b' * 64, 'size': 222}},
    ])

    pin_hf_manifest.pin_manifest(session, manifest_path)

    document = yaml.safe_load(manifest_path.read_text())
    models = {m['path']: m for m in document['models']}
    assert models['vae/a.safetensors']['hf']['revision'] == session.commit
    assert models['vae/a.safetensors']['sha256'] == 'a' * 64
    assert models['vae/a.safetensors']['bytes'] == 111
    assert models['vae/b.safetensors']['sha256'] == 'b' * 64
    assert models['vae/b.safetensors']['bytes'] == 222


def test_pin_only_touches_hf_entries_and_dedupes_per_repo_requests(tmp_path):
    manifest_path = tmp_path / 'm.yaml'
    write_manifest(manifest_path, [
        {'path': 'vae/a.safetensors', 'hf': {'repo': 'Org/Repo', 'revision': 'main', 'file': 'vae/a.safetensors'}, 'sha256': None, 'bytes': 0},
        {'path': 'custom/c.safetensors', 'url': 'https://example.com/c.safetensors', 'sha256': 'c' * 64},
    ])
    session = FakeSession('cafef00d' * 5, [
        {'path': 'vae/a.safetensors', 'size': 5, 'lfs': {'oid': 'a' * 64, 'size': 5}},
    ])

    pin_hf_manifest.pin_manifest(session, manifest_path)

    document = yaml.safe_load(manifest_path.read_text())
    models = {m['path']: m for m in document['models']}
    assert models['custom/c.safetensors'] == {
        'path': 'custom/c.safetensors', 'url': 'https://example.com/c.safetensors', 'sha256': 'c' * 64,
    }
    get_calls = [c for c in session.calls if c[0] == 'get']
    assert len(get_calls) == 1
