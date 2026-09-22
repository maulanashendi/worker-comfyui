import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
import handler
import media_output


@pytest.mark.parametrize('result', [{'images': []}, {'error': 'bad input'}])
def test_refresh_all_results(result):
    with patch.object(handler, '_handle_job', return_value=result.copy()), patch.object(handler, 'REFRESH_WORKER', True):
        assert handler.handler({})['refresh_worker'] is True


def test_refresh_exception_and_opt_out():
    with patch.object(handler, '_handle_job', side_effect=RuntimeError('oops')), patch.object(handler, 'REFRESH_WORKER', True):
        assert handler.handler({})['refresh_worker'] is True
    with patch.object(handler, '_handle_job', return_value={'images': []}), patch.object(handler, 'REFRESH_WORKER', False):
        assert 'refresh_worker' not in handler.handler({})


def test_ltx_graph_to_senai_video_response(monkeypatch):
    monkeypatch.setenv('OUTPUT_FORMAT', 'senai')
    monkeypatch.setenv('AWS_BUCKET_NAME', 'staging')
    graph = json.loads((ROOT / 'workflow/ltx25-t2v-v1.json').read_text())
    ws = MagicMock()
    ws.recv.return_value = json.dumps({'type': 'executing', 'data': {'node': None, 'prompt_id': 'p'}})
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = 'https://media.example/video.mp4'
    with patch.object(handler, 'check_server', return_value=True), patch.object(handler, 'validate_workflow_models', return_value=None), patch.object(handler, 'queue_workflow', return_value={'prompt_id': 'p'}) as queue, patch.object(handler.websocket, 'WebSocket', return_value=ws), patch.object(handler, 'get_history', return_value={'p': {'outputs': {'75': {'images': [{'filename': 'video.mp4', 'type': 'output'}]}}}}), patch.object(handler, 'get_image_data', return_value=b'fake mp4'), patch.object(media_output.boto3, 'client', return_value=s3):
        result = handler.handler({'id': 'test', 'input': {'workflow': graph}})
    assert result['status'] == 'success'
    assert result['refresh_worker'] is True
    assert result['output']['images'][0]['media_type'] == 'video/mp4'
    assert result['output']['images'][0]['type'] == 'url'
    assert queue.call_args.args[0] == graph
    s3.put_object.assert_called_once()


@pytest.mark.parametrize('scenario', ['completed', 'comfy_crash', 'signal'])
def test_start_supervises_and_reaps_children(tmp_path, scenario):
    # Run the real shell entrypoint against lightweight process doubles.
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'python'
    fake.write_text('''#!/usr/bin/env python3
import os,sys,time
from pathlib import Path
if '-c' in sys.argv:
    print('OK: mock GPU'); sys.exit(0)
if any('workflow_models.py' in arg for arg in sys.argv): sys.exit(0)
role = 'comfy' if any('main.py' in arg for arg in sys.argv) else 'handler'
Path(os.environ['TEST_ROOT'], role+'.pid').write_text(str(os.getpid()))
if os.environ['SCENARIO'] == 'completed' and role == 'handler':
    time.sleep(.3); sys.exit(0)
if os.environ['SCENARIO'] == 'comfy_crash' and role == 'comfy':
    time.sleep(.3); sys.exit(7)
time.sleep(60)
''')
    fake.chmod(0o755)
    # start.sh uses both python and python3; shebang must avoid recursion.
    fake.write_text(fake.read_text().replace('#!/usr/bin/env python3', '#!' + sys.executable))
    (bin_dir / 'python3').symlink_to(fake)
    env = {**os.environ, 'PATH': str(bin_dir) + ':' + os.environ['PATH'], 'TEST_ROOT': str(tmp_path), 'SCENARIO': scenario, 'PUBLIC_KEY': '', 'WORKFLOW_MANIFESTS': '', 'COMFY_PID_FILE': str(tmp_path / 'comfyui.pid')}
    proc = subprocess.Popen(['bash', str(ROOT / 'src/start.sh')], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        if scenario == 'signal':
            deadline = time.monotonic() + 5
            while not (tmp_path / 'handler.pid').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert (tmp_path / 'handler.pid').exists()
            proc.terminate()
        output, _ = proc.communicate(timeout=10)
        assert proc.returncode == {'completed': 0, 'comfy_crash': 7, 'signal': 143}[scenario], output.decode()
        for role in ('comfy', 'handler'):
            pid = int((tmp_path / (role + '.pid')).read_text())
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        assert not (tmp_path / 'comfyui.pid').exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.parametrize('result', [{'images': [{'filename': 'x.png'}]}, {'error': 'invalid workflow'}])
def test_real_runpod_sdk_reports_result_with_stop_pod(result):
    import asyncio
    from runpod.serverless.modules.rp_job import run_job
    with patch.object(handler, '_handle_job', return_value=result.copy()), patch.object(handler, 'REFRESH_WORKER', True):
        reply = asyncio.run(run_job(handler.handler, {'id': 'local-sdk-test', 'input': {}}))
    assert reply['stopPod'] is True
    if 'error' in result:
        assert reply['error'] == result['error']
    else:
        assert reply['output'] == result


def test_preparation_entrypoint_needs_no_gpu_or_handler(tmp_path):
    import workflow_models
    plan = workflow_models.load_plan('minimax-h3.yaml', ROOT / 'workflow', tmp_path)
    for path, item in plan.items():
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'prepared-model')
        workflow_models.record_cache(path, item)
    env = {**os.environ, 'PATH': str(Path(sys.executable).parent) + ':' + os.environ['PATH'],
           'WORKER_ROOT': str(ROOT / 'src'), 'WORKFLOW_DIR': str(ROOT / 'workflow'),
           'WORKFLOWS': 'minimax-h3.yaml', 'COMFY_MODEL_ROOT': str(tmp_path),
           'WORKFLOW_MODEL_PATHS': str(tmp_path / 'paths.yaml'),
           'PREPARE_MODELS_ONLY': 'true', 'MODEL_DOWNLOAD_POLICY': 'cache-only'}
    result = subprocess.run(['bash', str(ROOT / 'src/start.sh')], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'paths.yaml').exists()
    assert 'GPU' not in result.stdout
    assert 'Starting RunPod Handler' not in result.stdout
