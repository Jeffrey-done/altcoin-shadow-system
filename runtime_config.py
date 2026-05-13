#!/usr/bin/env python3
"""
运行时配置覆盖层 — 跨进程同步 config 变更 v1.0

为什么需要这个：
  `config.py` 里的参数是 Python 模块属性，直接改只影响当前进程。
  scheduler / realtime_monitor / dashboard 是三个独立进程，想从 admin panel
  改一个参数（比如 LIVE_MODE）并让三个进程都生效，必须借助文件。

工作方式：
  1. admin_panel 写 runtime_config.json（白名单字段，带时间戳）
  2. 所有进程在关键时刻调 apply_overrides()：
     - scheduler: 每轮 main_loop 开头
     - realtime_monitor: 每次 refresh_snapshot 前
     - dashboard: 每次 /api/data 请求前（已经够频繁）
     - admin_panel 自己写完后立刻 apply 一次
  3. apply_overrides 读文件 → 按白名单写回 config 模块属性

安全约束：
  - 只接受 ALLOWED 字典里白名单字段；其他键一律忽略
  - 所有值都做类型和范围校验；越界拒绝（不是 clip）
  - 文件权限 0600，和 admin_secrets.json 并列
  - 任何失败都走 fail-closed：出错时保持 config 原值（最保守）
"""

import json
import logging
import os
import stat
from typing import Any, Callable, Dict, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_CONFIG_FILE = os.path.join(SCRIPT_DIR, 'runtime_config.json')

logger = logging.getLogger("runtime_config")


# ══════════════════════════════════════════════════════════════════
#  允许从 admin panel 修改的字段白名单
# ══════════════════════════════════════════════════════════════════
#
# 格式: 'CONFIG_KEY': (type, validator_fn_or_None, human_label)
# validator 返回 (ok: bool, err_msg: str)；None 表示只做类型检查
#
# ⚠️  只暴露那些「误调一下不会立刻炸账户」的字段。
#     仓位/杠杆/止损类参数调大都可能直接加大亏损，所以每个都设了严格上限。
# ══════════════════════════════════════════════════════════════════

def _pct_validator(lo: float, hi: float):
    def v(x):
        if not (lo <= x <= hi):
            return False, f"必须在 [{lo}, {hi}] 区间"
        return True, ""
    return v


def _int_validator(lo: int, hi: int):
    def v(x):
        if not (lo <= x <= hi):
            return False, f"必须在 [{lo}, {hi}] 区间"
        return True, ""
    return v


def _enum_validator(allowed):
    def v(x):
        if x not in allowed:
            return False, f"必须是 {allowed} 之一"
        return True, ""
    return v


# 字段白名单（key, type, validator, human_label）
ALLOWED: Dict[str, Tuple[type, Callable, str]] = {
    # ── 实盘开关（最敏感）──
    'LIVE_MODE': (bool, None, 'Binance 实盘开关'),
    'OKX_LIVE_MODE': (bool, None, 'OKX 实盘开关'),
    'PRIMARY_EXCHANGE': (str,
                         _enum_validator(['binance', 'okx', 'both', 'auto']),
                         '实盘路由模式'),
    'PRIMARY_EXCHANGE_FALLBACK': (str,
                                   _enum_validator(['binance', 'okx']),
                                   'auto 模式下的默认选择'),

    # ── 仓位与杠杆（硬上限避免误操作）──
    # stake 上限 500U：就算你误把它设 9999 也只是上限失效，不会一次性爆账户
    'DEFAULT_STAKE': (int, _int_validator(5, 500), '单笔保证金 (U)'),
    'LEVERAGE': (int, _int_validator(1, 20), 'Binance 杠杆倍数'),
    'OKX_DEFAULT_LEVERAGE': (int, _int_validator(1, 20), 'OKX 杠杆倍数'),

    # ── 止盈止损档位（相对入场价的乘数）──
    # TP1 在 (0.80, 1.00)：做空 TP1 必须小于入场价
    'TP1_MULTIPLIER': (float, _pct_validator(0.80, 1.0), 'TP1 价格乘数'),
    'TP2_MULTIPLIER': (float, _pct_validator(0.70, 1.0), 'TP2 价格乘数'),
    'TP1_CLOSE_RATIO': (float, _pct_validator(0.1, 0.9), 'TP1 平仓比例'),
    'HARD_STOP_LOSS_PCT': (float, _pct_validator(1.0, 20.0), '硬止损 (%)'),

    # ── 风控阈值 ──
    'RISK_MAX_DAILY_LOSS': (float, _pct_validator(5, 500), '单日最大亏损 (U)'),
    'RISK_MAX_DAILY_TRADES': (int, _int_validator(1, 20), '单日最大开仓'),
    'RISK_CONSECUTIVE_LOSS_PAUSE': (int, _int_validator(2, 10), '连亏几次暂停'),
    'RISK_MAX_POSITION_PCT': (float, _pct_validator(0.1, 1.0), '最大持仓占比'),
    'COOLDOWN_HOURS': (int, _int_validator(1, 168), '止损后冷却期 (h)'),

    # ── 滑点告警 ──
    'SLIPPAGE_ALERT_PCT': (float, _pct_validator(0.1, 5.0), '滑点告警阈值 (%)'),
}


def validate_change(key: str, value: Any) -> Tuple[bool, str]:
    """
    单个字段的类型+范围校验。admin_panel 提交前必须调这个。

    返回 (ok, err_msg)。ok=True 时 err_msg 为空。
    """
    if key not in ALLOWED:
        return False, f"字段 {key} 不在白名单，拒绝修改"

    expected_type, validator, label = ALLOWED[key]

    # bool 必须严格匹配（Python 里 True 是 int 的子类，必须用 is instance 前先判）
    if expected_type is bool:
        if not isinstance(value, bool):
            return False, f"{label} 必须是 true/false 布尔值"
    elif expected_type is int:
        if isinstance(value, bool) or not isinstance(value, int):
            return False, f"{label} 必须是整数"
    elif expected_type is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, f"{label} 必须是数字"
        value = float(value)
    elif expected_type is str:
        if not isinstance(value, str):
            return False, f"{label} 必须是字符串"
    else:
        return False, f"{label} 类型未知"

    if validator:
        ok, err = validator(value)
        if not ok:
            return False, f"{label} {err}"

    return True, ""


# ══════════════════════════════════════════════════════════════════
#  读写 runtime_config.json
# ══════════════════════════════════════════════════════════════════

def load_overrides() -> dict:
    """读当前文件内容；不存在 / 损坏 → 返回 {}"""
    if not os.path.exists(RUNTIME_CONFIG_FILE):
        return {}
    try:
        with open(RUNTIME_CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, IOError, OSError) as e:
        logger.warning(f"读取 runtime_config.json 失败: {e}")
        return {}


def save_overrides(overrides: dict) -> None:
    """
    原子写入 + 0600 权限。
    注意：这个只应该被 admin_panel 调用；业务进程只读。
    """
    # 只保留白名单字段
    filtered = {k: v for k, v in overrides.items() if k in ALLOWED}

    tmp = RUNTIME_CONFIG_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(filtered, f, indent=2, ensure_ascii=False, sort_keys=True)
    # 0600
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, RUNTIME_CONFIG_FILE)


# ══════════════════════════════════════════════════════════════════
#  把 overrides 写回 config 模块
# ══════════════════════════════════════════════════════════════════

_last_applied_mtime = 0.0
_last_applied: dict = {}


def apply_overrides(force: bool = False) -> dict:
    """
    读 runtime_config.json 并把白名单字段写到 config 模块属性。

    幂等 + 懒加载：
      - 通过文件 mtime 判断是否有变化，没变化直接 return 上次结果
      - 业务进程可以在每个循环开头调，成本接近 0

    参数:
      force: 跳过 mtime 缓存，强制重新读文件（admin_panel 写完后用）

    返回：本次实际写入 config 的字段 dict（没变化时为 {}）
    """
    global _last_applied_mtime, _last_applied

    if not os.path.exists(RUNTIME_CONFIG_FILE):
        # 文件被删了 → 不主动回滚（config 里还是最后一次 apply 的值）
        # 如果确实需要回滚，重启进程即可
        return {}

    try:
        mtime = os.path.getmtime(RUNTIME_CONFIG_FILE)
    except OSError:
        return {}

    if not force and mtime == _last_applied_mtime:
        return {}

    overrides = load_overrides()
    applied = {}

    import config as _config

    for key, value in overrides.items():
        if key not in ALLOWED:
            continue
        ok, _err = validate_change(key, value)
        if not ok:
            logger.warning(f"runtime_config: {key}={value!r} 校验失败，跳过")
            continue
        old = getattr(_config, key, None)
        if old != value:
            setattr(_config, key, value)
            applied[key] = {'old': old, 'new': value}

    _last_applied_mtime = mtime
    _last_applied = applied

    if applied:
        logger.info(f"runtime_config 应用: {applied}")

    return applied


def get_current_values() -> dict:
    """
    admin_panel 查询当前生效值（读 config 模块而不是文件）。
    返回的 key 顺序与 ALLOWED 一致，方便前端稳定渲染。
    """
    import config as _config
    out = {}
    for key in ALLOWED.keys():
        out[key] = getattr(_config, key, None)
    return out
