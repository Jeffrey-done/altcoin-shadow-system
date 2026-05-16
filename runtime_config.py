#!/usr/bin/env python3
"""
运行时配置覆盖层 — 跨进程同步 config 变更 v2.0（多账户版）

工作方式：
  1. admin_panel 写 runtime_config.json（白名单字段，带时间戳）
  2. 所有进程在关键时刻调 apply_overrides()
  3. apply_overrides 读文件 → 按白名单写回 config 模块属性

v2 多账户结构：
{
  "_global": { "LIVE_MODE": false, "OKX_LIVE_MODE": false, ... },
  "acc_abc123": { "ACCOUNT_BALANCE": 100, "DEFAULT_STAKE": 50, ... },
  "acc_def456": { "ACCOUNT_BALANCE": 500, ... }
}

全局字段(GLOBAL_FIELDS): 只存一份，所有账户共享
  LIVE_MODE, OKX_LIVE_MODE, PRIMARY_EXCHANGE, PRIMARY_EXCHANGE_FALLBACK

账户字段(ACCOUNT_FIELDS): 每个账户独立的风控/仓位参数
  ACCOUNT_BALANCE, DEFAULT_STAKE, LEVERAGE, ...
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

    # ── 系统资金池 ──
    'ACCOUNT_BALANCE': (int, _int_validator(10, 10000), '系统可用资金池 (U)'),

    # ── 仓位与杠杆 ──
    'DEFAULT_STAKE': (int, _int_validator(5, 500), '单笔保证金 (U)'),
    'LEVERAGE': (int, _int_validator(1, 20), 'Binance 杠杆倍数'),
    'OKX_DEFAULT_LEVERAGE': (int, _int_validator(1, 20), 'OKX 杠杆倍数'),

    # ── 止盈止损档位 ──
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

    # ── 影子并行 ──
    'SHADOW_PARALLEL': (bool, None, '影子并行模式（实盘时同步跑影子对照）'),
}

# ── 全局字段 vs 账户字段 ──
GLOBAL_FIELDS = {'LIVE_MODE', 'OKX_LIVE_MODE', 'PRIMARY_EXCHANGE', 'PRIMARY_EXCHANGE_FALLBACK', 'SHADOW_PARALLEL'}
ACCOUNT_FIELDS = set(ALLOWED.keys()) - GLOBAL_FIELDS


def validate_change(key: str, value: Any) -> Tuple[bool, str]:
    """
    单个字段的类型+范围校验。

    返回 (ok, err_msg)。ok=True 时 err_msg 为空。
    """
    if key not in ALLOWED:
        return False, f"字段 {key} 不在白名单，拒绝修改"

    expected_type, validator, label = ALLOWED[key]

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


def validate_cross_field_consistency(overrides: dict, account_id: str = None) -> tuple:
    """
    跨字段一致性校验：检测参数组合是否会导致系统功能异常。

    2026-05 修复：返回 (errors, warnings) 元组（旧版仅返回单一 list）：
      - errors:   阻止保存级别（admin panel 应拒绝并返回 400；保存会让系统永远拒绝开仓）
      - warnings: 提示级别（不阻止保存，但会记录日志/推送告警让用户知晓）

    当前规则:
      - DEFAULT_STAKE > ACCOUNT_BALANCE
        → ERROR（保证金超过本金，风控 can_open_trade 永远拒绝开仓）
      - DEFAULT_STAKE > ACCOUNT_BALANCE × RISK_MAX_POSITION_PCT 但 ≤ ACCOUNT_BALANCE
        → WARNING（首笔可开，但同时存在其他持仓时新开仓会被拒）

    向后兼容：返回的 tuple 仍可被 `if warnings:` 当作 truthy 判断，
    且单元素元组下迭代仍然得到字符串列表。原来的调用方
    `for w in warnings: ...` 在切到 `errors, warnings = validate_*` 后即可。

    参数:
      overrides: 即将保存的覆盖值 dict
      account_id: 指定账户 ID；None 使用活跃账户（用于读取当前 config 值做合并）

    返回: (errors: list[str], warnings: list[str])
    """
    import config as _config
    errors: list = []
    warnings_out: list = []

    # 合并：用 overrides 覆盖当前 config 值，得到"如果保存后"的生效值
    balance = overrides.get('ACCOUNT_BALANCE', getattr(_config, 'ACCOUNT_BALANCE', 100))
    stake = overrides.get('DEFAULT_STAKE', getattr(_config, 'DEFAULT_STAKE', 50))
    pos_pct = overrides.get('RISK_MAX_POSITION_PCT', getattr(_config, 'RISK_MAX_POSITION_PCT', 0.5))

    # 尝试从账户覆盖里读（如果指定了 account_id）
    if account_id:
        try:
            acc_overrides = load_account_overrides(account_id)
            balance = overrides.get('ACCOUNT_BALANCE', acc_overrides.get('ACCOUNT_BALANCE', balance))
            stake = overrides.get('DEFAULT_STAKE', acc_overrides.get('DEFAULT_STAKE', stake))
            pos_pct = overrides.get('RISK_MAX_POSITION_PCT', acc_overrides.get('RISK_MAX_POSITION_PCT', pos_pct))
        except Exception:
            pass

    # 确保数值类型
    try:
        balance = float(balance)
        stake = float(stake)
        pos_pct = float(pos_pct)
    except (TypeError, ValueError):
        return (errors, warnings_out)  # 类型有问题，单字段校验已经会报错

    max_position = balance * pos_pct

    if stake > balance:
        errors.append(
            f"❌ DEFAULT_STAKE({stake:.0f}U) > ACCOUNT_BALANCE({balance:.0f}U)，"
            f"保证金超过本金，风控将永远拒绝开仓。请提高 ACCOUNT_BALANCE 或降低 DEFAULT_STAKE。"
        )
    elif stake > max_position:
        warnings_out.append(
            f"⚠️ DEFAULT_STAKE({stake:.0f}U) > 最大持仓上限({max_position:.0f}U = "
            f"ACCOUNT_BALANCE {balance:.0f} × RISK_MAX_POSITION_PCT {pos_pct})，"
            f"同时存在其他持仓时新开仓将被拒绝。"
        )

    # 日志记录
    for e in errors:
        logger.error(f"配置一致性 ERROR: {e}")
    for w in warnings_out:
        logger.warning(f"配置一致性 WARNING: {w}")

    return (errors, warnings_out)


# ══════════════════════════════════════════════════════════════════
#  读写 runtime_config.json（v2 多账户格式）
# ══════════════════════════════════════════════════════════════════

def _load_raw_config() -> dict:
    """读取原始文件内容，处理 v1→v2 迁移"""
    if not os.path.exists(RUNTIME_CONFIG_FILE):
        return {'_global': {}}
    try:
        with open(RUNTIME_CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {'_global': {}}
    except (json.JSONDecodeError, IOError, OSError) as e:
        logger.warning(f"读取 runtime_config.json 失败: {e}")
        return {'_global': {}}

    # 检测是否是 v1 格式（没有 _global 键，直接是平的 key-value）
    if '_global' not in data:
        # v1 迁移：把全局字段放 _global，其余放活跃账户下
        logger.info("runtime_config: 检测到 v1 格式，自动迁移到 v2")
        v2 = {'_global': {}}
        # 获取活跃账户 ID
        try:
            import admin_secrets
            active_id = admin_secrets.get_active_account_id()
        except Exception:
            active_id = ''

        for key, value in data.items():
            if key.startswith('_'):
                continue
            if key in GLOBAL_FIELDS:
                v2['_global'][key] = value
            elif key in ACCOUNT_FIELDS and active_id:
                v2.setdefault(active_id, {})[key] = value

        _save_raw_config(v2)
        return v2

    return data


def _save_raw_config(data: dict) -> None:
    """原子写入配置文件"""
    tmp = RUNTIME_CONFIG_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, RUNTIME_CONFIG_FILE)


def load_overrides() -> dict:
    """
    读当前活跃账户的合并配置（_global + 活跃账户的覆盖）。
    兼容旧代码：返回平的 key→value dict。
    """
    data = _load_raw_config()
    merged = {}

    # 全局字段
    global_data = data.get('_global', {})
    for key in GLOBAL_FIELDS:
        if key in global_data:
            merged[key] = global_data[key]

    # 活跃账户字段
    try:
        import admin_secrets
        active_id = admin_secrets.get_active_account_id()
    except Exception:
        active_id = ''

    if active_id:
        account_data = data.get(active_id, {})
        for key in ACCOUNT_FIELDS:
            if key in account_data:
                merged[key] = account_data[key]

    return merged


def save_overrides(overrides: dict) -> None:
    """
    保存配置覆盖（兼容旧接口）。
    自动拆分全局字段和账户字段。

    防御纵深：调用 validate_cross_field_consistency 检查 errors，
    若发现致命组合（如 DEFAULT_STAKE > ACCOUNT_BALANCE）抛 ValueError 拒绝写盘。
    Admin panel 应当在调用本函数前先做 pre-flight 校验返回 400，
    本函数的检查是最后一道防线，避免直接调用方绕过 admin UI 写入坏配置。
    """
    data = _load_raw_config()

    try:
        import admin_secrets
        active_id = admin_secrets.get_active_account_id()
    except Exception:
        active_id = ''

    # 防御性拒绝：致命错误直接抛
    errors, _warnings = validate_cross_field_consistency(overrides, account_id=active_id)
    if errors:
        raise ValueError(
            "配置一致性校验失败，拒绝保存：\n" + "\n".join(errors)
        )

    # 拆分并保存
    for key, value in overrides.items():
        if key not in ALLOWED:
            continue
        if key in GLOBAL_FIELDS:
            data.setdefault('_global', {})[key] = value
        elif active_id:
            data.setdefault(active_id, {})[key] = value

    _save_raw_config(data)


def load_account_overrides(account_id: str) -> dict:
    """读取指定账户的配置覆盖"""
    data = _load_raw_config()
    return data.get(account_id, {})


def save_account_overrides(account_id: str, overrides: dict) -> None:
    """保存指定账户的配置覆盖"""
    data = _load_raw_config()
    filtered = {}
    for key, value in overrides.items():
        if key in ACCOUNT_FIELDS:
            ok, _ = validate_change(key, value)
            if ok:
                filtered[key] = value
    data[account_id] = filtered
    _save_raw_config(data)


def load_global_overrides() -> dict:
    """读取全局配置"""
    data = _load_raw_config()
    return data.get('_global', {})


def save_global_overrides(overrides: dict) -> None:
    """保存全局配置"""
    data = _load_raw_config()
    global_data = data.setdefault('_global', {})
    for key, value in overrides.items():
        if key in GLOBAL_FIELDS:
            ok, _ = validate_change(key, value)
            if ok:
                global_data[key] = value
    data['_global'] = global_data
    _save_raw_config(data)


# ══════════════════════════════════════════════════════════════════
#  把 overrides 写回 config 模块
# ══════════════════════════════════════════════════════════════════

_last_applied_mtime = 0.0
_last_applied: dict = {}


def apply_overrides(force: bool = False) -> dict:
    """
    读 runtime_config.json 并把白名单字段写到 config 模块属性。

    使用活跃账户的配置合并全局配置。
    """
    global _last_applied_mtime, _last_applied

    if not os.path.exists(RUNTIME_CONFIG_FILE):
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
    """
    import config as _config
    out = {}
    for key in ALLOWED.keys():
        out[key] = getattr(_config, key, None)
    return out
