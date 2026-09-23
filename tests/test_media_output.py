import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
import media_output  # noqa: E402
from senai_errors import WorkerError  # noqa: E402

_RESPONSE_SCHEMA = json.loads((ROOT / 'contract/senai-worker-1/response.schema.json').read_text())
_OUTPUT_ENTRY_SCHEMA = {**_RESPONSE_SCHEMA['$defs']['outputEntry'], '$defs': _RESPONSE_SCHEMA['$defs']}

HAVE_FFMPEG = shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None


def _validate_output_entry(entry):
    jsonschema.validate(entry, _OUTPUT_ENTRY_SCHEMA)


def _run_ffmpeg(args):
    subprocess.run(['ffmpeg', '-y', *args], capture_output=True, check=True, timeout=30)


@pytest.fixture
def mp4_with_audio(tmp_path):
    path = tmp_path / 'clip.mp4'
    _run_ffmpeg([
        '-f', 'lavfi', '-i', 'testsrc=size=64x64:rate=24',
        '-f', 'lavfi', '-i', 'sine=frequency=440',
        '-t', '2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
        str(path),
    ])
    return path


@pytest.fixture
def mp4_without_audio(tmp_path):
    path = tmp_path / 'clip_silent.mp4'
    _run_ffmpeg([
        '-f', 'lavfi', '-i', 'testsrc=size=64x64:rate=24',
        '-t', '2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        str(path),
    ])
    return path


@pytest.fixture
def png_16x8(tmp_path):
    path = tmp_path / 'img.png'
    _run_ffmpeg(['-f', 'lavfi', '-i', 'color=c=red:size=16x8', '-frames:v', '1', str(path)])
    return path


# 1. mp4 h264+aac, 2s, 64x64, 24fps
@pytest.mark.skipif(not HAVE_FFMPEG, reason='ffmpeg/ffprobe not available')
def test_probe_media_mp4_with_audio(mp4_with_audio):
    result = media_output.probe_media(mp4_with_audio)
    assert result['media_type'] == 'video/mp4'
    assert result['width'] == 64
    assert result['height'] == 64
    assert result['fps'] == pytest.approx(24.0)
    assert result['has_audio'] is True
    assert result['duration_sec'] == pytest.approx(2.0, abs=0.1)


# 2. mp4 without audio
@pytest.mark.skipif(not HAVE_FFMPEG, reason='ffmpeg/ffprobe not available')
def test_probe_media_mp4_without_audio(mp4_without_audio):
    result = media_output.probe_media(mp4_without_audio)
    assert result['has_audio'] is False


# 3. PNG 16x8
@pytest.mark.skipif(not HAVE_FFMPEG, reason='ffmpeg/ffprobe not available')
def test_probe_media_png(png_16x8):
    result = media_output.probe_media(png_16x8)
    assert result['media_type'] == 'image/png'
    assert result['width'] == 16
    assert result['height'] == 8
    assert 'fps' not in result
    assert 'duration_sec' not in result


# 4. .png file whose content is actually an mp4
@pytest.mark.skipif(not HAVE_FFMPEG, reason='ffmpeg/ffprobe not available')
def test_probe_media_detects_content_not_extension(mp4_with_audio, tmp_path):
    fake_png = tmp_path / 'fake.png'
    fake_png.write_bytes(mp4_with_audio.read_bytes())
    result = media_output.probe_media(fake_png)
    assert result['media_type'] == 'video/mp4'


# 11. fake runner returns fixed ffprobe JSON, no real ffprobe invoked, always runs (also in CI)
def test_probe_media_with_fake_runner(tmp_path):
    fixture = {
        'format': {'format_name': 'mov,mp4,m4a,3gp,3g2,mj2', 'duration': '3.5'},
        'streams': [
            {'codec_type': 'video', 'width': 100, 'height': 50, 'avg_frame_rate': '30/1'},
            {'codec_type': 'audio'},
        ],
    }
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(fixture), stderr='')

    path = tmp_path / 'whatever.bin'
    path.write_bytes(b'not really media')
    result = media_output.probe_media(path, runner=fake_runner)
    assert result == {
        'media_type': 'video/mp4',
        'width': 100,
        'height': 50,
        'fps': 30.0,
        'duration_sec': 3.5,
        'has_audio': True,
    }
    assert len(calls) == 1
    assert calls[0][0] == 'ffprobe'


def test_probe_media_unknown_format_raises_internal(tmp_path):
    fixture = {'format': {'format_name': 'some_weird_container'}, 'streams': []}

    def fake_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(fixture), stderr='')

    path = tmp_path / 'whatever.bin'
    path.write_bytes(b'x')
    with pytest.raises(WorkerError) as exc_info:
        media_output.probe_media(path, runner=fake_runner)
    assert exc_info.value.code == 'INTERNAL'
    assert 'some_weird_container' in exc_info.value.message


def _write(tmp_path, name, content=b'x'):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _fake_probe(_path, **_kwargs):
    return {'media_type': 'image/png', 'width': 4, 'height': 4}


@pytest.fixture
def two_node_history(tmp_path, monkeypatch):
    monkeypatch.setattr(media_output, 'probe_media', _fake_probe)
    f9 = _write(tmp_path, 'out9.png', b'nine')
    f10 = _write(tmp_path, 'out10.png', b'ten')
    ftemp = _write(tmp_path, 'temp.png', b'temp')
    paths = {'out9.png': f9, 'out10.png': f10, 'temp.png': ftemp}
    history = {
        '10': {'images': [{'filename': 'out10.png', 'subfolder': '', 'type': 'output'}]},
        '9': {'images': [
            {'filename': 'out9.png', 'subfolder': '', 'type': 'output'},
            {'filename': 'temp.png', 'subfolder': '', 'type': 'temp'},
        ]},
    }

    def resolve_path(filename, subfolder, item_type):
        return paths[filename]

    return history, resolve_path


# 5. two nodes, one temp entry skipped, deterministic keys ordered by node_id
def test_collect_outputs_two_nodes_skips_temp(two_node_history):
    history, resolve_path = two_node_history
    s3_client = MagicMock()
    s3_client.generate_presigned_url.return_value = 'https://example.com/signed'
    entries = media_output.collect_outputs(
        history, resolve_path=resolve_path, trace={'generation_id': 'gen123', 'attempt': 2},
        rp_job_id='job1', s3_client=s3_client, bucket='bucket1',
    )
    assert len(entries) == 2
    assert entries[0]['node_id'] == '9'
    assert entries[0]['key'] == 'renders/gen123/2/00-out9.png'
    assert entries[1]['node_id'] == '10'
    assert entries[1]['key'] == 'renders/gen123/2/01-out10.png'


# 6. trace None falls back to renders/<rp_job_id>/0/...
def test_collect_outputs_no_trace_uses_rp_job_id(two_node_history):
    history, resolve_path = two_node_history
    s3_client = MagicMock()
    s3_client.generate_presigned_url.return_value = 'https://example.com/signed'
    entries = media_output.collect_outputs(
        history, resolve_path=resolve_path, trace=None,
        rp_job_id='job1', s3_client=s3_client, bucket='bucket1',
    )
    assert entries[0]['key'] == 'renders/job1/0/00-out9.png'


# 7. put_object fails twice then succeeds: 1 output, 3 calls total
def test_collect_outputs_retries_upload_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(media_output, 'probe_media', _fake_probe)
    monkeypatch.setattr(media_output.time, 'sleep', lambda _seconds: None)
    f = _write(tmp_path, 'out.png')
    history = {'1': {'images': [{'filename': 'out.png', 'subfolder': '', 'type': 'output'}]}}
    s3_client = MagicMock()
    s3_client.put_object.side_effect = [Exception('boom'), Exception('boom'), None]
    s3_client.generate_presigned_url.return_value = 'https://example.com/signed'
    entries = media_output.collect_outputs(
        history, resolve_path=lambda *_: f, trace={'generation_id': 'g', 'attempt': 1},
        rp_job_id='job1', s3_client=s3_client, bucket='bucket1',
    )
    assert len(entries) == 1
    assert s3_client.put_object.call_count == 3


# 8. put_object always fails -> UPLOAD_FAILED
def test_collect_outputs_upload_always_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(media_output, 'probe_media', _fake_probe)
    monkeypatch.setattr(media_output.time, 'sleep', lambda _seconds: None)
    f = _write(tmp_path, 'out.png')
    history = {'1': {'images': [{'filename': 'out.png', 'subfolder': '', 'type': 'output'}]}}
    s3_client = MagicMock()
    s3_client.put_object.side_effect = Exception('boom')
    with pytest.raises(WorkerError) as exc_info:
        media_output.collect_outputs(
            history, resolve_path=lambda *_: f, trace={'generation_id': 'g', 'attempt': 1},
            rp_job_id='job1', s3_client=s3_client, bucket='bucket1',
        )
    assert exc_info.value.code == 'UPLOAD_FAILED'
    assert s3_client.put_object.call_count == 3


# 9. history without non-temp output -> OUTPUT_EMPTY
def test_collect_outputs_empty_history_raises_output_empty():
    history = {'1': {'images': [{'filename': 'temp.png', 'subfolder': '', 'type': 'temp'}]}}
    s3_client = MagicMock()
    with pytest.raises(WorkerError) as exc_info:
        media_output.collect_outputs(
            history, resolve_path=lambda *_: None, trace={'generation_id': 'g', 'attempt': 1},
            rp_job_id='job1', s3_client=s3_client, bucket='bucket1',
        )
    assert exc_info.value.code == 'OUTPUT_EMPTY'


# 10. s3_client=None -> OUTPUT_NOT_CONFIGURED, base64 never used
def test_collect_outputs_no_s3_client_raises_output_not_configured():
    history = {'1': {'images': [{'filename': 'out.png', 'subfolder': '', 'type': 'output'}]}}
    with pytest.raises(WorkerError) as exc_info:
        media_output.collect_outputs(
            history, resolve_path=lambda *_: None, trace={'generation_id': 'g', 'attempt': 1},
            rp_job_id='job1', s3_client=None, bucket=None,
        )
    assert exc_info.value.code == 'OUTPUT_NOT_CONFIGURED'


# 12. every entry from case 5 validates against response.schema.json $defs.outputEntry
def test_collect_outputs_entries_match_output_entry_schema(two_node_history):
    history, resolve_path = two_node_history
    s3_client = MagicMock()
    s3_client.generate_presigned_url.return_value = 'https://example.com/signed'
    entries = media_output.collect_outputs(
        history, resolve_path=resolve_path, trace={'generation_id': 'gen123', 'attempt': 2},
        rp_job_id='job1', s3_client=s3_client, bucket='bucket1',
    )
    for entry in entries:
        _validate_output_entry(entry)


def test_make_s3_client_no_bucket_env(monkeypatch):
    monkeypatch.delenv('AWS_BUCKET_NAME', raising=False)
    client, bucket = media_output.make_s3_client()
    assert client is None
    assert bucket is None


def test_make_s3_client_with_bucket_env(monkeypatch):
    monkeypatch.setenv('AWS_BUCKET_NAME', 'staging')
    client, bucket = media_output.make_s3_client()
    assert bucket == 'staging'
    assert client is not None
