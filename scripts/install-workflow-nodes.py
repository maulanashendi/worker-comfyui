"""Install only explicitly selected, revision-pinned custom node dependencies."""
import argparse
from pathlib import Path
import re
import subprocess
import sys
sys.path.insert(0, '/')
from workflow_models import contained
import yaml

parser = argparse.ArgumentParser()
parser.add_argument('--manifests', default='')
args = parser.parse_args()
installed = {}
for selection in filter(None, args.manifests.split(',')):
    manifest = yaml.safe_load(contained(Path('/workflow'), selection.strip()).read_text())
    for node in manifest.get('custom_nodes', []):
        name, repo, revision = node['name'], node['repo'], node['revision']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name) or not re.fullmatch(r'[a-f0-9]{40}', revision) or not repo.startswith('https://'):
            raise ValueError('Custom nodes require a safe name, HTTPS repo and full commit SHA')
        if name in installed:
            if installed[name] != (repo, revision):
                raise ValueError('Conflicting custom node revisions')
            continue
        target = '/comfyui/custom_nodes/' + name
        subprocess.run(['git', 'clone', '--filter=blob:none', '--no-checkout', repo, target], check=True)
        subprocess.run(['git', '-C', target, 'checkout', revision], check=True)
        requirements = Path(target) / 'requirements.txt'
        if requirements.exists():
            subprocess.run(['uv', 'pip', 'install', '-r', str(requirements)], check=True)
        installed[name] = (repo, revision)
if installed:
    subprocess.run(['timeout', '300', sys.executable, 'main.py', '--quick-test-for-ci', '--cpu'], cwd='/comfyui', check=True)
