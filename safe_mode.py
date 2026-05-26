#!/usr/bin/env python3
"""
SAFE_MODE 安全模式 (M3 修复)
================================

当启动时配置一致性校验出现 ERROR（如 default_stake > account_balance × max_position_pct
导致永远无法开仓），系统不应静默吞掉警告继续跑实盘——但完全停止进程会让 dashboard /
TG bot / 持仓监控也下线，反而让运维更难处理。

折中方案：写入一个 .safe_mode 标记文件，含原因 + 时间戳。
  - can_open_trade() 入口立即拒绝（带明确原因 + TG 告警）
  - dashboard / monitoring / tracker / journal_recovery 正常运行
  - 用户从 admin panel 修复配置后调 clear_safe_mode() 即可恢复

设计点：
  - 用文件而非内存变量：跨 scheduler / dashboard / realtime_monitor 三个进程共享
  - 标记很小（< 1KB），所有进程读时缓存 5s 避免每次开仓 stat 文件
  - 文件不存在 → 系统正常；文件存在 → SAFE_MODE 激活
"""

import json
import os
import threading
import time
from typing import Optional

from common import (
    setup_logger, utcnow_iso, atomic_write_json, load_json,
)

logger = setup_logger("safe_mode")

# 标记文件路径（与其他运行时数据并列）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SAFE_MODE_FILE = os.path.join(SCRIPT_DIR, '.safe_mode')

# 内存缓存（避免每次开仓 stat 文件）
_cache: dict = {'value': None, 'expire': 0.0}
_cache_lock = threading.Lock()
_CACHE_TTL_SEC = 5.0


def is_safe_mode() -> bool:
    """是否处于 SAFE_MODE。命中缓存 5s。"""
    return get_safe_mode_info() is not None


def get_safe_mode_info() -> Optional[dict]:
    """
    返回 SAFE_MODE 详情 dict，若未激活返回 None。

    返回字段：
      - reason: 原因（多行字符串）
      - errors: 触发 ERROR 的列表
      - activated_at: ISO 时间戳
      - source: 'startup' | 'manual' | 'panic'
    """
    now = time.time()
    with _cache_lock:
        if now < _cache['expire']:
            return _cache['value']

    if not os.path.exists(SAFE_MODE_FILE):
        with _cache_lock:
            _cache['value'] = None
            _cache['expire'] = now + _CACHE_TTL_SEC
        return None

    info = load_json(SAFE_MODE_FILE, None)
    with _cache_lock:
        _cache['value'] = info
        _cache['expire'] = now + _CACHE_TTL_SEC
    return info


def set_safe_mode(reason: str, errors: Optional[list] = None,
                  source: str = 'startup') -> None:
    """
    激活 SAFE_MODE 并写文件。多次调用累加 errors。

    Args:
      reason: 短描述
      errors: 详细 ERROR 列表
      source: 'startup' | 'manual' | 'panic'
    """
    existing = get_safe_mode_info() or {}
    merged_errors = list(existing.get('errors', []) or [])
    if errors:
        for e in errors:
            if e not in merged_errors:
                merged_errors.append(e)

    info = {
        'reason': reason,
        'errors': merged_errors,
        'activated_at': existing.get('activated_at') or utcnow_iso(),
        'updated_at': utcnow_iso(),
        'source': source,
    }
    try:
        atomic_write_json(SAFE_MODE_FILE, info)
        # 失效缓存
        with _cache_lock:
            _cache['value'] = info
            _cache['expire'] = time.time() + _CACHE_TTL_SEC
        logger.error(
            f"🚫 SAFE_MODE 已激活 [{source}] {reason} "
            f"({len(merged_errors)} 项 ERROR)"
        )
    except Exception as e:
        logger.error(f"写 SAFE_MODE 文件失败: {e}")


def clear_safe_mode() -> bool:
    """
    手动清除 SAFE_MODE。仅 admin panel / 运维操作调用。
    成功返回 True，标记不存在返回 False。
    """
    if not os.path.exists(SAFE_MODE_FILE):
        return False
    try:
        os.remove(SAFE_MODE_FILE)
        with _cache_lock:
            _cache['value'] = None
            _cache['expire'] = time.time() + _CACHE_TTL_SEC
        logger.warning("✅ SAFE_MODE 已手动清除")
        return True
    except Exception as e:
        logger.error(f"清除 SAFE_MODE 失败: {e}")
        return False


def safe_mode_reason_text() -> str:
    """供 TG / dashboard 显示的多行原因文本。"""
    info = get_safe_mode_info()
    if not info:
        return ''
    lines = [
        f"原因: {info.get('reason', '')}",
        f"激活源: {info.get('source', '')}",
        f"激活时间: {info.get('activated_at', '')}",
    ]
    errors = info.get('errors') or []
    if errors:
        lines.append("")
        lines.append("详细 ERROR:")
        for e in errors[:10]:  # TG 单条上限保护
            lines.append(f"  • {e}")
        if len(errors) > 10:
            lines.append(f"  ... 还有 {len(errors) - 10} 项 (见 {SAFE_MODE_FILE})")
    return "\n".join(lines)
