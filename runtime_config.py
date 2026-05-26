#!/usr/bin/env python3
"""
运行时配置覆盖层 — 跨进程同步 config 变更 v3.0（多账户 + 每交易所独立账户）

工作方式：
  1. admin_panel 写 runtime_config.json（白名单字段，带时间戳）
  2. 所有进程在关键时刻调 apply_overrides()
  3. apply_overrides 读文件 → 按白名单写回 config 模块属性

v3 多账户 + 每交易所独立账户结构：
{
  "_global": { "PRIMARY_EXCHANGE": "binance", "SHADOW_PARALLEL": true, ... },
  "_exchanges": {
    "acc_abc123": {
      "binance": { "account_balance": 200, "leverage": 10, "live_mode": true, ... },
      "okx": { "account_balance": 100, "leverage": 5, "live_mode": false, ... },
      "gate": { ... }
    }
  },
  "acc_abc123": { "ACCOUNT_BALANCE": 100, "DEFAULT_STAKE": 50, ... },
  "acc_def456": { "ACCOUNT_BALANCE": 500, ... }
}

全局字段(GLOBAL_FIELDS): 只存一份，所有账户共享
  PRIMARY_EXCHANGE, PRIMARY_EXCHANGE_FALLBACK, SHADOW_PARALLEL

账户字段(ACCOUNT_FIELDS): 每个账户独立的风控/仓位参数（向后兼容）
  ACCOUNT_BALANCE, DEFAULT_STAKE, LEVERAGE, ...

交易所字段(_exchanges段): v3 新增，每个账户下每个交易所独立配置
  每个交易所拥有自己的 account_balance, leverage, default_stake, risk, compound, tp_sl
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
    'RISK_MAX_DAILY_TRADES': (int, _int_validator(1, 50), '单日最大开仓'),
    'RISK_MAX_DAILY_TRADES_LONG': (int, _int_validator(0, 50), '单日做多上限 (0=不限)'),
    'RISK_MAX_DAILY_TRADES_SHORT': (int, _int_validator(0, 50), '单日做空上限 (0=不限)'),
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
# v3 变更：LIVE_MODE / OKX_LIVE_MODE 移至每交易所独立设置
# 向后兼容：旧的 GLOBAL_FIELDS 保留这些字段用于读取 v2 格式配置
GLOBAL_FIELDS = {'LIVE_MODE', 'OKX_LIVE_MODE', 'PRIMARY_EXCHANGE',
                 'PRIMARY_EXCHANGE_FALLBACK', 'SHADOW_PARALLEL'}
ACCOUNT_FIELDS = set(ALLOWED.keys()) - GLOBAL_FIELDS

# v3: 每交易所独立允许的设置字段白名单
EXCHANGE_SETTING_FIELDS = {
    'account_balance': (int, _int_validator(10, 10000), '交易所账户本金 (U)'),
    'leverage': (int, _int_validator(1, 20), '杠杆倍数'),
    'default_stake': (int, _int_validator(5, 500), '单笔保证金 (U)'),
    'live_mode': (bool, None, '实盘开关'),
    'slippage_alert_pct': (float, _pct_validator(0.1, 5.0), '滑点告警阈值 (%)'),
}

# 支持的交易所列表
SUPPORTED_EXCHANGES = ('binance', 'okx', 'gate')


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
            explicit_balance = 'ACCOUNT_BALANCE' in overrides
            explicit_stake = 'DEFAULT_STAKE' in overrides
            used_scaled_stake = False

            if not explicit_balance or not explicit_stake:
                ps_state = get_position_scale_state()
                eff_bal = ps_state.get('effective_balance')
                scaled = ps_state.get('scaled_fields') or {}
                if not explicit_balance and eff_bal is not None and eff_bal > 0:
                    balance = float(eff_bal)
                if not explicit_stake and 'DEFAULT_STAKE' in scaled and scaled['DEFAULT_STAKE'] is not None:
                    stake = float(scaled['DEFAULT_STAKE'])
                    used_scaled_stake = True

            if not explicit_stake and not used_scaled_stake:
                # apply_position_scale 还没跑过：手动算。显式传 DEFAULT_STAKE 时不覆盖，
                # 否则 admin 保存明显非法值会被当前 scale state 掩盖。
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
        # 2026-05 修复（P1）：升级为 ERROR。
        # 风控 can_open_trade 检查 total_open_stake + stake > max_position 即拒绝；
        # 当 total_open_stake=0（首笔）时 stake 单独 > max_position 就会失败 → 永远开不出仓。
        # 这是 silent failure（用户感知"没新开仓"但不知道为什么），
        # 必须在 admin 保存时硬阻塞，让用户立刻意识到配置不一致。
        errors.append(
            f"❌ DEFAULT_STAKE({stake:.0f}U) > 最大持仓上限({max_position:.0f}U = "
            f"ACCOUNT_BALANCE {balance:.0f} × RISK_MAX_POSITION_PCT {pos_pct})，"
            f"风控会永远拒绝开仓（首笔即超限）。请降低 DEFAULT_STAKE 或提高 "
            f"ACCOUNT_BALANCE / RISK_MAX_POSITION_PCT。"
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
        # 2026-05 修正：get_compound_stake() 已引入动态 cap =
        # min(COMPOUND_MAX_STAKE, balance × position_pct)，
        # 实际 stake 永远不会超过 balance × pct，风控不会因此拒绝。
        # 仅当 COMPOUND_MAX_STAKE 远超本金且用户可能误以为能开大仓时给提示。
        # 降级为轻量 info 提示（不再是 WARNING），不展示在 dashboard 健康检查中。
        pass
    elif auto_compound and compound_max > max_position_for_check:
        # 同上：动态 cap 已保护，不再报 WARNING
        pass

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

    # v2 → v2.1 残留清理（2026-05 阶段 5）：旧版本里 POSITION_MODE 等被
    # 错误存到 _global 段（当时 POSITION_MODE 是全局字段），新版本里它是
    # 账号字段。如果发现 _global 里有 ACCOUNT_FIELDS 残留：
    #   1. 把残留 key 复制到所有"没有自己 override"的账号下；
    #   2. 从 _global 中删除。
    # 这样旧用户的"想让所有账号都 proportional"的意图被保留。
    global_data = data.get('_global', {})
    legacy_keys = [k for k in list(global_data.keys()) if k in ACCOUNT_FIELDS]
    if legacy_keys:
        logger.info(f"runtime_config: 检测到 _global 残留账号字段 {legacy_keys}，自动迁移到各账号下")
        # 收集所有账号 ID（admin_secrets + 已有 override 的）
        all_ids = set()
        try:
            import admin_secrets
            for acc in (admin_secrets.list_accounts() or []):
                aid = acc.get('id') if isinstance(acc, dict) else None
                if aid:
                    all_ids.add(aid)
        except Exception:
            pass
        for aid in data.keys():
            if aid and not aid.startswith('_'):
                all_ids.add(aid)

        # 给每个账号补全（不覆盖已有 override）
        for aid in all_ids:
            acc_seg = data.setdefault(aid, {})
            for k in legacy_keys:
                if k not in acc_seg:
                    acc_seg[k] = global_data[k]
        # 从 _global 移除残留
        for k in legacy_keys:
            del global_data[k]

        try:
            _save_raw_config(data)
        except Exception as e:
            logger.warning(f"runtime_config: v2.1 迁移写盘失败（非致命，下次再试）: {e}")

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

# ── 每账号独立 scale 状态缓存（2026-05 重构：阶段 1）─────────────────
# 结构: { account_id: {
#     'mode': 'manual' | 'proportional',
#     'effective_balance': float,
#     'scale': float,
#     'balance_source': 'config' | 'binance' | 'okx' | 'both' | 'unavailable',
#     'scaled_fields': { 'DEFAULT_STAKE': int, 'RISK_MAX_DAILY_LOSS': int, ... },
#     'last_applied_at': float,
# } }
#
# 由 compute_per_account_scaled() 写入；get_account_scale_state /
# get_all_account_scale_states / get_account_scaled_value 读取。
#
# 重要：_account_param 走这个 cache 后，不再被"活跃账号写入 config 模块"污染。
# 旧的 _position_scale_state 仍保留并同步活跃账号的快照（向后兼容
# get_position_scale_state、admin panel 旧字段、单账号路径）。
_per_account_state: Dict[str, dict] = {}


def get_position_scale_state() -> dict:
    """返回最近一次 apply_position_scale() 的执行状态（admin panel /api/state 用）"""
    return dict(_position_scale_state)


def get_account_scale_state(account_id: str) -> dict:
    """
    返回指定账号的 scale state 副本。
    若该账号尚未被 compute_per_account_scaled 算过，返回空 dict。
    """
    if not account_id:
        return {}
    return dict(_per_account_state.get(account_id, {}))


def get_all_account_scale_states() -> dict:
    """返回所有账号的 scale state 副本（admin panel /api/state 用）"""
    return {k: dict(v) for k, v in _per_account_state.items()}


def get_account_scaled_value(account_id: str, key: str):
    """
    返回该账号在 key 上"应用了自身 POSITION_MODE 后"的最终值。

    优先级：
      1. 该账号是 proportional 且 key ∈ _PROPORTIONAL_FIELDS
         → 返回 _per_account_state[acc][scaled_fields][key]
           （= PRISTINE × per-account scale；该账号自己的 ACCOUNT_BALANCE / 实盘余额算的）
      2. 该账号在 runtime_config.json 里有 override
         → 返回该 override 值（manual 模式下生效；proportional 模式下被 _PROPORTIONAL_FIELDS
           的特殊处理盖过 — 上面分支命中后已返回）
      3. config.py 的 PRISTINE 默认值

    返回 None 表示既无缓存也无 override 也无 PRISTINE（极少；调用方自己兜底）。

    永远不读 config 模块（避免被活跃账号污染），也永远不写 config 模块。
    """
    if not account_id or account_id == '_default':
        return None

    # 触发 cache 填充（first-call 或 cache miss 时）
    st = _per_account_state.get(account_id)
    if st is None:
        try:
            compute_per_account_scaled()
        except Exception as e:
            logger.debug(f"get_account_scaled_value: compute_per_account_scaled 失败 {e}")
            return None
        st = _per_account_state.get(account_id)
        if st is None:
            # 该账号在 admin_secrets / runtime_config.json 都不存在 → 无数据
            return None

    # 1. proportional + 缩放字段：直接读 cache（per-account scale 算好的值）
    if key in _PROPORTIONAL_FIELDS:
        v = st.get('scaled_fields', {}).get(key)
        if v is not None:
            return v

    # 2. 账号的显式 override
    try:
        overrides = load_account_overrides(account_id) or {}
        if key in overrides:
            return overrides[key]
    except Exception:
        pass

    # 3. PRISTINE 兜底
    return get_pristine_default(key)


def compute_per_account_scaled() -> dict:
    """
    遍历所有账号独立计算 mode + scale + scaled_fields，写入 _per_account_state。

    账号集合 = admin_secrets.list_accounts() ∪ runtime_config.json 中有 override 的账号 ID。

    多账号 + 实盘的边界（已知限制）：
      - 影子模式（LIVE_MODE/OKX_LIVE_MODE 都关）：每个账号用自己的
        ACCOUNT_BALANCE override 算 scale，账号间独立；
      - 实盘模式：所有账号共享 _fetch_live_balance_cached() 拉到的全局 balance。
        按账号 API key 独立拉余额是更深层重构，不在本期范围。

    返回 _per_account_state 的副本。
    """
    import time as _time
    import config as _config

    now = _time.time()
    try:
        baseline = float(getattr(_config, 'BASELINE_BALANCE', 100) or 100)
    except (TypeError, ValueError):
        baseline = 100.0
    if baseline <= 0:
        baseline = 100.0

    # 1. 收集账号 ID
    all_ids = set()
    try:
        from admin_secrets import list_accounts as _list_accounts
        for acc in (_list_accounts() or []):
            aid = acc.get('id') if isinstance(acc, dict) else None
            if aid:
                all_ids.add(aid)
    except Exception as e:
        logger.debug(f"compute_per_account_scaled: list_accounts 失败 {e}")
    try:
        all_overrides = load_all_account_overrides() or {}
        for aid in all_overrides.keys():
            if aid and not aid.startswith('_'):
                all_ids.add(aid)
    except Exception as e:
        logger.debug(f"compute_per_account_scaled: load_all_account_overrides 失败 {e}")
        all_overrides = {}

    # 2. 实盘 fetch 一次（所有账号共享；影子模式返回 None）
    live_balance, live_source = _fetch_live_balance_cached()

    state_map: Dict[str, dict] = {}
    pristine_balance = get_pristine_default('ACCOUNT_BALANCE')
    pristine_position_mode = get_pristine_default('POSITION_MODE') or 'manual'

    for acc_id in all_ids:
        overrides = all_overrides.get(acc_id, {}) if isinstance(all_overrides, dict) else {}

        # 决定 mode
        mode = overrides.get('POSITION_MODE')
        if mode not in ('manual', 'proportional'):
            mode = pristine_position_mode

        # 决定 effective_balance
        # 注意：系统影子账户必须始终使用自己的配置余额，不跟随实盘余额缩放。
        is_shadow_account = (acc_id == 'acc_shadow_system')
        if live_balance is not None and not is_shadow_account:
            # 实盘账号：共享交易所余额
            effective_balance = float(live_balance)
            balance_source = live_source or 'unavailable'
        else:
            # 影子账号 / 全影子模式：用账号自己的 ACCOUNT_BALANCE override
            bal = overrides.get('ACCOUNT_BALANCE')
            if bal is None:
                bal = pristine_balance
            try:
                effective_balance = float(bal) if bal is not None else baseline
            except (TypeError, ValueError):
                effective_balance = baseline
            balance_source = 'config' if not is_shadow_account else 'config-shadow'

        st = {
            'mode': mode,
            'effective_balance': round(float(effective_balance), 2)
                if effective_balance is not None else None,
            'scale': 1.0,
            'balance_source': balance_source,
            'scaled_fields': {},
            'last_applied_at': now,
        }

        if mode == 'proportional' and effective_balance and effective_balance > 0:
            scale = effective_balance / baseline
            st['scale'] = round(scale, 4)
            for key in _PROPORTIONAL_FIELDS:
                base_value = get_pristine_default(key)
                if base_value is None:
                    continue
                if isinstance(base_value, int):
                    scaled = max(1, int(round(base_value * scale)))
                else:
                    scaled = round(float(base_value) * scale, 2)
                st['scaled_fields'][key] = scaled
        else:
            # manual 模式 或 proportional + balance 无效:
            # scaled_fields = override 或 PRISTINE（让 get_account_scaled_value
            # 直接读 scaled_fields 也能命中正确的 manual 值）
            for key in _PROPORTIONAL_FIELDS:
                v = overrides.get(key)
                if v is None:
                    v = get_pristine_default(key)
                if v is not None:
                    st['scaled_fields'][key] = v

        state_map[acc_id] = st

    _per_account_state.clear()
    _per_account_state.update(state_map)
    return {k: dict(v) for k, v in state_map.items()}


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
        previous_mode = _position_scale_state.get('mode')
        if previous_mode != 'proportional':
            _position_scale_state.update(state)
            return dict(state)

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

        previous_scaled = _position_scale_state.get('scaled_fields') or {}
        for key in _PROPORTIONAL_FIELDS:
            if getattr(_config, key, None) != previous_scaled.get(key):
                continue
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




def _enforce_live_mode_prereqs() -> dict:
    """没有交易所凭证时，强制关闭实盘开关，避免 live_executor 空转刷错。"""
    import config as _config
    changes = {}
    try:
        from admin_secrets import get_active_account_id, get_exchange_credentials
        acc_id = get_active_account_id()
    except Exception:
        acc_id = ''

    def _has_binance() -> bool:
        try:
            creds = get_exchange_credentials('binance', account_id=acc_id or None)
            return bool(creds.get('api_key')) and bool(creds.get('secret'))
        except Exception:
            return False

    def _has_okx() -> bool:
        try:
            creds = get_exchange_credentials('okx', account_id=acc_id or None)
            return bool(creds.get('api_key')) and bool(creds.get('secret')) and bool(creds.get('passphrase'))
        except Exception:
            return False

    bn_ok = _has_binance()
    okx_ok = _has_okx()

    if getattr(_config, 'LIVE_MODE', False) and not bn_ok:
        changes['LIVE_MODE'] = {'old': True, 'new': False}
        _config.LIVE_MODE = False
    if getattr(_config, 'OKX_LIVE_MODE', False) and not okx_ok:
        changes['OKX_LIVE_MODE'] = {'old': True, 'new': False}
        _config.OKX_LIVE_MODE = False

    if changes:
        logger.warning(f"runtime_config: 缺少交易所凭证，已自动关闭实盘开关: {changes}")
    return changes

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

    # 没有交易所凭证时，强制关闭实盘开关，避免 live_executor 空转刷错。
    try:
        _enforce_live_mode_prereqs()
    except Exception as e:
        logger.warning(f"_enforce_live_mode_prereqs 异常（非致命）: {e}")

    # 不论 overrides 有没有变，都重跑比例缩放：
    # - manual 模式 no-op 几乎零成本
    # - proportional 模式靠 60s 缓存避免每次都打交易所
    try:
        apply_position_scale()
    except Exception as e:
        logger.warning(f"apply_position_scale 异常（非致命）: {e}")

    # 阶段 1：每账号独立 scale 计算（compute_per_account_scaled 写入
    # _per_account_state，供 _account_param 按账号查参数；不污染 config 模块）
    try:
        compute_per_account_scaled()
        # 同步：把活跃账号的 state 写到 _position_scale_state，
        # 让 get_position_scale_state() / 老的 admin panel 字段仍能拿到合理值
        try:
            from admin_secrets import get_active_account_id as _gaa
            _active = _gaa()
        except Exception:
            _active = ''
        # 只有 active 账号确实存在 runtime override 时，才用 per-account state 覆盖
        # 老的全局 position scale state。否则测试/单账户场景中 admin_secrets 的
        # 真实账号列表会把刚按 config 计算出的 state 覆盖成 pristine manual。
        try:
            _all_overrides = load_all_account_overrides() or {}
        except Exception:
            _all_overrides = {}
        if _active and _active in _per_account_state and _active in _all_overrides:
            _position_scale_state.update(_per_account_state[_active])
    except Exception as e:
        logger.warning(f"compute_per_account_scaled 异常（非致命）: {e}")

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



# ══════════════════════════════════════════════════════════════════
#  v3: 每交易所独立账户配置覆盖层
# ══════════════════════════════════════════════════════════════════
# 设计理念：每个交易所（binance/okx/gate）拥有自己独立的资金池、杠杆、
# 仓位、风控参数。不同交易所之间互不影响。
#
# runtime_config.json 中 "_exchanges" 段结构：
# {
#   "_exchanges": {
#     "acc_abc123": {
#       "binance": { "account_balance": 200, "leverage": 10, ... },
#       "okx": { "account_balance": 100, "leverage": 5, ... }
#     }
#   }
# }
#
# 优先级（高→低）：
#   1. runtime_config.json _exchanges 段（admin panel 实时修改）
#   2. admin_secrets.json 中的 settings 字段
#   3. config/_defaults.py EXCHANGE_ACCOUNTS 字典
#   4. _default_exchange_settings() 兜底默认值

def _default_exchange_settings() -> dict:
    """每交易所账户的默认设置"""
    return {
        'account_balance': 100,
        'leverage': 10,
        'default_stake': 30,
        'live_mode': False,
        'slippage_alert_pct': 1.0,
        'risk': {
            'max_daily_loss': 30,
            'max_daily_trades': 3,
            'consecutive_loss_pause': 3,
            'max_position_pct': 0.5,
            'cooldown_hours': 24,
        },
        'compound': {
            'enabled': True,
            'step': 50,
            'increase': 25,
            'max_stake': 300,
        },
        'tp_sl': {
            'tp1_multiplier': 0.95,
            'tp2_multiplier': 0.92,
            'tp1_close_ratio': 0.5,
            'hard_stop_loss_pct': 5.0,
        },
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并字典，override 覆盖 base"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_exchange_overrides(account_id: str, exchange: str) -> dict:
    """
    读取指定账户+交易所的配置覆盖。

    Args:
        account_id: 账户 ID
        exchange: 交易所名称 ('binance', 'okx', 'gate')

    Returns:
        该交易所的覆盖值字典（可能为空）
    """
    data = _load_raw_config()
    exchanges_data = data.get('_exchanges', {})
    acc_exchanges = exchanges_data.get(account_id, {})
    return acc_exchanges.get(exchange.lower(), {})


def save_exchange_overrides(account_id: str, exchange: str, overrides: dict) -> None:
    """
    保存指定账户+交易所的配置覆盖。

    使用深度合并，只更新传入的字段。

    Args:
        account_id: 账户 ID
        exchange: 交易所名称 ('binance', 'okx', 'gate')
        overrides: 要保存的覆盖值字典
    """
    exchange = exchange.lower()
    if exchange not in SUPPORTED_EXCHANGES:
        raise ValueError(f"不支持的交易所: {exchange}")

    with _locked_config() as (data, save):
        exchanges_data = data.setdefault('_exchanges', {})
        acc_exchanges = exchanges_data.setdefault(account_id, {})
        current = acc_exchanges.get(exchange, {})
        merged = _deep_merge(current, overrides)
        acc_exchanges[exchange] = merged
        save(data)


def load_all_exchange_overrides(account_id: str) -> dict:
    """
    读取指定账户下所有交易所的配置覆盖。

    Returns:
        { 'binance': {...}, 'okx': {...}, 'gate': {...} }
    """
    data = _load_raw_config()
    exchanges_data = data.get('_exchanges', {})
    return exchanges_data.get(account_id, {})


def get_effective_exchange_config(exchange: str, account_id: str = None) -> dict:
    """
    获取指定交易所的最终生效配置。

    合并优先级（高→低）：
      1. runtime_config.json _exchanges 段覆盖
      2. admin_secrets.json 中的 per-exchange settings
      3. config/_defaults.py EXCHANGE_ACCOUNTS 字典
      4. 默认值

    Args:
        exchange: 交易所名称 ('binance', 'okx', 'gate')
        account_id: 账户 ID，None 使用活跃账户

    Returns:
        完整的交易所配置字典
    """
    exchange = exchange.lower()
    if not account_id:
        try:
            import admin_secrets
            account_id = admin_secrets.get_active_account_id()
        except Exception:
            account_id = ''

    # 层 4: 默认值
    result = _default_exchange_settings()

    # 层 3: config/_defaults.py EXCHANGE_ACCOUNTS
    try:
        import config
        exchange_accounts = getattr(config, 'EXCHANGE_ACCOUNTS', {})
        legacy_cfg = exchange_accounts.get(exchange, {})
        if legacy_cfg:
            result = _deep_merge(result, legacy_cfg)
    except Exception:
        pass

    # 层 2: admin_secrets.json settings
    if account_id:
        try:
            import admin_secrets
            secrets_settings = admin_secrets.get_exchange_settings(exchange, account_id)
            if secrets_settings:
                result = _deep_merge(result, secrets_settings)
        except Exception:
            pass

    # 层 1: runtime_config.json _exchanges 覆盖
    if account_id:
        runtime_overrides = load_exchange_overrides(account_id, exchange)
        if runtime_overrides:
            result = _deep_merge(result, runtime_overrides)

    return result


def get_effective_exchange_param(exchange: str, key: str,
                                 account_id: str = None, default=None):
    """
    获取指定交易所的单个生效参数。支持点号分隔的嵌套路径。

    Args:
        exchange: 交易所名称
        key: 参数路径 ('leverage', 'risk.max_daily_loss', 'compound.step')
        account_id: 账户 ID
        default: 未找到时的默认值

    Returns:
        参数值

    用法:
        get_effective_exchange_param('okx', 'leverage')           → 5
        get_effective_exchange_param('binance', 'risk.max_daily_loss')  → 30
    """
    cfg = get_effective_exchange_config(exchange, account_id)
    keys = key.split('.')
    current = cfg
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k)
        else:
            return default
        if current is None:
            return default
    return current


def is_exchange_live(exchange: str, account_id: str = None) -> bool:
    """
    检查指定交易所是否开启了实盘模式。

    Args:
        exchange: 交易所名称
        account_id: 账户 ID，None 使用活跃账户

    Returns:
        True/False
    """
    return bool(get_effective_exchange_param(exchange, 'live_mode', account_id, False))


def get_all_live_exchanges(account_id: str = None) -> list:
    """
    返回所有开启了实盘模式的交易所名称列表。

    Returns:
        ['binance', 'okx'] — live_mode=True 的交易所
    """
    result = []
    for exch in SUPPORTED_EXCHANGES:
        if is_exchange_live(exch, account_id):
            result.append(exch)
    return result


def validate_exchange_overrides(exchange: str, overrides: dict,
                                 account_id: str = None) -> tuple:
    """
    校验每交易所覆盖值的一致性。

    Args:
        exchange: 交易所名称
        overrides: 即将保存的覆盖值
        account_id: 账户 ID

    Returns:
        (errors: list[str], warnings: list[str])
    """
    errors = []
    warnings_out = []

    # 获取合并后的生效配置
    current = get_effective_exchange_config(exchange, account_id)
    # 应用 overrides 得到"保存后"的值
    merged = _deep_merge(current, overrides)

    balance = float(merged.get('account_balance', 100))
    stake = float(merged.get('default_stake', 30))
    risk = merged.get('risk', {})
    pos_pct = float(risk.get('max_position_pct', 0.5))

    max_position = balance * pos_pct

    if stake > balance:
        errors.append(
            f"❌ [{exchange}] default_stake({stake:.0f}U) > account_balance({balance:.0f}U)，"
            f"保证金超过本金，风控将永远拒绝开仓。"
        )
    elif stake > max_position:
        errors.append(
            f"❌ [{exchange}] default_stake({stake:.0f}U) > 最大持仓上限({max_position:.0f}U = "
            f"account_balance {balance:.0f} × max_position_pct {pos_pct})，"
            f"风控会永远拒绝开仓。"
        )

    for e in errors:
        logger.error(f"交易所配置一致性 ERROR: {e}")
    for w in warnings_out:
        logger.warning(f"交易所配置一致性 WARNING: {w}")

    return (errors, warnings_out)


def get_current_exchange_values() -> dict:
    """
    获取所有交易所当前的生效配置（供 admin panel 使用）。

    Returns:
        {
            'binance': { 'account_balance': 200, 'leverage': 10, ... },
            'okx': { 'account_balance': 100, 'leverage': 5, ... },
            'gate': { ... }
        }
    """
    result = {}
    for exch in SUPPORTED_EXCHANGES:
        result[exch] = get_effective_exchange_config(exch)
    return result
