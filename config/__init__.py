"""
YAML 分层配置管理
从 config/ 目录加载 YAML 文件，提供类型安全的配置访问。

配置优先级（高→低）:
  1. 环境变量（DATABASE_URL 等）
  2. runtime_config.json（admin panel 动态修改）
  3. config/*.yaml（项目级默认值）
  4. 代码内置默认值
"""

import os
from typing import Any, Dict, Optional

import yaml


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
