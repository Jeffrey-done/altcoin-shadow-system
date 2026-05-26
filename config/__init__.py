"""
YAML 分层配置管理
从 config/ 目录加载 YAML 文件，提供类型安全的配置访问。

配置优先级（高→低）:
  1. 环境变量（DATABASE_URL 等）
  2. runtime_config.json（admin panel 动态修改）
  3. config/*.yaml（项目级默认值）
  4. 代码内置默认值（config_legacy.py）

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
#  关键：从 config_legacy.py 导入全部常量，保证向后兼容
#  所有旧代码 `import config; config.LEVERAGE` 仍然正常工作
# ══════════════════════════════════════════════════════════════════
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_legacy_path = os.path.join(_SCRIPT_DIR, 'config_legacy.py')

if os.path.exists(_legacy_path):
    import importlib.util
    _spec = importlib.util.spec_from_file_location('config_legacy', _legacy_path)
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
    'compound.enabled': 'AUTO_COMPOUND_ENABLED',
    'compound.step': 'COMPOUND_STEP',
    'compound.increase': 'COMPOUND_INCREASE',
    'compound.max_stake': 'COMPOUND_MAX_STAKE',
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
