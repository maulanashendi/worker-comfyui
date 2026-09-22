"""Senai-compatible media envelope; legacy image responses remain the default."""
import base64
import mimetypes
import os
from pathlib import PurePosixPath

import boto3


def collect_senai_outputs(outputs, job_id, fetch, errors):
    if errors:
        return {'status': 'error', 'error': '; '.join(errors)}
    result = {}
    bucket = os.getenv('AWS_BUCKET_NAME')
    client = boto3.client('s3') if bucket else None
    index = 0
    for node in outputs.values():
        for category in ('images', 'videos', 'gifs', 'audio'):
            for item in node.get(category, []):
                if item.get('type') == 'temp':
                    continue
                filename = item.get('filename')
                if not filename:
                    raise ValueError('Output has no filename')
                data = fetch(filename, item.get('subfolder', ''), item.get('type', 'output'))
                if not data:
                    raise ValueError('Failed to fetch output media')
                media_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                entry = {'filename': filename, 'subfolder': item.get('subfolder', ''), 'media_type': media_type}
                if client:
                    key = f'renders/{job_id}/{index:02d}-{PurePosixPath(filename).name}'
                    client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=media_type)
                    entry.update(type='url', data=client.generate_presigned_url('get_object', Params={'Bucket': bucket, 'Key': key}, ExpiresIn=86400))
                else:
                    if len(data) > 5 * 1024 * 1024:
                        raise ValueError('Configure AWS_BUCKET_NAME for outputs larger than 5 MiB')
                    entry.update(type='base64', data=base64.b64encode(data).decode('ascii'))
                result.setdefault(category, []).append(entry)
                index += 1
    if not result:
        return {'status': 'error', 'error': 'Workflow produced no media'}
    return {'status': 'success', 'output': result}
