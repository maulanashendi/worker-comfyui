"""Pin a v2 workflow manifest's `hf` model references to a commit + checksum.

Resolves `revision` (a branch/tag) to a commit SHA via the Hugging Face
`revision` API, then fetches the LFS sha256 and byte size of every declared
file via `paths-info`, and rewrites the manifest in place with `revision`,
`sha256`, and `bytes` filled in. Files without an `hf` source are left
untouched. An optional `HF_TOKEN` is sent as a bearer token if set.
"""
import argparse
import os
from pathlib import Path

import requests
import yaml

API_ROOT = 'https://huggingface.co/api/models'


def _headers(token):
    return {'Authorization': f'Bearer {token}'} if token else {}


def resolve_revision(session, repo, revision, token=None):
    response = session.get(f'{API_ROOT}/{repo}/revision/{revision}', headers=_headers(token), timeout=30)
    response.raise_for_status()
    return response.json()['sha']


def fetch_paths_info(session, repo, commit, paths, token=None):
    response = session.post(
        f'{API_ROOT}/{repo}/paths-info/{commit}',
        data={'paths': paths, 'expand': 'true'},
        headers=_headers(token),
        timeout=30,
    )
    response.raise_for_status()
    result = {}
    for entry in response.json():
        lfs = entry.get('lfs') or {}
        result[entry['path']] = {
            'sha256': lfs.get('oid'),
            'bytes': entry.get('size', lfs.get('size')),
        }
    return result


def pin_repo(session, repo, revision, files, token=None):
    commit = resolve_revision(session, repo, revision, token)
    info = fetch_paths_info(session, repo, commit, files, token)
    return commit, info


def pin_manifest(session, manifest_path, revision_arg=None, token=None):
    document = yaml.safe_load(manifest_path.read_text())
    by_repo = {}
    for model in document.get('models', []):
        hf = model.get('hf')
        if not hf:
            continue
        by_repo.setdefault(hf['repo'], []).append(model)
    for repo, entries in by_repo.items():
        revision = revision_arg or entries[0]['hf'].get('revision') or 'main'
        files = [entry['hf']['file'] for entry in entries]
        commit, info = pin_repo(session, repo, revision, files, token)
        for entry in entries:
            entry['hf']['revision'] = commit
            meta = info.get(entry['hf']['file'])
            if not meta:
                continue
            if meta.get('sha256'):
                entry['sha256'] = meta['sha256']
            if meta.get('bytes') is not None:
                entry['bytes'] = meta['bytes']
    manifest_path.write_text(yaml.safe_dump(document, sort_keys=False))
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path, help='Path to a v2 workflow manifest YAML file')
    parser.add_argument('--revision', default=None,
                         help='Branch/tag to resolve per repo (default: the hf.revision already in the manifest, else main)')
    args = parser.parse_args(argv)
    token = os.getenv('HF_TOKEN')
    with requests.Session() as session:
        pin_manifest(session, args.manifest, args.revision, token)


if __name__ == '__main__':
    main()
