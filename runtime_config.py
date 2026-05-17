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
from contextlib import contextmanager
from typing import Any, Callable, Dict, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_CONFIG_FILE = os.path.join(SCRIPT_DIR, 'runtime_config.json')
_RUNTIME_LOCK = RUNTIME_CONFIG_FILE + '.lock'

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

    # ── 复利策略（每账号独立曲线）──
    'AUTO_COMPOUND_ENABLED': (bool, None, '自动复利总开关'),
    'COMPOUND_STEP': (int, _int_validator(10, 500), '每累计盈利 N U 步进 (U)'),
    'COMPOUND_INCREASE': (int, _int_validator(1, 200), '每步增加的保证金 (U)'),
    'COMPOUND_MAX_STAKE': (int, _int_validator(10, 1000), '复利后单笔保证金上限 (U)'),

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

    # ── 仓位模式（v5.1）──
    # manual: 用户手填 DEFAULT_STAKE 等绝对值（向后兼容）
    # proportional: 以 100U 为基准按余额比例自动缩放金额参数
    'POSITION_MODE': (str,
                      _enum_validator(['manual', 'proportional']),
                      '仓位模式 (manual=手动 / proportional=按余额比例缩放)'),
}

# ── 全局字段 vs 账户字段 ──
GLOBAL_FIELDS = {'LIVE_MODE', 'OKX_LIVE_MODE', 'PRIMARY_EXCHANGE',
                 'PRIMARY_EXCHANGE_FALLBACK', 'SHADOW_PARALLEL', 'POSITION_MODE'}
ACCOUNT_FIELDS = set(ALLOWED.keys()) - GLOBAL_FIELDS


# ══════════════════════════════════════════════════════════════════
#  config.py 原始默认值快照
# ══════════════════════════════════════════════════════════════════
# 关键修复：apply_overrides() 会把当前活跃账号的覆盖值写到 config 模块属性，
# 这之后再读 config.X 就拿不到 config.py 的原始默认值了。
#
# 但是 ACCOUNT_FIELDS 的 fallback 必须用原始默认值——否则"没设过覆盖的 B 账号"
# 会 fallback 到 config 模块（已经被 A 账号污染），UI/后端都会以为
# "A 改了之后 B 也跟着变"，正是用户报告的 bug。
#
# 解决方案：runtime_config 模块导入时立刻快照 config 模块所有 ALLOWED 键的值。
# 因为 config.py 只有常量定义、没有副作用，所以模块导入完成时拿到的就是源文件
# 写死的值。之后无论 apply_overrides 多少次，PRISTINE_DEFAULTS 都不会被覆盖。

_PRISTINE_DEFAULTS: Dict[str, Any] = {}


def _snapshot_pristine_defaults() -> None:
    """
    把 config 模块当前所有 ALLOWED 键的值复制到 _PRISTINE_DEFAULTS。
    必须在 apply_overrides 第一次运行之前调用。
    幂等：只在 _PRISTINE_DEFAULTS 为空时填充。
    """
    if _PRISTINE_DEFAULTS:
        return
    try:
        import config as _config
        for key in ALLOWED.keys():
            _PRISTINE_DEFAULTS[key] = getattr(_config, key, None)
    except Exception as e:
        logger.error(f"_snapshot_pristine_defaults 失败: {e}")


def get_pristine_default(key: str):
    """
    返回某个白名单字段的 config.py 原始默认值。
    用于 ACCOUNT_FIELDS 在该账号没有覆盖时的 fallback，避免被 apply_overrides 污染。
    """
    if not _PRISTINE_DEFAULTS:
        _snapshot_pristine_defaults()
    return _PRISTINE_DEFAULTS.get(key)


def get_all_pristine_defaults() -> dict:
    """返回所有白名单字段的原始默认值字典（admin panel /api/state 下发到前端做 fallback）。"""
    if not _PRISTINE_DEFAULTS:
        _snapshot_pristine_defaults()
    return dict(_PRISTINE_DEFAULTS)


# 模块导入时立刻快照——在任何 apply_overrides 之前
_snapshot_pristine_defaults()


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

    # P2-2 修复（2026-05）：proportional 模式下，DEFAULT_STAKE 实际生效值不是
    # admin override 的值，而是 PRISTINE × scale。用 override 校验会得到误报警
    # （admin 设的 48 永远 < 100 baseline 不报警，但实际 86 vs 286 也不报警，
    # 看起来一致；但当 effective_balance 极小或缩放因子很大时，会出现"看起来
    # 配置 OK 实际锁死"的 silent failure）。改用 PRISTINE × scale 计算。
    # ACCOUNT_BALANCE 的比对也改用 effective_balance（实盘真实余额或影子手填值），
    # 否则 baseline 100 的虚拟值跟实际 stake 86 比意义不大。
    try:
        position_mode = overrides.get('POSITION_MODE',
                                      getattr(_config, 'POSITION_MODE', 'manual'))
    except Exception:
        position_mode = 'manual'

    if position_mode == 'proportional':
        try:
            ps_state = get_position_scale_state()
            eff_bal = ps_state.get('effective_balance')
            scaled = ps_state.get('scaled_fields') or {}
            if eff_bal is not None and eff_bal > 0:
                balance = float(eff_bal)
            if 'DEFAULT_STAKE' in scaled and scaled['DEFAULT_STAKE'] is not None:
                stake = float(scaled['DEFAULT_STAKE'])
            else:
                # apply_position_scale 还没跑过：手动算
                pristine_stake = get_pristine_default('DEFAULT_STAKE') or stake
                pristine_baseline = float(getattr(_config, 'BASELINE_BALANCE', 100) or 100)
                if pristine_baseline > 0 and balance > 0:
                    stake = float(pristine_stake) * (balance / pristine_baseline)
        except Exception:
            pass  # fallback 到 override 值，行为同 manual

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

    # 新增：复利上限 vs 持仓上限的一致性检查
    # COMPOUND_MAX_STAKE 是复利后单笔保证金的硬上限。如果它已经 > 最大持仓上限，
    # 复利触顶后系统会进入"按规则不能开仓但 stake 仍按上限计算"的死循环。
    # 这是配置层面的 silent kill：用户设了一个永远到不了的复利目标。
    auto_compound = overrides.get('AUTO_COMPOUND_ENABLED',
                                  getattr(_config, 'AUTO_COMPOUND_ENABLED', True))
    compound_max = overrides.get('COMPOUND_MAX_STAKE',
                                 getattr(_config, 'COMPOUND_MAX_STAKE', 300))
    if account_id:
        try:
            acc_overrides = load_account_overrides(account_id)
            auto_compound = overrides.get('AUTO_COMPOUND_ENABLED',
                                          acc_overrides.get('AUTO_COMPOUND_ENABLED', auto_compound))
            compound_max = overrides.get('COMPOUND_MAX_STAKE',
                                         acc_overrides.get('COMPOUND_MAX_STAKE', compound_max))
        except Exception:
            pass

    try:
        compound_max = float(compound_max)
    except (TypeError, ValueError):
        compound_max = 0

    # P2-3 修复（2026-05）：proportional 模式下 ACCOUNT_BALANCE 是个"basement
    # baseline"（默认 100U），不是真实账户能用的余额。COMPOUND_MAX_STAKE 是
    # 用户要求的"绝对值上限不缩放"，跟 baseline 100 比永远会触发警告 → 启动日志
    # spam。改用 effective_balance（实盘真实余额或影子手填值）做比较，让警告
    # 真实反映"复利上限是否高于实际可用资金"。
    try:
        position_mode = overrides.get('POSITION_MODE',
                                      getattr(_config, 'POSITION_MODE', 'manual'))
    except Exception:
        position_mode = 'manual'

    effective_balance_for_check = balance
    if position_mode == 'proportional':
        try:
            from runtime_config import get_position_scale_state as _gps
            ps_state = _gps()
            eb = ps_state.get('effective_balance')
            if eb is not None and eb > 0:
                effective_balance_for_check = float(eb)
        except Exception:
            # 拿不到就 fallback 到 baseline，行为同 manual
            pass

    max_position_for_check = effective_balance_for_check * pos_pct

    if auto_compound and compound_max > effective_balance_for_check:
        # 与 DEFAULT_STAKE > balance 不同：COMPOUND_MAX_STAKE 是"复利触顶后"
        # 的硬上限，亏损时 get_compound_stake 会回退到 DEFAULT_STAKE，所以
        # 不会立刻锁死开仓，只是当账户累计盈利触顶时进入"理论上能拿大仓位
        # 但风控拒收"的状态。属于 WARNING 而非 ERROR。
        mode_tag = (
            f"（proportional, effective_balance={effective_balance_for_check:.0f}U）"
            if position_mode == 'proportional' else ''
        )
        warnings_out.append(
            f"⚠️ COMPOUND_MAX_STAKE({compound_max:.0f}U) > "
            f"实际可用本金({effective_balance_for_check:.0f}U){mode_tag}，"
            f"复利触顶后单笔保证金会超过本金，届时风控会永远拒绝开仓。"
            f"建议把 COMPOUND_MAX_STAKE 控制在 ≤ {effective_balance_for_check:.0f}U。"
        )
    elif auto_compound and compound_max > max_position_for_check:
        warnings_out.append(
            f"⚠️ COMPOUND_MAX_STAKE({compound_max:.0f}U) > "
            f"最大持仓上限({max_position_for_check:.0f}U)，"
            f"复利触顶后只要有任何其他持仓，新开仓将被拒绝。建议把 "
            f"COMPOUND_MAX_STAKE 控制在 ≤ {max_position_for_check:.0f}U "
            f"或提高 RISK_MAX_POSITION_PCT。"
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
    # bind-mount 兼容：rename 失败 (EBUSY/EXDEV) 时 fallback 到原地覆盖
    from common import _replace_or_inplace_overwrite as _replace
    _replace(tmp, RUNTIME_CONFIG_FILE)


@contextmanager
def _locked_config():
    """
    NF2-1 修复：read-modify-write 上下文管理器，对 runtime_config.json 加排他锁。

    用法:
        with _locked_config() as (data, save):
            data['_global']['LIVE_MODE'] = True
            save(data)

    背景:
        admin panel 的 save_overrides / save_account_overrides / save_global_overrides
        都是 RMW 操作 (load_raw → mutate → save_raw)。原实现没有锁，多 worker 并发
        修改时会丢失更新（admin_secrets 已用 _locked_secrets 解决了同类问题，对齐设计）。

    实现:
        使用 common.fcntl (Linux=fcntl 真模块；Windows=common._LockShim 含 NF-1 lseek(0)
        修复)，与 LockedJsonFile / admin_secrets._locked_secrets 行为一致。
    """
    # 延迟 import 避免循环依赖（common 不依赖 runtime_config，但保险起见仍延迟）
    from common import fcntl as _fcntl
    lock_fd = open(_RUNTIME_LOCK, 'a')
    try:
        _fcntl.flock(lock_fd, _fcntl.LOCK_EX)
        data = _load_raw_config()

        def save(new_data: dict) -> None:
            _save_raw_config(new_data)

        yield data, save
    finally:
        try:
            _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
        finally:
            lock_fd.close()


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

    NF2-1: 整段 RMW 在 _locked_config() 排他锁内，避免多 worker 并发写丢字段。
    """
    try:
        import admin_secrets
        active_id = admin_secrets.get_active_account_id()
    except Exception:
        active_id = ''

    # 防御性拒绝：致命错误直接抛（不需要持锁，纯校验）
    errors, _warnings = validate_cross_field_consistency(overrides, account_id=active_id)
    if errors:
        raise ValueError(
            "配置一致性校验失败，拒绝保存：\n" + "\n".join(errors)
        )

    # 拆分并保存（持锁 RMW）
    with _locked_config() as (data, save):
        for key, value in overrides.items():
            if key not in ALLOWED:
                continue
            if key in GLOBAL_FIELDS:
                data.setdefault('_global', {})[key] = value
            elif active_id:
                data.setdefault(active_id, {})[key] = value
        save(data)


def load_account_overrides(account_id: str) -> dict:
    """读取指定账户的配置覆盖"""
    data = _load_raw_config()
    return data.get(account_id, {})


def load_all_account_overrides() -> dict:
    """
    读取所有账户的配置覆盖，结构：
        { 'acc_abc': { 'ACCOUNT_BALANCE': 100, 'DEFAULT_STAKE': 50, ... },
          'acc_def': { 'ACCOUNT_BALANCE': 500, ... } }

    注意：不会包含 _global 段。供 admin panel /api/state 一次性下发到前端，
    前端切账号时本地 reload 表单不需要再发请求。
    """
    data = _load_raw_config()
    out = {}
    for key, value in data.items():
        if key.startswith('_'):
            continue
        if isinstance(value, dict):
            out[key] = value
    return out


def save_account_overrides(account_id: str, overrides: dict) -> None:
    """
    保存指定账户的配置覆盖。

    NF2-1: 整段 RMW 在 _locked_config() 排他锁内。
    NF2-2: 改为字段级合并 (update) 而不是整体替换，
           否则调用方传 {'LEVERAGE': 5} 会清空该账户的 ACCOUNT_BALANCE 等其他字段。
    """
    filtered = {}
    for key, value in overrides.items():
        if key in ACCOUNT_FIELDS:
            ok, _ = validate_change(key, value)
            if ok:
                filtered[key] = value

    with _locked_config() as (data, save):
        # NF2-2: 合并而非替换 — 保留账户中既有的其他字段
        data.setdefault(account_id, {}).update(filtered)
        save(data)


def load_global_overrides() -> dict:
    """读取全局配置"""
    data = _load_raw_config()
    return data.get('_global', {})


def save_global_overrides(overrides: dict) -> None:
    """
    保存全局配置。

    NF2-1: 整段 RMW 在 _locked_config() 排他锁内。
    """
    filtered = {}
    for key, value in overrides.items():
        if key in GLOBAL_FIELDS:
            ok, _ = validate_change(key, value)
            if ok:
                filtered[key] = value

    with _locked_config() as (data, save):
        global_data = data.setdefault('_global', {})
        global_data.update(filtered)
        save(data)


# ══════════════════════════════════════════════════════════════════
#  把 overrides 写回 config 模块
# ══════════════════════════════════════════════════════════════════

_last_applied_mtime = 0.0
_last_applied: dict = {}

# ── 比例模式状态（用于 admin panel /api/state 上报）─────────────────
# apply_position_scale() 每次执行后会更新此字典，admin 面板读它做展示。
_position_scale_state: dict = {
    'mode': 'manual',           # manual | proportional
    'effective_balance': None,  # 实际生效的余额（U）
    'scale': 1.0,               # 缩放系数 = effective_balance / BASELINE_BALANCE
    'balance_source': 'config', # config | binance | okx | both
    'scaled_fields': {},        # 缩放后的 4 个字段值
    'last_applied_at': 0.0,     # time.time()
}

# ── 真实余额拉取的 60s TTL 缓存 ─────────────────────────────────────
# 避免 apply_overrides() 30s 一调就打一次交易所 fetch_balance；
# 每 60s 才真正查一次（实盘场景下账户余额变化不会那么快）。
_balance_cache: dict = {
    'value': None,
    'source': None,
    'expires_at': 0.0,
}

# 比例模式下被自动缩放的金额参数（COMPOUND_MAX_STAKE 不在内）
_PROPORTIONAL_FIELDS = (
    'DEFAULT_STAKE',
    'RISK_MAX_DAILY_LOSS',
    'COMPOUND_STEP',
    'COMPOUND_INCREASE',
)


def get_position_scale_state() -> dict:
    """返回最近一次 apply_position_scale() 的执行状态（admin panel /api/state 用）"""
    return dict(_position_scale_state)


def _fetch_live_balance_cached() -> tuple:
    """
    从交易所拉真实余额，带 60s TTL 缓存。

    返回 (balance: float, source: str)
    - source ∈ {'binance', 'okx', 'both', 'config', 'unavailable'}
    - balance 为 None 时调用方应 fallback 到 config.ACCOUNT_BALANCE

    根据 LIVE_MODE / OKX_LIVE_MODE / PRIMARY_EXCHANGE 决定从哪所拉：
      - 双所且 PRIMARY_EXCHANGE='both': 两所余额相加
      - 单所实盘:                       拉那一所
      - 全是影子:                       返回 None（调用方用 ACCOUNT_BALANCE）
    """
    import time as _time
    import config as _config

    now = _time.time()
    if _balance_cache['value'] is not None and now < _balance_cache['expires_at']:
        return _balance_cache['value'], _balance_cache['source']

    bn_live = bool(getattr(_config, 'LIVE_MODE', False))
    okx_live = bool(getattr(_config, 'OKX_LIVE_MODE', False))
    primary = getattr(_config, 'PRIMARY_EXCHANGE', 'binance')

    if not bn_live and not okx_live:
        # 全影子：用 ACCOUNT_BALANCE 手填值
        return None, 'config'

    # 延迟 import 避免循环依赖（live_executor 依赖 config）
    try:
        from live_executor import check_live_balance, check_okx_balance
    except Exception as e:
        logger.warning(f"_fetch_live_balance_cached: import live_executor 失败: {e}")
        return None, 'unavailable'

    bn_total = 0.0
    okx_total = 0.0
    sources = []

    if bn_live:
        try:
            bn = check_live_balance()
            bn_total = float(bn.get('total', 0) or 0)
            if bn_total > 0:
                sources.append('binance')
        except Exception as e:
            logger.warning(f"check_live_balance 失败: {e}")

    if okx_live:
        try:
            okx = check_okx_balance()
            okx_total = float(okx.get('total', 0) or 0)
            if okx_total > 0:
                sources.append('okx')
        except Exception as e:
            logger.warning(f"check_okx_balance 失败: {e}")

    # 决定用哪个余额作为"实际可用本金"
    if primary == 'both' and bn_live and okx_live:
        balance = bn_total + okx_total
        source = 'both'
    elif primary == 'okx' and okx_live:
        balance = okx_total
        source = 'okx'
    elif primary == 'binance' and bn_live:
        balance = bn_total
        source = 'binance'
    elif primary == 'auto':
        # auto 模式：取较大者作为基准（避免余额低的所拖低 scale）
        if bn_total >= okx_total and bn_live:
            balance, source = bn_total, 'binance'
        elif okx_live:
            balance, source = okx_total, 'okx'
        else:
            balance, source = bn_total, 'binance'
    else:
        # 兜底：哪个开了用哪个
        if bn_live and bn_total > 0:
            balance, source = bn_total, 'binance'
        elif okx_live and okx_total > 0:
            balance, source = okx_total, 'okx'
        else:
            return None, 'unavailable'

    if balance <= 0:
        return None, 'unavailable'

    _balance_cache['value'] = balance
    _balance_cache['source'] = source
    _balance_cache['expires_at'] = now + 60  # 60s TTL
    return balance, source


def apply_position_scale() -> dict:
    """
    根据 POSITION_MODE 把金额参数按 scale 缩放写回 config 模块。

    - manual 模式: 不动任何字段，记录状态后返回。
    - proportional 模式:
      1) 决定 effective_balance:
         - LIVE_MODE/OKX_LIVE_MODE 至少一个开 → 拉真实余额（带 60s 缓存）
         - 全是影子 → 用 config.ACCOUNT_BALANCE（admin 面板可改）
      2) scale = effective_balance / BASELINE_BALANCE
      3) 用 PRISTINE 默认值 × scale 写回 4 个字段（不基于"当前值"否则会复合放大）
      4) COMPOUND_MAX_STAKE 不动（用户要求绝对值封顶 300U）

    返回最新的 _position_scale_state 字典副本。
    """
    import time as _time
    import config as _config

    mode = getattr(_config, 'POSITION_MODE', 'manual')
    baseline = float(getattr(_config, 'BASELINE_BALANCE', 100) or 100)

    state = {
        'mode': mode,
        'effective_balance': None,
        'scale': 1.0,
        'balance_source': 'config',
        'scaled_fields': {},
        'last_applied_at': _time.time(),
    }

    if mode != 'proportional':
        # manual: 必须把 4 个字段恢复成"admin override 或 PRISTINE 默认"，
        # 否则从 proportional 切回 manual 时 config 模块残留缩放后的脏值
        # （proportional 时 apply_position_scale 写入的 86/143/72 等不会被
        # apply_overrides 自动还原，因为 admin override 里没这些字段时
        # apply_overrides 不会主动重置）。
        # 此举确保 manual 模式下 4 字段始终反映"用户填的值或 PRISTINE 默认"，
        # 跟 admin 面板的 fallback 语义一致。
        try:
            from admin_secrets import get_active_account_id
            active_id = get_active_account_id()
        except Exception:
            active_id = ''

        try:
            acc_overrides = load_account_overrides(active_id) if active_id else {}
        except Exception:
            acc_overrides = {}

        for key in _PROPORTIONAL_FIELDS:
            override_val = acc_overrides.get(key)
            if override_val is not None:
                setattr(_config, key, override_val)
            else:
                pristine = get_pristine_default(key)
                if pristine is not None:
                    setattr(_config, key, pristine)

        _position_scale_state.update(state)
        return dict(state)

    # ── proportional 模式 ──
    # 决定 effective_balance
    live_balance, source = _fetch_live_balance_cached()
    if live_balance is not None:
        effective_balance = live_balance
        balance_source = source
    else:
        # 影子或 fetch 失败 → 用 ACCOUNT_BALANCE 手填值
        try:
            effective_balance = float(getattr(_config, 'ACCOUNT_BALANCE', baseline))
        except (TypeError, ValueError):
            effective_balance = baseline
        balance_source = source if source else 'config'

    if effective_balance <= 0 or baseline <= 0:
        # 边界保护：余额 0 不缩放，记 warning
        logger.warning(
            f"apply_position_scale: effective_balance={effective_balance} "
            f"baseline={baseline}，跳过缩放保留 PRISTINE 默认值"
        )
        # 把 PRISTINE 写回，避免之前的 scale 残留
        for k in _PROPORTIONAL_FIELDS:
            v = get_pristine_default(k)
            if v is not None:
                setattr(_config, k, v)
        state['effective_balance'] = effective_balance
        state['balance_source'] = balance_source
        state['scale'] = 1.0
        state['scaled_fields'] = {k: getattr(_config, k, None) for k in _PROPORTIONAL_FIELDS}
        _position_scale_state.update(state)
        return dict(state)

    scale = effective_balance / baseline

    scaled_fields = {}
    for key in _PROPORTIONAL_FIELDS:
        base_value = get_pristine_default(key)
        if base_value is None:
            continue
        # 整型字段保持整型，浮点保持浮点
        if isinstance(base_value, int):
            scaled = max(1, int(round(base_value * scale)))
        else:
            scaled = round(float(base_value) * scale, 2)
        setattr(_config, key, scaled)
        scaled_fields[key] = scaled

    state.update({
        'effective_balance': round(effective_balance, 2),
        'scale': round(scale, 4),
        'balance_source': balance_source,
        'scaled_fields': scaled_fields,
    })
    _position_scale_state.update(state)

    logger.info(
        f"POSITION_MODE=proportional, "
        f"balance={effective_balance:.2f}U({balance_source}), "
        f"scale={scale:.2f}, "
        f"DEFAULT_STAKE={scaled_fields.get('DEFAULT_STAKE')}, "
        f"RISK_MAX_DAILY_LOSS={scaled_fields.get('RISK_MAX_DAILY_LOSS')}, "
        f"COMPOUND_STEP={scaled_fields.get('COMPOUND_STEP')}, "
        f"COMPOUND_INCREASE={scaled_fields.get('COMPOUND_INCREASE')} "
        f"(COMPOUND_MAX_STAKE 不缩放)"
    )

    return dict(state)


def apply_overrides(force: bool = False) -> dict:
    """
    读 runtime_config.json 并把白名单字段写到 config 模块属性。

    使用活跃账户的配置合并全局配置。

    末尾自动调用 apply_position_scale()：
    - manual 模式: 走 no-op 路径（仅刷新状态）
    - proportional 模式: 把 PRISTINE × scale 写回 4 个金额字段
    即使 runtime_config.json 没变（mtime 没刷新），也会重跑 apply_position_scale
    以反映交易所余额变化（live balance 60s 缓存）。
    """
    global _last_applied_mtime, _last_applied

    applied: dict = {}

    if os.path.exists(RUNTIME_CONFIG_FILE):
        try:
            mtime = os.path.getmtime(RUNTIME_CONFIG_FILE)
        except OSError:
            mtime = 0.0

        if force or mtime != _last_applied_mtime:
            overrides = load_overrides()
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

    # 不论 overrides 有没有变，都重跑比例缩放：
    # - manual 模式 no-op 几乎零成本
    # - proportional 模式靠 60s 缓存避免每次都打交易所
    try:
        apply_position_scale()
    except Exception as e:
        logger.warning(f"apply_position_scale 异常（非致命）: {e}")

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
