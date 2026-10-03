"""OUTPUT_TRANSCODE: optional libx264 re-encode of video outputs before upload."""
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
import media_output  # noqa: E402
from senai_errors import WorkerError  # noqa: E402

HAVE_FFMPEG = shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason='ffmpeg/ffprobe not installed')


def _ffmpeg(args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], capture_output=True, check=True, timeout=60)


def _probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format', str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.fixture
def sliced_mp4(tmp_path):
    """A small h264+aac clip encoded the way PyAV/SaveVideo does today (CRF 23, sliced threads)."""
    path = tmp_path / 'Senfers1_t2v_00001_.mp4'
    _ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=size=128x128:rate=24', '-f', 'lavfi', '-i', 'sine=frequency=440',
             '-t', '1', '-c:v', 'libx264', '-crf', '23', '-threads', '4', '-x264-params', 'sliced-threads=1',
             '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '64k', str(path)])
    return path


@pytest.fixture
def png(tmp_path):
    path = tmp_path / 'still.png'
    _ffmpeg(['-f', 'lavfi', '-i', 'color=c=red:size=16x8', '-frames:v', '1', str(path)])
    return path


def _s3():
    s3 = MagicMock()
    uploaded = {}

    def put_object(Bucket, Key, Body, ContentType):
        uploaded[Key] = Body.read()

    s3.put_object.side_effect = put_object
    s3.generate_presigned_url.side_effect = lambda op, Params, ExpiresIn: f"https://r2.example/{Params['Key']}"
    return s3, uploaded


def _collect(path, category, transcode):
    s3, uploaded = _s3()
    history = {'75': {category: [{'filename': path.name, 'subfolder': '', 'type': 'output'}]}}
    entries = media_output.collect_outputs(
        history, resolve_path=lambda f, sub, t: path.parent / sub / f,
        trace={'generation_id': 'gen-1', 'attempt': 1}, rp_job_id='job-1',
        s3_client=s3, bucket='b', transcode=transcode)
    return entries, uploaded


@pytest.mark.parametrize('value,crf', [(None, None), ('', None), ('  ', None), ('h264-crf16', 16), ('h264-crf0', 0), ('h264-crf51', 51)])
def test_parse_output_transcode_accepts(value, crf):
    assert media_output.parse_output_transcode(value) == crf


@pytest.mark.parametrize('value', ['h264', 'h264-crf52', 'av1-crf30', 'crf16', 'h264-crf-1'])
def test_parse_output_transcode_rejects(value):
    with pytest.raises(WorkerError) as exc:
        media_output.parse_output_transcode(value)
    assert exc.value.code == 'INTERNAL'


def test_transcode_command_uses_frame_threads_and_copies_audio():
    cmd = media_output.transcode_command('in.mp4', 'out.mp4', 16)
    joined = ' '.join(cmd)
    assert '-c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p' in joined
    assert '-x264-params sliced-threads=0' in joined
    assert '-c:a copy' in joined and '-map 0:a?' in joined
    assert '-movflags +faststart' in joined


@needs_ffmpeg
def test_unset_uploads_saved_file_unchanged(sliced_mp4):
    entries, uploaded = _collect(sliced_mp4, 'videos', None)
    assert uploaded[entries[0]['key']] == sliced_mp4.read_bytes()
    assert entries[0]['key'] == 'renders/gen-1/1/00-Senfers1_t2v_00001_.mp4'


@needs_ffmpeg
def test_transcode_reencodes_video_and_keeps_audio(sliced_mp4, tmp_path):
    entries, uploaded = _collect(sliced_mp4, 'videos', 'h264-crf16')
    (entry,) = entries
    assert entry['key'] == 'renders/gen-1/1/00-Senfers1_t2v_00001_.mp4'
    body = uploaded[entry['key']]
    assert body != sliced_mp4.read_bytes()
    assert entry['bytes'] == len(body) and entry['media_type'] == 'video/mp4'
    assert entry['has_audio'] is True and (entry['width'], entry['height']) == (128, 128)

    out = tmp_path / 'uploaded.mp4'
    out.write_bytes(body)
    info = _probe(out)
    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
    audio = next(s for s in info['streams'] if s['codec_type'] == 'audio')
    src_audio = next(s for s in _probe(sliced_mp4)['streams'] if s['codec_type'] == 'audio')
    assert video['codec_name'] == 'h264' and video['pix_fmt'] == 'yuv420p'
    assert audio['codec_name'] == 'aac' and audio['bit_rate'] == src_audio['bit_rate']  # copied, not re-encoded
    # x264 writes its settings into the stream SEI: CRF 16, frame threads, no slices.
    assert b'crf=16.0' in body and b'sliced_threads=0' in body and b'sliced_threads=1' not in body
    # +faststart: moov before mdat.
    assert body.index(b'moov') < body.index(b'mdat')


@needs_ffmpeg
def test_transcode_leaves_images_alone(png):
    entries, uploaded = _collect(png, 'images', 'h264-crf16')
    assert uploaded[entries[0]['key']] == png.read_bytes()
    assert entries[0]['media_type'] == 'image/png'


@needs_ffmpeg
def test_transcode_cleans_up_temp_dir(sliced_mp4, monkeypatch, tmp_path):
    work = tmp_path / 'work'
    monkeypatch.setattr(media_output.tempfile, 'mkdtemp', lambda prefix: (work.mkdir(), str(work))[1])
    _collect(sliced_mp4, 'videos', 'h264-crf16')
    assert not work.exists()
    assert sliced_mp4.exists()  # the ComfyUI output itself is never touched


def test_transcode_failure_is_an_error_not_a_silent_fallback(tmp_path):
    runner = MagicMock(return_value=MagicMock(returncode=1, stderr='Unknown encoder libx264'))
    with pytest.raises(WorkerError) as exc:
        media_output.transcode_video(tmp_path / 'in.mp4', tmp_path / 'out.mp4', 16, runner=runner)
    assert exc.value.code == 'INTERNAL' and 'libx264' in str(exc.value)


def test_transcode_timeout_is_an_error(tmp_path):
    runner = MagicMock(side_effect=subprocess.TimeoutExpired('ffmpeg', 900))
    with pytest.raises(WorkerError, match='timed out'):
        media_output.transcode_video(tmp_path / 'in.mp4', tmp_path / 'out.mp4', 16, runner=runner)
