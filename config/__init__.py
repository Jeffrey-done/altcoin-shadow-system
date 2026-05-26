"""
YAML 分层配置管理
从 config/ 目录加载 YAML 文件，提供类型安全的配置访问。

配置优先级（高→低）:
  1. 环境变量（DATABASE_URL 等）
  2. runtime_config.json（admin panel 动态修改）
  3. config/*.yaml（项目级默认值）
  4. 代码内置默认值（config/_defaults.py）

集成说明:
  - 所有旧代码 `import config` 仍然正常工作（本包 re-export 全部常量）
  - 策略模块通过 get_strategy_config('short_overbought') 获取参数
  - 风控模块通过 get_risk_config(account_id) 获取参数
  - 系统配置通过 get_system_config() 获取
  - apply_yaml_to_config() 可将 YAML 值覆盖到本模块的全局变量
"""

import os
import sys
import logging
from typing import Any, Dict, Optional

import yaml

# ══════════════════════════════════════════════════════════════════
#  关键：从 config/_defaults.py 导入全部常量，保证向后兼容
#  所有旧代码 `import config; config.LEVERAGE` 仍然正常工作
# ══════════════════════════════════════════════════════════════════
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_legacy_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_defaults.py')

if os.path.exists(_legacy_path):
    import importlib.util
    _spec = importlib.util.spec_from_file_location('config._defaults', _legacy_path)
    _legacy_module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_legacy_module)
    # 将所有公开属性注入本模块
    for _name in dir(_legacy_module):
        if not _name.startswith('_'):
            globals()[_name] = getattr(_legacy_module, _name)

logger = logging.getLogger("config.yaml_loader")

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_cache: Dict[str, Any] = {}


def _load_yaml(filename: str) -> dict:
    """加载单个 YAML 配置文件"""
    filepath = os.path.join(_CONFIG_DIR, filename)
    if not os.path.exists(filepath):
        return {}
    with open(filepath, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def load_all(force_reload: bool = False) -> Dict[str, Any]:
    """加载所有配置文件到内存缓存"""
    global _cache
    if _cache and not force_reload:
        return _cache

    _cache = {
        'strategy': _load_yaml('strategy.yaml'),
        'risk': _load_yaml('risk.yaml'),
        'system': _load_yaml('system.yaml'),
    }
    return _cache


def get(section: str, key: str = '', default: Any = None) -> Any:
    """
    获取配置值。

    用法:
      get('system', 'account.balance')  → 100
      get('strategy', 'short_overbought.exits.tp1_pct')  → 5.0
      get('risk', 'global.max_daily_loss')  → 30
    """
    cfg = load_all()
    section_data = cfg.get(section, {})

    if not key:
        return section_data

    keys = key.split('.')
    current = section_data
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k)
        else:
            return default
        if current is None:
            return default
    return current


def get_strategy_config(strategy_name: str) -> dict:
    """获取指定策略的完整配置"""
    cfg = load_all()
    return cfg.get('strategy', {}).get(strategy_name, {})


def get_risk_config(account_id: Optional[str] = None) -> dict:
    """获取风控配置，支持按账户覆盖"""
    cfg = load_all()
    risk = cfg.get('risk', {})
    base = dict(risk.get('global', {}))

    if account_id:
        accounts = risk.get('accounts', {})
        overrides = accounts.get(account_id, {})
        base.update(overrides)

    return base


def get_system_config() -> dict:
    """获取系统配置"""
    cfg = load_all()
    return cfg.get('system', {})


def reload():
    """强制重新加载配置"""
    load_all(force_reload=True)


# ══════════════════════════════════════════════════════════════════
#  向后兼容桥接 — 将 YAML 值注入老 config.py 模块
# ══════════════════════════════════════════════════════════════════

# YAML key → config.py 属性名 的映射
_SYSTEM_MAPPING = {
    'account.balance': 'ACCOUNT_BALANCE',
    'account.live_mode': 'LIVE_MODE',
    'account.position_mode': 'POSITION_MODE',
    'account.baseline_balance': 'BASELINE_BALANCE',
    'account.shadow_parallel': 'SHADOW_PARALLEL',
    'compound.enabled': 'AUTO_COMPOUND_ENABLED',
    'compound.step': 'COMPOUND_STEP',
    'compound.increase': 'COMPOUND_INCREASE',
    'compound.max_stake': 'COMPOUND_MAX_STAKE',
    'candidates.expire_hours': 'CANDIDATE_EXPIRE_HOURS',
    'candidates.check_open_exec_timeout_sec': 'CHECK_CANDIDATES_OPEN_EXEC_TIMEOUT_SEC',
    'candidates.check_hard_timeout_sec': 'CHECK_CANDIDATES_HARD_TIMEOUT_SEC',
    'auto_optimize.enabled': 'AUTO_OPTIMIZE_ENABLED',
    'auto_optimize.day': 'AUTO_OPTIMIZE_DAY',
    'scheduler.task_timeout_seconds': 'TASK_TIMEOUT_SECONDS',
    'scheduler.check_candidates_interval_minutes': 'CHECK_CANDIDATES_INTERVAL_MINUTES',
    'scheduler.check_candidates_budget_sec': 'CHECK_CANDIDATES_BUDGET_SEC',
    'scheduler.check_candidates_per_candidate_sec': 'CHECK_CANDIDATES_PER_CANDIDATE_SEC',
    'scheduler.check_candidates_parallelism': 'CHECK_CANDIDATES_PARALLELISM',
    'exchanges.binance.max_open_trades': 'MAX_OPEN_TRADES',
    'exchanges.binance.leverage': 'LEVERAGE',
    'exchanges.binance.slippage_alert_pct': 'SLIPPAGE_ALERT_PCT',
    'exchanges.routing.primary_exchange': 'PRIMARY_EXCHANGE',
    'exchanges.routing.primary_fallback': 'PRIMARY_EXCHANGE_FALLBACK',
    'exchanges.routing.price_divergence_max_pct': 'PRICE_DIVERGENCE_MAX_PCT',
    'backtest.slippage_pct': 'BACKTEST_SLIPPAGE_PCT',
    'backtest.fee_pct': 'BACKTEST_FEE_PCT',
    'backtest.default_days': 'BATCH_BACKTEST_DAYS',
    'backtest.correlation_threshold': 'BATCH_CORRELATION_THRESHOLD',
    'archive.days': 'TRADES_ARCHIVE_DAYS',
    'notifications.ws_disconnect_alert_minutes': 'WS_DISCONNECT_ALERT_MINUTES',
    'weekly_report.enabled': 'WEEKLY_REPORT_ENABLED',
    'weekly_report.roi_grade_a': 'WEEKLY_ROI_GRADE_A',
    'weekly_report.roi_grade_b': 'WEEKLY_ROI_GRADE_B',
    'weekly_report.roi_grade_c': 'WEEKLY_ROI_GRADE_C',
}

_STRATEGY_MAPPING = {
    # short_overbought YAML → config.py 属性名
    'scan.vol_min': 'VOL_MIN',
    'scan.price_max': 'PRICE_MAX',
    'scan.pct_24h_min': 'PCT_24H_MIN',
    'rsi.period': 'RSI_PERIOD',
    'rsi.daily_min': 'DAILY_RSI_MIN',
    'rsi.h4_enter': 'H4_RSI_ENTER',
    'rsi.h4_drop': 'H4_RSI_DROP',
    'rsi.h4_peak_lookback': 'H4_RSI_PEAK_LOOKBACK',
    'yao.oi_change_min': 'OI_CHANGE_MIN',
    'yao.funding_max': 'FUNDING_MAX',
    'yao.funding_min': 'FUNDING_MIN',
    'yao.funding_hot': 'FUNDING_HOT',
    'abandon.body_drop_pct': 'ABANDON_BODY_DROP_PCT',
    'abandon.consecutive': 'ABANDON_CONSECUTIVE',
    'abandon.oi_drop_pct': 'ABANDON_OI_DROP_PCT',
    'exits.tp1_pct': ('TP1_MULTIPLIER', lambda v: 1 - v / 100),
    'exits.tp2_pct': ('TP2_MULTIPLIER', lambda v: 1 - v / 100),
    'exits.tp1_close_ratio': 'TP1_CLOSE_RATIO',
    'exits.hard_stop_pct': 'HARD_STOP_LOSS_PCT',
    'exits.trail_activate_pct': 'TRAIL_STOP_ACTIVATE_PCT',
    'exits.trail_retrace_ratio': 'TRAIL_STOP_RETRACE_RATIO',
    'exits.max_hold_hours': ('MAX_HOLD_DAYS', lambda v: v / 24),
    'scoring.full_threshold': 'SCORE_FULL_THRESHOLD',
    'scoring.half_threshold': 'SCORE_HALF_THRESHOLD',
    'scoring.skip_threshold': 'SCORE_SKIP_THRESHOLD',
    'btc_filter.enabled': 'BTC_FILTER_ENABLED',
    'btc_filter.crash_threshold': 'BTC_CRASH_THRESHOLD',
    'btc_filter.pump_threshold': 'BTC_PUMP_THRESHOLD',
    'okx_cross.enabled': 'OKX_CROSS_VALIDATE_ENABLED',
    'okx_cross.bonus': 'OKX_CROSS_VALIDATE_BONUS',
    'position.default_stake': 'DEFAULT_STAKE',
    'position.leverage': 'LEVERAGE',
}

_RISK_MAPPING = {
    'max_daily_loss': 'RISK_MAX_DAILY_LOSS',
    'max_daily_trades': 'RISK_MAX_DAILY_TRADES',
    'consecutive_loss_pause': 'RISK_CONSECUTIVE_LOSS_PAUSE',
    'pause_hours': 'RISK_PAUSE_HOURS',
    'max_position_pct': 'RISK_MAX_POSITION_PCT',
    'cooldown_hours': 'COOLDOWN_HOURS',
    'cooldown_scope': 'COOLDOWN_SCOPE',
}


def _resolve_nested(data: dict, dotted_key: str, default=None):
    """从嵌套 dict 取值"""
    keys = dotted_key.split('.')
    current = data
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k)
        else:
            return default
        if current is None:
            return default
    return current


def apply_yaml_to_config():
    """
    将 YAML 配置值覆盖到 config.py 模块的全局变量。

    调用时机：程序启动时（scheduler.py / realtime_monitor.py 的 main 开头）。
    只覆盖 YAML 中明确设置的值，保留 config.py 中的其他值不变。

    这是一个过渡方案——最终目标是所有模块直接读 YAML，
    但在过渡期间通过注入的方式保证旧代码也能拿到 YAML 值。
    """
    import sys
    cfg_module = sys.modules[__name__]  # 即 config 包本身

    yaml_cfg = load_all(force_reload=True)
    applied = 0

    # 1. 系统配置
    system = yaml_cfg.get('system', {})
    for yaml_key, attr_name in _SYSTEM_MAPPING.items():
        value = _resolve_nested(system, yaml_key)
        if value is not None:
            setattr(cfg_module, attr_name, value)
            applied += 1

    # 2. 策略配置（short_overbought → config.py 平铺属性）
    strategy = yaml_cfg.get('strategy', {}).get('short_overbought', {})
    for yaml_key, mapping in _STRATEGY_MAPPING.items():
        value = _resolve_nested(strategy, yaml_key)
        if value is not None:
            if isinstance(mapping, tuple):
                attr_name, transform = mapping
                value = transform(value)
            else:
                attr_name = mapping
            setattr(cfg_module, attr_name, value)
            applied += 1

    # 3. 风控配置
    risk = yaml_cfg.get('risk', {}).get('global', {})
    for yaml_key, attr_name in _RISK_MAPPING.items():
        value = risk.get(yaml_key)
        if value is not None:
            setattr(cfg_module, attr_name, value)
            applied += 1

    if applied > 0:
        logger.info(f"📋 YAML 配置已注入 config.py: {applied} 个属性")

    return applied



# ══════════════════════════════════════════════════════════════════
#  S5 修复（2026-05）: 统一配置解析器
# ══════════════════════════════════════════════════════════════════
#
# 历史背景
# --------
# 项目存在 4 级配置（按优先级从高到低）：
#   1. 环境变量（os.environ）
#   2. runtime_config.json + admin_secrets.json[settings]（admin panel）
#   3. config/*.yaml（项目级）
#   4. config/_defaults.py（代码兜底）
#
# 但**没有任何函数**能直接告诉你"key X 的最终生效值是什么、来自哪一层"。
# 实际机制是各模块在启动时 / 周期性调 ``apply_yaml_to_config()`` +
# ``runtime_config.apply_overrides()`` 把值"灌"到 config 模块的全局属性，
# 调用方读 ``config.X`` 时已经分不清来源。
#
# ``resolve()`` 提供唯一对外解析入口：
#
#   from config import resolve
#   value = resolve('DEFAULT_STAKE')           # → 当前生效值
#   value, source = resolve('LEVERAGE', with_source=True)
#                                              # → (10, 'runtime_config')
#
# 解析顺序：env > runtime_config > admin_secrets settings > yaml > defaults
# 仅做"读"，不修改任何状态。

import json as _s5_json


_S5_RUNTIME_CONFIG_FILE = os.path.join(
    os.path.dirname(_CONFIG_DIR), 'runtime_config.json'
)
_S5_ADMIN_SECRETS_FILE = os.path.join(
    os.path.dirname(_CONFIG_DIR), 'admin_secrets.json'
)


def _s5_load_json(path: str) -> dict:
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return _s5_json.load(fh) or {}
    except (FileNotFoundError, _s5_json.JSONDecodeError, OSError):
        return {}


def _s5_layer_env(key: str):
    """1. 环境变量层 — 仅当 key 完全匹配 env 变量名时返回"""
    if key in os.environ:
        return os.environ[key]
    return _MISSING


def _s5_layer_runtime(key: str):
    """2. runtime_config.json 层（含全局 + 活跃账户覆盖）"""
    data = _s5_load_json(_S5_RUNTIME_CONFIG_FILE)
    if not data:
        return _MISSING
    # v3 结构：{"_global": {...}, "_exchanges": {...}, "<acc_id>": {...}}
    # 非 v3：扁平 {"KEY": val}
    if '_global' in data or '_exchanges' in data:
        global_section = data.get('_global', {}) or {}
        if key in global_section:
            return global_section[key]
        # 活跃账户
        try:
            from admin_secrets import get_active_account_id as _gaa
            acc_id = _gaa()
        except Exception:
            acc_id = None
        if acc_id and isinstance(data.get(acc_id), dict) and key in data[acc_id]:
            return data[acc_id][key]
    elif key in data:
        return data[key]
    return _MISSING


def _s5_layer_admin_secrets(key: str):
    """3. admin_secrets.json 的 settings 字段"""
    data = _s5_load_json(_S5_ADMIN_SECRETS_FILE)
    settings = data.get('settings') if isinstance(data, dict) else None
    if isinstance(settings, dict) and key in settings:
        return settings[key]
    return _MISSING


def _s5_layer_yaml(key: str):
    """
    4. YAML 层 — 反向查 ``_SYSTEM_MAPPING`` / ``_STRATEGY_MAPPING`` /
    ``_RISK_MAPPING``，找到 attr_name == key 的 yaml 路径，再 ``get(...)``。
    """
    yaml_cfg = load_all()

    # 系统
    for yaml_key, attr_name in _SYSTEM_MAPPING.items():
        if attr_name == key:
            v = _resolve_nested(yaml_cfg.get('system', {}), yaml_key)
            if v is not None:
                return v

    # 策略
    strategy_root = yaml_cfg.get('strategy', {}).get('short_overbought', {})
    for yaml_key, mapping in _STRATEGY_MAPPING.items():
        target = mapping[0] if isinstance(mapping, tuple) else mapping
        if target == key:
            v = _resolve_nested(strategy_root, yaml_key)
            if v is not None:
                if isinstance(mapping, tuple):
                    _, transform = mapping
                    try:
                        v = transform(v)
                    except Exception:
                        pass
                return v

    # 风控
    risk_root = yaml_cfg.get('risk', {}).get('global', {})
    for yaml_key, attr_name in _RISK_MAPPING.items():
        if attr_name == key and yaml_key in risk_root:
            return risk_root[yaml_key]

    return _MISSING


def _s5_layer_defaults(key: str):
    """5. 代码层兜底 — 直接读已注入本模块的 _defaults.py 全局变量"""
    if key in globals() and not key.startswith('_'):
        return globals()[key]
    return _MISSING


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return '<MISSING>'


_MISSING = _Missing()


_S5_LAYERS = (
    ('env',           _s5_layer_env),
    ('runtime_config', _s5_layer_runtime),
    ('admin_secrets', _s5_layer_admin_secrets),
    ('yaml',          _s5_layer_yaml),
    ('defaults',      _s5_layer_defaults),
)


def resolve(key: str, default: Any = None, *, with_source: bool = False):
    """
    按 4 级配置优先级读取 ``key`` 的最终生效值。

    优先级（高 → 低）：
      env > runtime_config > admin_secrets.settings > yaml > defaults

    Args:
        key: 配置键名（与 config 模块属性名一致，如 'DEFAULT_STAKE'）
        default: 全部层都缺失时返回的默认值
        with_source: True 时返回 ``(value, source_name)`` 元组

    Returns:
        值，或（with_source=True 时）``(value, source)``。

    例子::

        from config import resolve
        stake = resolve('DEFAULT_STAKE')
        stake, src = resolve('DEFAULT_STAKE', with_source=True)
        # → (33, 'runtime_config')

    注意：本函数**只读**，不会修改 config 模块属性。如果你需要"应用"
    新值到运行时进程，仍要调 ``apply_yaml_to_config()`` 或
    ``runtime_config.apply_overrides()``。
    """
    for layer_name, layer_fn in _S5_LAYERS:
        try:
            v = layer_fn(key)
        except Exception:
            continue
        if v is _MISSING:
            continue
        return (v, layer_name) if with_source else v
    return (default, 'default') if with_source else default


def explain(key: str) -> Dict[str, Any]:
    """
    返回每一层对 ``key`` 的可见值，方便 admin panel / debug 时定位
    "为什么生效值是这个"。

    Returns:
        {
          'final_value': ...,
          'final_source': 'runtime_config',
          'layers': {
            'env': <MISSING> | value,
            'runtime_config': ...,
            'admin_secrets': ...,
            'yaml': ...,
            'defaults': ...,
          },
        }
    """
    out: Dict[str, Any] = {'layers': {}}
    final_value: Any = None
    final_source = 'default'
    found = False
    for layer_name, layer_fn in _S5_LAYERS:
        try:
            v = layer_fn(key)
        except Exception as e:
            v = f'<error: {e}>'
        out['layers'][layer_name] = '<MISSING>' if v is _MISSING else v
        if not found and v is not _MISSING and not isinstance(v, str):
            final_value, final_source, found = v, layer_name, True
        elif not found and v is not _MISSING:
            final_value, final_source, found = v, layer_name, True
    out['final_value'] = final_value
    out['final_source'] = final_source
    return out
