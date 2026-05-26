"""
Dashboard 模块化包（M1 修复 — 2026-05）

历史背景
========
``dashboard.py`` 单文件原本 1535 行，职责包括：
  * Flask + SocketIO 初始化、admin/Prometheus blueprint 挂载
  * Token 认证、ETag/Last-Modified 缓存
  * 数据读取（trades / risk / pool / pnl chart / execution metrics）
  * 事件抽取（_extract_events + mtime 缓存）
  * 实时价格拉取 + background_push 协程
  * 30+ 个 ``@app.route`` API / 页面端点
  * SocketIO 事件回调

混在一起后任何一处修改都可能引发"风控暂停事件 cache 未失效""ETag 用了
错误的 mtime"等回归。M1 修复将无副作用的纯函数 helper 拆到本包子模块，
``dashboard.py`` 仅保留**路由层**（@app.route / @socketio.on）作为门面。

子模块
======
  * ``auth``        — token 认证、_require_auth / Unauthorized
  * ``data``        — get_dashboard_data / _build_execution_metrics 等
  * ``events``      — _extract_events + mtime 缓存
  * ``live_prices`` — _fetch_live_prices / _inject_live_prices / background_push
  * ``etag``        — _make_etag_response 帮手

兼容性
------
``dashboard.py`` 依旧是进程入口（``python3 dashboard.py``）。本包子模块
只对内使用，不直接对外暴露 Flask 路由。前端 / API 调用方零变动。
"""

from __future__ import annotations
