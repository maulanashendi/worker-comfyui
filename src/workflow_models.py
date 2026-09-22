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
import time
from urllib.parse import urlparse

import requests
import yaml


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


def load_plan(selection, workflow_root, model_root):
    """Only selected YAML/JSON files contribute assets; never scan the folder."""
    plan = {}
    for name in selection.split(','):
        name = name.strip()
        document = yaml.safe_load(contained(workflow_root, name).read_text())
        if name.endswith('.json'):
            models, references = graph_assets(document)
        else:
            if not isinstance(document, dict) or document.get('version') != 1:
                raise ValueError(f'Unsupported manifest version: {name}')
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
        declared = set()
        for model in models:
            target = contained(model_root, model['path'])
            if len(Path(model['path']).parts) < 2:
                raise ValueError('Model path must include category and filename')
            parsed = urlparse(model['url'])
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('Model URL must use HTTPS without embedded credentials')
            digest = model.get('sha256')
            if digest is not None and not re.fullmatch(r'[a-fA-F0-9]{64}', str(digest)):
                raise ValueError('Invalid sha256')
            item = {'path': model['path'], 'url': model['url'], 'sha256': digest}
            if target in plan and plan[target] != item:
                raise ValueError(f"Conflicting model definitions: {model['path']}")
            plan[target] = item
            declared.add('/'.join(Path(model['path']).parts[1:]))
        missing = references - declared
        if missing:
            raise ValueError('Workflow model missing from manifest: ' + ', '.join(sorted(missing)))
    return plan


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
    # Shared-volume workers must not download the same asset concurrently.
    with path.with_name(path.name + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _download(path, model, attempts)



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Validate and print checklist; no downloads or worker startup')
    args = parser.parse_args()
    selection = os.getenv('WORKFLOWS') or os.getenv('WORKFLOW_MANIFESTS', '')
    if not selection:
        print('No workflows selected; using existing models')
        return
    root = Path(os.getenv('WORKFLOW_DIR', '/workflow'))
    models = Path(os.getenv('COMFY_MODEL_ROOT', '/comfyui/models'))
    plan = load_plan(selection, root, models)
    policy = os.getenv('MODEL_DOWNLOAD_POLICY', 'missing')
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
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda pair: download(*pair), plan.items()))
    if not args.check:
        categories = {Path(item['path']).parts[0] for item in plan.values()}
        config = {'workflow_models': {'base_path': str(models.resolve()), **{key: key + '/' for key in sorted(categories)}}}
        Path(os.getenv('WORKFLOW_MODEL_PATHS', '/tmp/workflow_model_paths.yaml')).write_text(yaml.safe_dump(config))


if __name__ == '__main__':
    main()
