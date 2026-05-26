"""
Dashboard token 认证（M1 拆分自 dashboard.py）

提供：
  * ``check_api_token`` 装饰器：要求 ``X-Dashboard-Token`` 头
  * ``require_auth``  函数：在路由处理函数顶部调用
  * ``Unauthorized`` 异常类（兼容旧 dashboard 内的同名类）

设计点：
  * ``DASHBOARD_TOKEN`` 未设置时直接放行（开发模式）
  * 用 ``hmac.compare_digest`` 做常数时间比较，防 timing attack
"""

from __future__ import annotations

import hmac
import os
from functools import wraps

from flask import jsonify, request


class Unauthorized(Exception):
    """API token 校验失败时抛出，由路由层捕获并返回 401"""


def check_api_token(f):
    """装饰器：校验 ``X-Dashboard-Token`` 头部"""
    @wraps(f)
    def decorated(*args, **kwargs):
        expected_token = os.environ.get('DASHBOARD_TOKEN', '')
        if not expected_token:
            return f(*args, **kwargs)
        provided_token = request.headers.get('X-Dashboard-Token', '')
        if not hmac.compare_digest(provided_token, expected_token):
            return jsonify({'error': 'Unauthorized',
                            'message': 'Invalid or missing X-Dashboard-Token'}), 401
        return f(*args, **kwargs)
    return decorated


def require_auth() -> None:
    """页面级守卫：在 HTML route 顶部调用，未授权时抛 :class:`Unauthorized`"""
    expected_token = os.environ.get('DASHBOARD_TOKEN', '')
    if not expected_token:
        return
    provided = request.headers.get('X-Dashboard-Token', '')
    if not hmac.compare_digest(provided, expected_token):
        raise Unauthorized('Invalid or missing X-Dashboard-Token')


__all__ = ['check_api_token', 'require_auth', 'Unauthorized']
