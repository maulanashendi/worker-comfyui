"""Validated, declarative model downloads selected by WORKFLOWS."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlparse

import requests
import yaml

PROTOCOL = "senai-worker/1"


def contained(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("Expected a nonempty relative path")
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()) or result == root.resolve():
        raise ValueError("Path escapes configured root")
    return result


MODEL_SUFFIXES = ('.safetensors', '.gguf', '.ckpt', '.pt', '.pth', '.bin')


def graph_assets(graph):
    """Read API graphs or editor metadata, including nested subgraphs."""
    assets, references = [], set()
    if isinstance(graph.get('nodes'), list):
        nodes = list(graph['nodes'])
        for subgraph in graph.get('definitions', {}).get('subgraphs', []):
            extra_assets, extra_refs = graph_assets(subgraph)
            assets.extend(extra_assets)
            references.update(extra_refs)
        for node in nodes:
            for model in node.get('properties', {}).get('models', []):
                assets.append({'path': model['directory'] + '/' + model['name'],
                               'url': model['url'], 'sha256': model.get('sha256')})
            if node.get('type') in ('MarkdownNote', 'Note'):
                continue
            for value in node.get('widgets_values') or []:
                if isinstance(value, str) and value.endswith(MODEL_SUFFIXES):
                    references.add(value)
    else:
        if not graph or not all(isinstance(n, dict) and 'class_type' in n for n in graph.values()):
            raise ValueError('Expected a ComfyUI API or editor workflow')
        for node in graph.values():
            for value in node.get('inputs', {}).values():
                if isinstance(value, str) and value.endswith(MODEL_SUFFIXES):
                    references.add(value)
    return assets, references


def graph_class_types(graph):
    """class_type values of an API graph (a dict of node_id -> node); empty for editor JSON."""
    if not isinstance(graph, dict) or isinstance(graph.get('nodes'), list):
        return set()
    return {node['class_type'] for node in graph.values() if isinstance(node, dict) and 'class_type' in node}


def resolve_hf_model(model):
    """v2 manifest entry with an `hf` source: derive the download URL, keep the hf pin."""
    hf = model['hf']
    if hf.get('file') != model['path']:
        raise ValueError(f"hf.file must equal path: {model['path']}")
    revision = hf.get('revision') or 'main'
    url = model.get('url') or f"https://huggingface.co/{hf['repo']}/resolve/{revision}/{hf['file']}"
    item = {'path': model['path'], 'url': url, 'sha256': model.get('sha256') or None, 'hf': hf}
    if model.get('bytes'):
        item['bytes'] = model['bytes']
    return item


def apply_models(plan, model_root, models, references, declared_model_names):
    declared = set()
    for model in models:
        target = contained(model_root, model['path'])
        if len(Path(model['path']).parts) < 2:
            raise ValueError('Model path must include category and filename')
        parsed = urlparse(model['url'])
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Model URL must use HTTPS without embedded credentials')
        digest = model.get('sha256') or None
        if digest is not None and not re.fullmatch(r'[a-fA-F0-9]{64}', str(digest)):
            raise ValueError('Invalid sha256')
        item = {'path': model['path'], 'url': model['url'], 'sha256': digest}
        if model.get('bytes'):
            item['bytes'] = model['bytes']
        if model.get('hf'):
            item['hf'] = model['hf']
        if target in plan and plan[target] != item:
            raise ValueError(f"Conflicting model definitions: {model['path']}")
        plan[target] = item
        declared.add('/'.join(Path(model['path']).parts[1:]))
    missing = references - declared
    if missing:
        raise ValueError('Workflow model missing from manifest: ' + ', '.join(sorted(missing)))
    declared_model_names.update(declared)


class ManifestSet:
    __slots__ = ('plan', 'custom_nodes', 'allowed_class_types', 'declared_model_names',
                 'limits', 'warmup_graph', 'documents')

    def __init__(self):
        self.plan = {}
        self.custom_nodes = []
        self.allowed_class_types = set()
        self.declared_model_names = set()
        self.limits = {}
        self.warmup_graph = None
        self.documents = []


def load_manifests(selection, workflow_root, model_root):
    """Parse every manifest/graph named by `selection` (a comma list); never scan the folder."""
    result = ManifestSet()
    for name in selection.split(','):
        name = name.strip()
        document = yaml.safe_load(contained(workflow_root, name).read_text())
        if name.endswith('.json'):
            models, references = graph_assets(document)
            apply_models(result.plan, model_root, models, references, result.declared_model_names)
            continue
        version = document.get('version')
        if version == 1:
            models = list(document.get('models', []))
            references = set()
            graphs = document.get('workflows')
            if not isinstance(graphs, list) or not graphs:
                raise ValueError('Manifest must reference at least one workflow')
            for graph_path in graphs:
                graph = json.loads(contained(workflow_root, graph_path).read_text())
                embedded, refs = graph_assets(graph)
                models.extend(embedded)
                references.update(refs)
                result.allowed_class_types.update(graph_class_types(graph))
            apply_models(result.plan, model_root, models, references, result.declared_model_names)
        elif version == 2:
            models = [resolve_hf_model(m) if m.get('hf') else dict(m) for m in document.get('models', [])]
            references = set()
            graphs = document.get('workflows') or []
            for graph_path in graphs:
                graph = json.loads(contained(workflow_root, graph_path).read_text())
                _, refs = graph_assets(graph)
                references.update(refs)
                result.allowed_class_types.update(graph_class_types(graph))
            result.allowed_class_types.update(document.get('allowed_class_types_extra') or [])
            for node in document.get('custom_nodes') or []:
                result.custom_nodes.append(node['name'] if isinstance(node, dict) else node)
            result.limits.update(document.get('limits') or {})
            if document.get('warmup_graph') is not None:
                result.warmup_graph = document['warmup_graph']
            apply_models(result.plan, model_root, models, references, result.declared_model_names)
        else:
            raise ValueError(f'Unsupported manifest version: {name}')
        result.documents.append(document)
    return result


def load_plan(selection, workflow_root, model_root):
    """Backward-compatible entry point: only the resolved model plan."""
    return load_manifests(selection, workflow_root, model_root).plan


def cache_record(path, model):
    stat = path.stat()
    return {**model, 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def receipt_path(path):
    return path.with_name(path.name + '.worker-cache.json')


def cached(path, model):
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        return json.loads(receipt_path(path).read_text()) == cache_record(path, model)
    except (OSError, ValueError):
        return False


def record_cache(path, model):
    # Called under the per-model lock, after successful verification/download.
    receipt = receipt_path(path)
    temporary = receipt.with_suffix('.tmp')
    temporary.write_text(json.dumps(cache_record(path, model)))
    temporary.replace(receipt)


def valid_file(path, digest):
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if digest:
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest() == digest.lower()
    return True


def acquire_lock(lock_file, timeout_sec):
    deadline = time.monotonic() + timeout_sec
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f'Timed out waiting for model lock after {timeout_sec}s')
            time.sleep(0.05)


def _download(path, model, attempts=3):
    if cached(path, model):
        print(f"[cached] {model['path']}")
        return
    if not receipt_path(path).exists() and valid_file(path, model['sha256']):
        record_cache(path, model)
        print(f"[adopted] {model['path']}")
        return
    token = os.getenv('HF_TOKEN') or os.getenv('HUGGINGFACE_ACCESS_TOKEN')
    headers = {}
    if token and urlparse(model['url']).hostname == 'huggingface.co':
        headers['Authorization'] = f'Bearer {token}'
    for attempt in range(attempts):
        temporary = None
        try:
            with requests.get(model['url'], headers=headers, stream=True, timeout=(30, 120)) as response:
                response.raise_for_status()
                if 'text/html' in response.headers.get('Content-Type', ''):
                    raise ValueError('Server returned HTML instead of a model')
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', suffix='.part', delete=False) as out:
                    temporary = Path(out.name)
                    for chunk in response.iter_content(8 * 1024 * 1024):
                        out.write(chunk)
                if not valid_file(temporary, model['sha256']):
                    raise ValueError('Empty download or checksum mismatch')
                length = response.headers.get('Content-Length')
                if length and not response.headers.get('Content-Encoding') and temporary.stat().st_size != int(length):
                    raise ValueError('Incomplete download')
                temporary.replace(path)
                record_cache(path, model)
                print(f"[downloaded] {model['path']}")
                return
        except (requests.RequestException, OSError, ValueError):
            if attempt + 1 == attempts:
                # Never print signed URLs or authorization headers from exceptions.
                raise RuntimeError(f"Download failed: {model['path']} ({attempts} attempts)") from None
            time.sleep(2 ** attempt)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def download(path, model, attempts=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_timeout = float(os.getenv('MODEL_LOCK_TIMEOUT_SEC', '600'))
    # Shared-volume workers must not download the same asset concurrently.
    with path.with_name(path.name + '.lock').open('a') as lock:
        acquire_lock(lock, lock_timeout)
        _download(path, model, attempts)


def download_all(plan, workers, budget_sec):
    """Download every model in `plan`, bounded by `budget_sec` overall.

    Worker threads are daemons so a budget timeout can return immediately
    instead of waiting for a still-hanging request; the process either exits
    right after (subprocess boundary) or moves on to --verify.
    """
    deadline = time.monotonic() + budget_sec
    errors = []
    errors_lock = threading.Lock()
    threads = []
    semaphore = threading.Semaphore(workers)

    def run(path, model):
        try:
            download(path, model)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller below
            with errors_lock:
                errors.append(exc)
        finally:
            semaphore.release()

    started_all = True
    for path, model in plan.items():
        if time.monotonic() >= deadline:
            started_all = False
            break
        semaphore.acquire()
        thread = threading.Thread(target=run, args=(path, model), daemon=True)
        thread.start()
        threads.append(thread)

    for thread in threads:
        remaining = deadline - time.monotonic()
        thread.join(timeout=max(0.0, remaining))

    if not started_all or any(thread.is_alive() for thread in threads):
        raise RuntimeError(f'Model download exceeded MODEL_DOWNLOAD_BUDGET_SEC={budget_sec}s')
    if errors:
        raise errors[0]


def resolve_snapshot_dir(hf_cache_root, repo, revision):
    org, name = repo.split('/', 1)
    repo_dir = hf_cache_root / f'models--{org}--{name}'
    rev = revision or 'main'
    if not re.fullmatch(r'[0-9a-fA-F]{40}', rev):
        ref_file = repo_dir / 'refs' / rev
        if ref_file.is_file():
            rev = ref_file.read_text().strip()
    return repo_dir / 'snapshots' / rev


def find_model_file(item, hf_cache_root, model_root):
    """Preferred candidate first: the HF cache snapshot, then COMFY_MODEL_ROOT as a fallback path."""
    candidates = []
    hf = item.get('hf')
    if hf:
        snapshot = resolve_snapshot_dir(hf_cache_root, hf['repo'], hf.get('revision'))
        candidates.append(snapshot / hf['file'])
    candidates.append(contained(model_root, item['path']))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def comfyui_version():
    version_file = Path('/comfyui/comfyui_version.py')
    if version_file.is_file():
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)', version_file.read_text())
        if match:
            return match.group(1)
    pinned = Path('/etc/senai-comfyui-version')
    if pinned.is_file():
        text = pinned.read_text().strip()
        if text:
            return text
    return os.getenv('COMFYUI_VERSION', 'unknown')


def write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def raw_manifest_sha256(selection, workflow_root):
    """sha256 of the raw bytes of every WORKFLOWS-named manifest file, concatenated in
    order; a missing/unreadable file counts as empty bytes (contract 0.2.1 §4.3:
    manifest_sha256 must never be null, even when the manifest fails to parse)."""
    digest = hashlib.sha256()
    for name in selection.split(','):
        name = name.strip()
        try:
            data = contained(workflow_root, name).read_bytes()
        except (OSError, ValueError):
            data = b''
        digest.update(data)
    return digest.hexdigest()


def write_model_paths(plan, model_root, hf_cache_root, path):
    """One `extra_model_paths` section per HF snapshot repo used, plus the COMFY_MODEL_ROOT fallback."""
    config = {}
    for item in plan.values():
        hf = item.get('hf')
        if not hf:
            continue
        base = resolve_snapshot_dir(hf_cache_root, hf['repo'], hf.get('revision'))
        key = 'hf_' + re.sub(r'[^a-z0-9]+', '_', hf['repo'].lower()).strip('_')
        category = Path(item['path']).parts[0]
        section = config.setdefault(key, {'base_path': str(base)})
        section[category] = category + '/'
    categories = sorted({Path(item['path']).parts[0] for item in plan.values()})
    config['workflow_models'] = {'base_path': str(model_root.resolve()), **{c: c + '/' for c in categories}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config))


def run_verify():
    selection = os.getenv('WORKFLOWS') or os.getenv('WORKFLOW_MANIFESTS', '')
    root = Path(os.getenv('WORKFLOW_DIR', '/workflow'))
    model_root = Path(os.getenv('COMFY_MODEL_ROOT', '/comfyui/models'))
    hf_cache_root = Path(os.getenv('HF_CACHE_ROOT', '/runpod-volume/huggingface-cache/hub'))
    state_path = Path(os.getenv('SENAI_WORKER_STATE', '/tmp/senai-worker-state.json'))
    paths_path = Path(os.getenv('WORKFLOW_MODEL_PATHS', '/tmp/workflow_model_paths.yaml'))
    aws_bucket = os.getenv('AWS_BUCKET_NAME', '')

    state = {
        'protocol': PROTOCOL,
        'workflows': selection,
        'comfyui': comfyui_version(),
    }
    try:
        manifests = load_manifests(selection, root, model_root)
    except Exception as exc:  # noqa: BLE001 - boot must never crash-loop on this
        state.update(
            manifest_sha256=raw_manifest_sha256(selection, root),
            ready=False,
            unready_code='MODEL_CACHE_MISSING',
            unready_message=str(exc),
            models={'declared': 0, 'present': 0, 'missing': [], 'bytes_manifest': 0, 'bytes_visible': 0},
            declared_model_names=[],
            allowed_class_types=[],
            custom_nodes=[],
            limits={},
            warmup_graph=None,
        )
        write_state(state_path, state)
        write_model_paths({}, model_root, hf_cache_root, paths_path)
        return state

    missing = []
    bytes_manifest = 0
    bytes_visible = 0
    for item in manifests.plan.values():
        declared_bytes = item.get('bytes') or 0
        bytes_manifest += declared_bytes
        path = find_model_file(item, hf_cache_root, model_root)
        if not path.is_file():
            missing.append(item['path'])
            continue
        size = path.stat().st_size
        if declared_bytes and size != declared_bytes:
            missing.append(item['path'])
            continue
        bytes_visible += size

    if missing:
        ready, code, message = False, 'MODEL_CACHE_MISSING', 'Missing or mismatched models: ' + ', '.join(sorted(missing))
    elif not aws_bucket:
        ready, code, message = False, 'OUTPUT_NOT_CONFIGURED', 'AWS_BUCKET_NAME is not configured'
    else:
        ready, code, message = True, None, None

    manifest_sha256 = hashlib.sha256(
        json.dumps(manifests.documents, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()

    state.update(
        manifest_sha256=manifest_sha256,
        ready=ready,
        unready_code=code,
        unready_message=message,
        models={
            'declared': len(manifests.plan),
            'present': len(manifests.plan) - len(missing),
            'missing': sorted(missing),
            'bytes_manifest': bytes_manifest,
            'bytes_visible': bytes_visible,
        },
        declared_model_names=sorted(manifests.declared_model_names),
        allowed_class_types=sorted(manifests.allowed_class_types),
        custom_nodes=list(dict.fromkeys(manifests.custom_nodes)),
        limits={
            'no_progress_sec': manifests.limits.get('no_progress_sec', int(os.getenv('NO_PROGRESS_SEC', '120'))),
            'no_progress_load_sec': manifests.limits.get('no_progress_load_sec', int(os.getenv('NO_PROGRESS_LOAD_SEC', '600'))),
            'execution_ceiling_sec': manifests.limits.get('execution_ceiling_sec', int(os.getenv('JOB_DEADLINE_CEILING_SEC', '1800'))),
        },
        warmup_graph=manifests.warmup_graph,
    )
    write_state(state_path, state)
    write_model_paths(manifests.plan, model_root, hf_cache_root, paths_path)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Validate and print checklist; no downloads or worker startup')
    parser.add_argument('--verify', action='store_true', help='Verify cached models against the manifest and write boot state; never downloads')
    args = parser.parse_args()
    if args.verify:
        run_verify()
        return
    selection = os.getenv('WORKFLOWS') or os.getenv('WORKFLOW_MANIFESTS', '')
    if not selection:
        print('No workflows selected; using existing models')
        return
    root = Path(os.getenv('WORKFLOW_DIR', '/workflow'))
    models = Path(os.getenv('COMFY_MODEL_ROOT', '/comfyui/models'))
    plan = load_plan(selection, root, models)
    policy = os.getenv('MODEL_DOWNLOAD_POLICY', 'cache-only')
    if policy not in ('missing', 'cache-only'):
        raise ValueError('MODEL_DOWNLOAD_POLICY must be missing or cache-only')
    if args.check:
        for path, model in plan.items():
            state = 'cached' if cached(path, model) else 'prepare required'
            print(f"[ ] {model['path']} ({state})")
    elif policy == 'cache-only':
        missing = [item['path'] for path, item in plan.items() if not cached(path, item)]
        if missing:
            raise RuntimeError('Prepare model cache before starting worker: ' + ', '.join(missing))
    else:
        workers = int(os.getenv('MODEL_DOWNLOAD_CONCURRENCY', '4'))
        if not 1 <= workers <= 16:
            raise ValueError('MODEL_DOWNLOAD_CONCURRENCY must be between 1 and 16')
        budget = float(os.getenv('MODEL_DOWNLOAD_BUDGET_SEC', '1800'))
        download_all(plan, workers, budget)
    if not args.check:
        categories = {Path(item['path']).parts[0] for item in plan.values()}
        config = {'workflow_models': {'base_path': str(models.resolve()), **{key: key + '/' for key in sorted(categories)}}}
        Path(os.getenv('WORKFLOW_MODEL_PATHS', '/tmp/workflow_model_paths.yaml')).write_text(yaml.safe_dump(config))


if __name__ == '__main__':
    main()
