"""
ETag / Last-Modified 缓存帮手（M1 拆分自 dashboard.py）
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from flask import jsonify, make_response, request


def make_etag_response(data):
    """
    把 ``data`` 序列化为 JSON 并附 ETag + Last-Modified；
    若客户端 If-None-Match 命中则返回 304。
    """
    content = json.dumps(data, ensure_ascii=False, sort_keys=True)
    etag = hashlib.md5(content.encode()).hexdigest()

    if_none_match = request.headers.get('If-None-Match', '')
    if if_none_match == etag:
        return make_response('', 304)

    resp = make_response(jsonify(data))
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = 'private, max-age=60'
    resp.headers['Last-Modified'] = datetime.now(timezone.utc).strftime(
        '%a, %d %b %Y %H:%M:%S GMT'
    )
    return resp


__all__ = ['make_etag_response']
