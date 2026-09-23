"""Output collection for protocol senai-worker/1: ffprobe-derived metadata, R2 upload, presigned URLs."""
import hashlib
import json
import os
import subprocess
import time
from pathlib import PurePosixPath

import boto3

from senai_errors import WorkerError


_FORMAT_NAME_TO_MEDIA_TYPE = {
    'matroska,webm': 'video/webm',
    'png_pipe': 'image/png',
    'image2': 'image/jpeg',
    'jpeg_pipe': 'image/jpeg',
    'webp_pipe': 'image/webp',
    'gif': 'image/gif',
    'wav': 'audio/wav',
    'mp3': 'audio/mpeg',
    'flac': 'audio/flac',
    'ogg': 'audio/ogg',
}
_MP4_FAMILY_FORMAT_NAME = 'mov,mp4,m4a,3gp,3g2,mj2'


def _parse_frame_rate(value):
    num, _, den = value.partition('/')
    den = den or '1'
    try:
        numerator, denominator = int(num), int(den)
    except ValueError:
        return None
    if denominator == 0:
        return None
    return numerator / denominator


def probe_media(path, *, runner=subprocess.run):
    cmd = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', str(path)]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise WorkerError('INTERNAL', f'ffprobe timed out probing {path}: {exc}') from exc
    if proc.returncode != 0:
        raise WorkerError('INTERNAL', f'ffprobe failed (rc={proc.returncode}) for {path}: {proc.stderr[:200]}')
    data = json.loads(proc.stdout)
    fmt = data.get('format', {})
    streams = data.get('streams', [])
    video_stream = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio_stream = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    format_name = fmt.get('format_name', '')

    if format_name == _MP4_FAMILY_FORMAT_NAME:
        media_type = 'video/mp4' if video_stream else 'audio/mp4'
    elif format_name in _FORMAT_NAME_TO_MEDIA_TYPE:
        media_type = _FORMAT_NAME_TO_MEDIA_TYPE[format_name]
    else:
        raise WorkerError('INTERNAL', f'unrecognized ffprobe format_name {format_name!r} for {path}')

    result = {'media_type': media_type}
    if media_type.startswith('video/'):
        if video_stream is not None:
            result['width'] = int(video_stream['width'])
            result['height'] = int(video_stream['height'])
            avg_frame_rate = video_stream.get('avg_frame_rate')
            if avg_frame_rate:
                fps = _parse_frame_rate(avg_frame_rate)
                if fps is not None:
                    result['fps'] = fps
        duration = fmt.get('duration')
        if duration is not None:
            result['duration_sec'] = float(duration)
        result['has_audio'] = audio_stream is not None
    elif media_type.startswith('image/'):
        if video_stream is not None:
            result['width'] = int(video_stream['width'])
            result['height'] = int(video_stream['height'])
    elif media_type.startswith('audio/'):
        duration = fmt.get('duration')
        if duration is not None:
            result['duration_sec'] = float(duration)
    return result


def _hash_file(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _upload(s3_client, bucket, key, path, media_type, presign_ttl_sec):
    last_exc = None
    for attempt_no in range(3):
        try:
            with open(path, 'rb') as handle:
                s3_client.put_object(Bucket=bucket, Key=key, Body=handle, ContentType=media_type)
            url = s3_client.generate_presigned_url(
                'get_object', Params={'Bucket': bucket, 'Key': key}, ExpiresIn=presign_ttl_sec
            )
            if not url.startswith('https://'):
                raise WorkerError('UPLOAD_FAILED', f'presigned URL for {key} is not https')
            return url
        except WorkerError:
            raise
        except Exception as exc:  # noqa: BLE001 - any upload/presign failure is retried
            last_exc = exc
            if attempt_no < 2:
                time.sleep((1, 2)[attempt_no])
    raise WorkerError('UPLOAD_FAILED', f'upload of {key} failed after 3 attempts: {last_exc}')


def _node_sort_key(node_id):
    return (0, int(node_id)) if node_id.isdigit() else (1, node_id)


def collect_outputs(history_outputs, *, resolve_path, trace, rp_job_id, s3_client=None,
                     bucket=None, prefix='renders', presign_ttl_sec=86400):
    if s3_client is None:
        raise WorkerError('OUTPUT_NOT_CONFIGURED', 'no S3 client configured for outputs')

    if trace:
        generation_id = trace['generation_id']
        attempt = trace['attempt']
    else:
        generation_id = rp_job_id
        attempt = 0

    entries = []
    index = 0
    for node_id in sorted(history_outputs.keys(), key=_node_sort_key):
        node = history_outputs[node_id]
        for category in ('images', 'videos', 'gifs', 'audio'):
            for item in node.get(category, []) or []:
                if item.get('type') == 'temp':
                    continue
                filename = item.get('filename')
                if not filename:
                    raise WorkerError('INTERNAL', f'output entry on node {node_id} has no filename')
                subfolder = item.get('subfolder', '')
                item_type = item.get('type', 'output')
                path = resolve_path(filename, subfolder, item_type)
                probe = probe_media(path)
                sha256, size = _hash_file(path)
                basename = PurePosixPath(filename).name
                key = f'{prefix}/{generation_id}/{attempt}/{index:02d}-{basename}'
                url = _upload(s3_client, bucket, key, path, probe['media_type'], presign_ttl_sec)
                entry = {
                    'url': url,
                    'bucket': bucket,
                    'key': key,
                    'filename': basename,
                    'node_id': node_id,
                    'media_type': probe['media_type'],
                    'bytes': size,
                    'sha256': sha256,
                }
                for field in ('width', 'height', 'duration_sec', 'fps', 'has_audio'):
                    if field in probe:
                        entry[field] = probe[field]
                entries.append(entry)
                index += 1

    if not entries:
        raise WorkerError('OUTPUT_EMPTY', 'workflow produced no non-temp outputs')
    return entries


def make_s3_client():
    bucket = os.getenv('AWS_BUCKET_NAME')
    if not bucket:
        return None, None
    return boto3.client('s3'), bucket
