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
from senai_errors import PROTOCOL as SENAI_PROTOCOL


@pytest.mark.parametrize('result', [{'images': []}, {'error': 'bad input'}])
def test_refresh_all_results(result):
    with patch.object(handler, '_handle_job', return_value=result.copy()), patch.object(handler, 'REFRESH_WORKER', True):
        assert handler.handler({'input': {}, 'id': 't'})['refresh_worker'] is True


def test_refresh_exception_and_opt_out(monkeypatch):
    monkeypatch.setenv('LEGACY_UPSTREAM_INPUT', 'true')
    with patch.object(handler, '_handle_job', side_effect=RuntimeError('oops')), patch.object(handler, 'REFRESH_WORKER', True):
        assert handler.handler({'input': {}, 'id': 't'})['refresh_worker'] is True
    with patch.object(handler, '_handle_job', return_value={'images': []}), patch.object(handler, 'REFRESH_WORKER', False):
        assert 'refresh_worker' not in handler.handler({'input': {}, 'id': 't'})


def test_senai_protocol_dispatches_to_run_job():
    job = {'id': 'job-1', 'input': {'protocol': SENAI_PROTOCOL, 'health_check': True}}
    canned = {'status': 'healthy', 'protocol': SENAI_PROTOCOL, 'worker': {}, 'timings': {}}
    with patch.object(handler, 'senai_worker') as fake_worker:
        fake_worker.run_job.return_value = canned
        result = handler.handler(job)
    fake_worker.run_job.assert_called_once()
    assert fake_worker.run_job.call_args.args[0] is job
    assert result is canned


def test_unknown_protocol_rejected_without_gpu_work():
    result = handler.handler({'id': 'job-2', 'input': {'protocol': 'not-senai'}})
    assert result['status'] == 'error'
    assert result['failure']['code'] == 'UNSUPPORTED_PROTOCOL'


def test_missing_protocol_rejected_when_legacy_disabled(monkeypatch):
    monkeypatch.delenv('LEGACY_UPSTREAM_INPUT', raising=False)
    result = handler.handler({'id': 'job-3', 'input': {'workflow': {}}})
    assert result['status'] == 'error'
    assert result['failure']['code'] == 'UNSUPPORTED_PROTOCOL'


def test_missing_protocol_uses_legacy_path_when_enabled(monkeypatch):
    monkeypatch.setenv('LEGACY_UPSTREAM_INPUT', 'true')
    with patch.object(handler, '_handle_job', return_value={'images': []}) as legacy:
        result = handler.handler({'id': 'job-4', 'input': {'workflow': {}}})
    legacy.assert_called_once()
    assert result == {'images': []}


FAKE_PYTHON = '''#!/usr/bin/env python3
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
'''


def _fake_bin_dir(tmp_path):
    # Run the real shell entrypoint against lightweight process doubles.
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'python'
    # start.sh uses both python and python3; shebang must avoid recursion.
    fake.write_text('#!' + sys.executable + '\n' + FAKE_PYTHON.split('\n', 1)[1])
    fake.chmod(0o755)
    (bin_dir / 'python3').symlink_to(fake)
    return bin_dir


@pytest.mark.parametrize('scenario', ['completed', 'comfy_crash', 'signal'])
def test_start_supervises_and_reaps_children(tmp_path, scenario):
    bin_dir = _fake_bin_dir(tmp_path)
    env = {
        **os.environ, 'PATH': str(bin_dir) + ':' + os.environ['PATH'], 'TEST_ROOT': str(tmp_path),
        'SCENARIO': scenario, 'PUBLIC_KEY': '', 'WORKFLOW_MANIFESTS': '',
        'COMFY_PID_FILE': str(tmp_path / 'comfyui.pid'),
        'SENAI_BOOT_TIMELINE': str(tmp_path / 'timeline'),
        'SENAI_WORKER_STATE': str(tmp_path / 'state.json'),
        # Shortened so the comfy_crash scenario's grace window doesn't blow the test timeout;
        # production default (30s) lives in start.sh, not in the contract env list.
        'SENAI_HANDLER_GRACE_SEC': '1',
    }
    proc = subprocess.Popen(['bash', str(ROOT / 'src/start.sh')], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        if scenario == 'signal':
            deadline = time.monotonic() + 5
            while not ((tmp_path / 'handler.pid').exists() and (tmp_path / 'comfy.pid').exists()) and time.monotonic() < deadline:
                time.sleep(.02)
            assert (tmp_path / 'handler.pid').exists()
            assert (tmp_path / 'comfy.pid').exists()
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


def test_boot_continues_unready_and_records_full_timeline(tmp_path):
    # cache-only with an empty model cache: the model stage reports unready,
    # but start.sh must still launch both ComfyUI and the handler.
    bin_dir = _fake_bin_dir(tmp_path)
    env = {
        **os.environ, 'PATH': str(bin_dir) + ':' + os.environ['PATH'], 'TEST_ROOT': str(tmp_path),
        'SCENARIO': 'both_run', 'PUBLIC_KEY': '', 'WORKFLOWS': 'ltx25.yaml',
        'WORKFLOW_DIR': str(ROOT / 'workflow'), 'COMFY_MODEL_ROOT': str(tmp_path / 'models'),
        'HF_CACHE_ROOT': str(tmp_path / 'hf-cache'), 'MODEL_DOWNLOAD_POLICY': 'cache-only',
        'COMFY_PID_FILE': str(tmp_path / 'comfyui.pid'),
        'SENAI_BOOT_TIMELINE': str(tmp_path / 'timeline'),
        'SENAI_WORKER_STATE': str(tmp_path / 'state.json'),
        'WORKFLOW_MODEL_PATHS': str(tmp_path / 'paths.yaml'),
    }
    proc = subprocess.Popen(['bash', str(ROOT / 'src/start.sh')], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 5
        while not ((tmp_path / 'comfy.pid').exists() and (tmp_path / 'handler.pid').exists()) and time.monotonic() < deadline:
            time.sleep(.02)
        assert (tmp_path / 'comfy.pid').exists(), 'ComfyUI must start even while models are unready'
        assert (tmp_path / 'handler.pid').exists(), 'The handler must start even while models are unready'
        proc.terminate()
        output, _ = proc.communicate(timeout=10)
        assert proc.returncode == 143, output.decode()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    stages = [line.split()[0] for line in (tmp_path / 'timeline').read_text().splitlines()]
    assert stages == ['start', 'gpu_check', 'model_verify', 'comfy_start']


LOGGING_FAKE_PYTHON = '''#!/usr/bin/env python3
import os,sys,time
from pathlib import Path
if '-c' in sys.argv:
    print('OK: mock GPU'); sys.exit(0)
if any('workflow_models.py' in arg for arg in sys.argv):
    with open(os.environ['MODEL_CALL_LOG'], 'a') as f:
        f.write(' '.join(sys.argv[1:]) + '\\n')
    sys.exit(0)
role = 'comfy' if any('main.py' in arg for arg in sys.argv) else 'handler'
Path(os.environ['TEST_ROOT'], role+'.pid').write_text(str(os.getpid()))
time.sleep(60)
'''


def test_default_download_policy_never_downloads_at_boot(tmp_path):
    # No MODEL_DOWNLOAD_POLICY set: the model stage must only ever invoke
    # `workflow_models.py --verify` (cache-only default), never a bare
    # (potentially multi-GB) download call.
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'python'
    fake.write_text('#!' + sys.executable + '\n' + LOGGING_FAKE_PYTHON.split('\n', 1)[1])
    fake.chmod(0o755)
    (bin_dir / 'python3').symlink_to(fake)
    log_path = tmp_path / 'model_calls.log'

    env = {
        **os.environ, 'PATH': str(bin_dir) + ':' + os.environ['PATH'], 'TEST_ROOT': str(tmp_path),
        'MODEL_CALL_LOG': str(log_path), 'PUBLIC_KEY': '', 'WORKFLOW_MANIFESTS': '',
        'COMFY_PID_FILE': str(tmp_path / 'comfyui.pid'),
        'SENAI_BOOT_TIMELINE': str(tmp_path / 'timeline'),
        'SENAI_WORKER_STATE': str(tmp_path / 'state.json'),
    }
    env.pop('MODEL_DOWNLOAD_POLICY', None)
    proc = subprocess.Popen(['bash', str(ROOT / 'src/start.sh')], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / 'handler.pid').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (tmp_path / 'handler.pid').exists()
        proc.terminate()
        output, _ = proc.communicate(timeout=10)
        assert proc.returncode == 143, output.decode()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    calls = log_path.read_text().splitlines() if log_path.exists() else []
    assert calls, 'workflow_models.py --verify should have run at least once'
    assert all('--verify' in call for call in calls), calls


@pytest.mark.parametrize('result', [{'images': [{'filename': 'x.png'}]}, {'error': 'invalid workflow'}])
def test_real_runpod_sdk_reports_result_with_stop_pod(result, monkeypatch):
    import asyncio
    from runpod.serverless.modules.rp_job import run_job
    monkeypatch.setenv('LEGACY_UPSTREAM_INPUT', 'true')
    with patch.object(handler, '_handle_job', return_value=result.copy()), patch.object(handler, 'REFRESH_WORKER', True):
        reply = asyncio.run(run_job(handler.handler, {'id': 'local-sdk-test', 'input': {}}))
    assert reply['stopPod'] is True
    if 'error' in result:
        assert reply['error'] == result['error']
    else:
        assert reply['output'] == result


@pytest.mark.parametrize('refresh_worker', [False, True])
def test_real_runpod_sdk_keeps_protocol_failure_output_intact(refresh_worker):
    """Contract 0.2.0 §4/§9a: the protocol error object lives at output["failure"],
    never output["error"] — runpod's real rp_job.run_job() does
    job_output.pop("error", None) / pop("refresh_worker", None) on whatever dict
    handler() returns (rp_job.py:192-193) *before* assigning it to
    run_result["output"]. Because senai_worker never puts an "error" key in the
    dict it returns (only "failure"), that pop is a no-op here: `output` survives
    intact, run_result never gets a top-level `error`, and `refresh_worker` (when
    set) is popped into `stopPod` as designed.
    """
    import asyncio
    from runpod.serverless.modules.rp_job import run_job
    protocol_error_output = {
        'status': 'error', 'protocol': SENAI_PROTOCOL,
        'failure': {'type': 'timeout', 'code': 'NO_PROGRESS', 'stage': 'execute', 'message': 'stuck',
                    'infra': True, 'retryable': True, 'gpu_work': True},
    }
    if refresh_worker:
        protocol_error_output = {**protocol_error_output, 'refresh_worker': True}
    job = {'id': 'local-sdk-test-2', 'input': {'protocol': SENAI_PROTOCOL, 'health_check': True}}
    with patch.object(handler, 'senai_worker') as fake_worker:
        fake_worker.run_job.return_value = protocol_error_output
        reply = asyncio.run(run_job(handler.handler, job))
    print(f'SDK run_job reply (refresh_worker={refresh_worker}):', reply)
    assert 'error' not in reply
    assert reply['output']['status'] == 'error'
    assert reply['output']['failure'] == protocol_error_output['failure']
    assert 'refresh_worker' not in reply['output']
    assert reply.get('stopPod') is (True if refresh_worker else None)


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
