"""Install only explicitly selected, revision-pinned custom node dependencies."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
sys.path.insert(0, '/')
from workflow_models import contained
import yaml

# A requirement spec: name[extras] plus optional version constraints; no URLs,
# paths or pip options (`-r`, `--index-url`, ...).
PIP_SPEC_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?([<>=!~]=?[A-Za-z0-9.*+!-]+(,[<>=!~]=?[A-Za-z0-9.*+!-]+)*)?')


def pip_specs(node):
    """`pip:` replaces the pack's requirements.txt with an explicit list (None = use it)."""
    specs = node.get('pip')
    if specs is None:
        return None
    if not isinstance(specs, list) or not all(isinstance(s, str) and PIP_SPEC_RE.fullmatch(s) for s in specs):
        raise ValueError('custom_nodes[].pip must be a list of plain requirement specs')
    return specs


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifests', default='')
    args = parser.parse_args(argv)
    workflow_root = Path(os.getenv('WORKFLOW_DIR', '/workflow'))
    comfy_root = os.getenv('COMFY_ROOT', '/comfyui')
    installed = {}
    for selection in filter(None, args.manifests.split(',')):
        manifest = yaml.safe_load(contained(workflow_root, selection.strip()).read_text())
        for node in manifest.get('custom_nodes', []):
            name, repo, revision = node['name'], node['repo'], node['revision']
            if not re.fullmatch(r'[A-Za-z0-9_-]+', name) or not re.fullmatch(r'[a-f0-9]{40}', revision) or not repo.startswith('https://'):
                raise ValueError('Custom nodes require a safe name, HTTPS repo and full commit SHA')
            specs = pip_specs(node)
            if name in installed:
                if installed[name] != (repo, revision, specs):
                    raise ValueError('Conflicting custom node revisions')
                continue
            target = comfy_root + '/custom_nodes/' + name
            subprocess.run(['git', 'clone', '--filter=blob:none', '--no-checkout', repo, target], check=True)
            subprocess.run(['git', '-C', target, 'checkout', revision], check=True)
            requirements = Path(target) / 'requirements.txt'
            if specs is not None:
                if specs:
                    subprocess.run(['uv', 'pip', 'install', *specs], check=True)
            elif requirements.exists():
                subprocess.run(['uv', 'pip', 'install', '-r', str(requirements)], check=True)
            installed[name] = (repo, revision, specs)
    if installed:
        subprocess.run(['timeout', '300', sys.executable, 'main.py', '--quick-test-for-ci', '--cpu'], cwd=comfy_root, check=True)
    return installed


if __name__ == '__main__':
    main()
