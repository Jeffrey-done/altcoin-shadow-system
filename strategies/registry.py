"""
策略注册中心
管理所有策略的注册、发现、加载和生命周期。

用法:
  from strategies.registry import StrategyRegistry

  # 注册策略
  registry = StrategyRegistry()
  registry.register(ShortOverboughtStrategy())

  # 或通过装饰器自动注册
  @registry.auto_register
  class MyStrategy(BaseStrategy): ...

  # 获取策略
  strategy = registry.get('short_overbought')
  all_strategies = registry.get_all()
  active_strategies = registry.get_active()
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import Dict, List, Optional, Type, TYPE_CHECKING

from strategies.base import BaseStrategy

if TYPE_CHECKING:
    # 仅用于类型注解,运行时不需要(避免循环 import)
    from strategies.base import (
        DataFeed, MarketSnapshot, Candidate, Signal, ExitSignal,
    )

logger = logging.getLogger("strategy_registry")


def _flatten_strategy_config(cfg: dict, valid_keys: set) -> dict:
    """把 config/strategy.yaml 的分层配置映射到策略参数的扁平 key。"""
    special = {
        ('rsi', 'period'): 'rsi_period',
        ('rsi', 'daily_min'): 'daily_rsi_min',
        ('rsi', 'daily_max'): 'daily_rsi_max',
        ('rsi', 'h4_enter'): 'h4_rsi_enter',
        ('rsi', 'h4_drop'): 'h4_rsi_drop',
        ('rsi', 'h4_rise'): 'h4_rsi_rise',
        ('rsi', 'h4_peak_lookback'): 'h4_rsi_peak_lookback',
        ('abandon', 'body_drop_pct'): 'abandon_body_drop_pct',
        ('abandon', 'consecutive'): 'abandon_consecutive',
        ('abandon', 'oi_drop_pct'): 'abandon_oi_drop_pct',
        ('btc_filter', 'enabled'): 'btc_filter_enabled',
        ('btc_filter', 'crash_threshold'): 'btc_crash_threshold',
        ('btc_filter', 'pump_threshold'): 'btc_pump_threshold',
        ('okx_cross', 'enabled'): 'okx_cross_validate_enabled',
        ('okx_cross', 'bonus'): 'okx_cross_validate_bonus',
    }
    out = {}

    def walk(node, path=()):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            next_path = path + (key,)
            if not path and key in ('enabled', 'version'):
                continue
            if isinstance(value, dict):
                walk(value, next_path)
                continue
            candidates = [special.get(next_path), key, '_'.join(next_path)]
            for param_key in candidates:
                if param_key and param_key in valid_keys:
                    out[param_key] = value
                    break

    walk(cfg or {})
    return out


def _apply_yaml_config(strategy: BaseStrategy) -> bool:
    """应用 config/strategy.yaml 中的 enabled 与参数覆盖。"""
    try:
        from config import get_strategy_config
        cfg = get_strategy_config(strategy.name) or {}
    except Exception as exc:
        logger.debug(f"读取策略配置失败 {strategy.name}: {exc}")
        cfg = {}

    try:
        valid_keys = set(strategy.get_params().keys())
        overrides = _flatten_strategy_config(cfg, valid_keys)
        if overrides:
            strategy.set_params(overrides)
            logger.info(f"策略参数覆盖: {strategy.name} {sorted(overrides.keys())}")
    except Exception as exc:
        logger.warning(f"策略参数覆盖失败 {strategy.name}: {exc}")

    return bool(cfg.get('enabled', True))


class StrategyRegistry:
    """
    策略注册中心 — 单例模式管理所有策略实例。

    职责:
      - 注册/注销策略
      - 按名称查找策略
      - 管理策略启用/禁用状态
      - 自动发现 strategies/ 目录下的策略模块
    """

    _instance: Optional[StrategyRegistry] = None
    _strategies: Dict[str, BaseStrategy]
    _enabled: Dict[str, bool]

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._strategies = {}
            cls._instance._enabled = {}
        return cls._instance

    def register(self, strategy: BaseStrategy, enabled: bool = True) -> None:
        """
        注册策略实例。

        参数:
          strategy: BaseStrategy 实例
          enabled: 是否启用（禁用的策略不参与扫描/确认循环）
        """
        name = strategy.name
        if name in self._strategies:
            logger.warning(f"策略 '{name}' 已注册，将被覆盖（新版本: {strategy.version}）")

        self._strategies[name] = strategy
        self._enabled[name] = enabled
        logger.info(f"📦 策略注册: {strategy} [{'启用' if enabled else '禁用'}]")

    def unregister(self, name: str) -> bool:
        """注销策略"""
        if name in self._strategies:
            del self._strategies[name]
            del self._enabled[name]
            logger.info(f"📦 策略注销: {name}")
            return True
        return False

    def get(self, name: str) -> Optional[BaseStrategy]:
        """按名称获取策略"""
        return self._strategies.get(name)

    def get_all(self) -> List[BaseStrategy]:
        """获取所有已注册策略"""
        return list(self._strategies.values())

    def get_active(self) -> List[BaseStrategy]:
        """获取所有启用的策略"""
        return [s for name, s in self._strategies.items() if self._enabled.get(name, True)]

    def enable(self, name: str) -> bool:
        """启用策略"""
        if name in self._strategies:
            self._enabled[name] = True
            logger.info(f"✅ 策略启用: {name}")
            return True
        return False

    def disable(self, name: str) -> bool:
        """禁用策略"""
        if name in self._strategies:
            self._enabled[name] = False
            logger.info(f"⏸️ 策略禁用: {name}")
            return True
        return False

    def is_enabled(self, name: str) -> bool:
        """检查策略是否启用"""
        return self._enabled.get(name, False)

    def list_names(self) -> List[str]:
        """列出所有策略名"""
        return list(self._strategies.keys())

    def summary(self) -> List[Dict[str, str]]:
        """获取所有策略的摘要信息"""
        return [
            {
                'name': s.name,
                'version': s.version,
                'description': s.description,
                'direction': s.direction.value,
                'enabled': self._enabled.get(s.name, True),
                'params_count': len(s.get_params()),
            }
            for s in self._strategies.values()
        ]

    # ── 自动发现 ─────────────────────────────────────────────────

    def auto_discover(self, strategies_dir: Optional[str] = None) -> int:
        """
        自动发现并注册 strategies/ 子目录中的策略。

        规则：
          - 每个策略是一个子目录（如 strategies/short_overbought/）
          - 子目录中必须有 __init__.py 导出 strategy_class 变量
          - strategy_class 必须是 BaseStrategy 的子类

        返回:
          成功注册的策略数量
        """
        if strategies_dir is None:
            strategies_dir = os.path.dirname(os.path.abspath(__file__))

        count = 0
        for item in os.listdir(strategies_dir):
            item_path = os.path.join(strategies_dir, item)
            if not os.path.isdir(item_path):
                continue
            if item.startswith('_') or item.startswith('.'):
                continue

            init_file = os.path.join(item_path, '__init__.py')
            if not os.path.exists(init_file):
                continue

            try:
                module = importlib.import_module(f'strategies.{item}')
                strategy_class = getattr(module, 'strategy_class', None)

                if strategy_class is None:
                    # 尝试 create_strategy 工厂函数
                    factory = getattr(module, 'create_strategy', None)
                    if factory:
                        instance = factory()
                    else:
                        logger.debug(f"策略目录 '{item}' 没有导出 strategy_class 或 create_strategy，跳过")
                        continue
                elif isinstance(strategy_class, type) and issubclass(strategy_class, BaseStrategy):
                    instance = strategy_class()
                elif isinstance(strategy_class, BaseStrategy):
                    instance = strategy_class
                else:
                    logger.warning(f"策略目录 '{item}' 的 strategy_class 不是 BaseStrategy 子类，跳过")
                    continue

                enabled = _apply_yaml_config(instance)
                self.register(instance, enabled=enabled)
                count += 1
            except Exception as e:
                logger.error(f"加载策略 '{item}' 失败: {e}")

        logger.info(f"📦 自动发现完成：注册了 {count} 个策略")
        return count

    def auto_register(self, cls: Type[BaseStrategy]) -> Type[BaseStrategy]:
        """
        装饰器：自动注册策略类。

        用法:
          @registry.auto_register
          class MyStrategy(BaseStrategy): ...
        """
        instance = cls()
        self.register(instance)
        return cls

    # ── 重置（测试用）─────────────────────────────────────────────

    @classmethod
    def reset(cls) -> None:
        """重置注册中心（仅用于测试）"""
        if cls._instance:
            cls._instance._strategies = {}
            cls._instance._enabled = {}


# ══════════════════════════════════════════════════════════════════
#  策略引擎（调度所有策略的扫描/确认/退出循环）
# ══════════════════════════════════════════════════════════════════

class StrategyEngine:
    """
    策略引擎 — 调度所有已注册策略的执行循环。

    职责:
      - 按调度计划运行 scan / confirm / evaluate_exit
      - 汇总信号 → 风控审批 → 执行路由
      - 记录信号日志
      - 错误隔离（单个策略异常不影响其他策略）

    这是 scheduler.py 的下一代替代方案。
    当前版本为同步执行，后续可升级为 async。
    """

    def __init__(self, registry: Optional[StrategyRegistry] = None):
        self.registry = registry or StrategyRegistry()
        self._logger = logging.getLogger("strategy_engine")

    def run_scan_cycle(self, data_feed: 'DataFeed', market: 'MarketSnapshot') -> List[Candidate]:
        """
        运行一轮扫描：所有活跃策略并行扫描 → 汇总候选。
        自动注入 strategy_name 和 direction 到每个候选对象。
        """
        from strategies.base import Candidate as CandidateDTO, DataFeed, MarketSnapshot
        all_candidates: List[CandidateDTO] = []

        for strategy in self.registry.get_active():
            try:
                candidates = strategy.scan(data_feed, market)
                if candidates:
                    # 注入策略标识到每个候选
                    for c in candidates:
                        c.strategy_name = strategy.name
                        c.direction = strategy.direction.value
                    self._logger.info(
                        f"[{strategy.name}] 扫描产生 {len(candidates)} 个候选"
                    )
                    all_candidates.extend(candidates)
            except Exception as e:
                self._logger.error(
                    f"[{strategy.name}] scan 异常（已隔离）: {e}", exc_info=True
                )

        return all_candidates

    def run_confirm_cycle(self, candidates: List[Dict], data_feed: 'DataFeed') -> List[Signal]:
        """
        运行一轮确认：遍历候选池 → 各策略确认 → 汇总信号。
        """
        from strategies.base import Candidate as CandidateDTO, Signal
        signals: List[Signal] = []

        for strategy in self.registry.get_active():
            strategy_candidates = [
                c for c in candidates
                if c.get('strategy', 'short_overbought') == strategy.name
            ]

            for c_data in strategy_candidates:
                try:
                    metadata = dict(c_data.get('metadata') or {})
                    if c_data.get('metadata_json') and not metadata:
                        try:
                            import json as _json
                            metadata.update(_json.loads(c_data.get('metadata_json') or '{}'))
                        except Exception:
                            pass
                    for _key in (
                        'rsi_1d', 'rsi_4h', 'rsi_4h_peak', 'pct24h', 'pct_24h',
                        'vol24h', 'vol_24h', 'oi_change', 'funding_rate', 'yao_score',
                    ):
                        if _key in c_data:
                            metadata.setdefault(_key, c_data[_key])
                    if 'pct24h' in metadata and 'pct_24h' not in metadata:
                        metadata['pct_24h'] = metadata['pct24h']
                    if 'pct_24h' in metadata and 'pct24h' not in metadata:
                        metadata['pct24h'] = metadata['pct_24h']
                    if 'vol24h' in metadata and 'vol_24h' not in metadata:
                        metadata['vol_24h'] = metadata['vol24h']
                    if 'vol_24h' in metadata and 'vol24h' not in metadata:
                        metadata['vol24h'] = metadata['vol_24h']

                    candidate = CandidateDTO(
                        symbol=c_data['symbol'],
                        price=c_data.get('price', 0),
                        score=c_data.get('score', 0),
                        metadata=metadata,
                    )
                    signal = strategy.confirm(candidate, data_feed)
                    if signal:
                        signal.strategy_name = strategy.name
                        signal.strategy_version = strategy.version
                        signals.append(signal)
                        self._logger.info(
                            f"[{strategy.name}] 信号触发: {signal.symbol} "
                            f"score={signal.score} {signal.trigger_type}"
                        )
                except Exception as e:
                    self._logger.error(
                        f"[{strategy.name}] confirm 异常 ({c_data.get('symbol', '?')}): {e}",
                        exc_info=True,
                    )

        return signals

    def run_exit_cycle(self, open_trades: List[Dict], data_feed: 'DataFeed') -> List[ExitSignal]:
        """
        运行一轮退出检查：遍历持仓 → 各策略评估退出。
        """
        from strategies.base import TradeContext, ExitSignal
        exit_signals: List[ExitSignal] = []

        for trade_data in open_trades:
            strategy_name = trade_data.get('strategy', 'short_overbought')
            strategy = self.registry.get(strategy_name)
            if not strategy:
                self._logger.warning(f"交易 {trade_data.get('id')} 的策略 '{strategy_name}' 未注册，跳过退出评估")
                continue

            try:
                ctx = TradeContext(
                    trade_id=trade_data['id'],
                    symbol=trade_data['symbol'],
                    direction=trade_data.get('direction', 'SHORT'),
                    entry_price=trade_data['entry_price'],
                    current_price=trade_data.get('current_price', 0),
                    stake=trade_data['stake'],
                    stake_remaining=trade_data.get('stake_remaining', trade_data['stake']),
                    leverage=trade_data.get('leverage', 10),
                    shares=trade_data.get('shares', 0),
                    opened_at=str(trade_data.get('opened_at', '')),
                    pnl_pct=trade_data.get('pnl_pct', 0),
                    best_pnl_pct=trade_data.get('best_pnl_pct', 0),
                    hold_hours=trade_data.get('hold_hours', 0),
                    tp1_triggered=trade_data.get('tp1_triggered', False),
                    tp1_locked_pnl=trade_data.get('tp1_locked_pnl', 0),
                    hard_stop_price=trade_data.get('hard_stop_price'),
                    trail_stop_price=trade_data.get('trail_stop_price'),
                    exchange=trade_data.get('exchange', 'shadow'),
                    account_id=trade_data.get('account_id', ''),
                )
                exit_signal = strategy.evaluate_exit(ctx, data_feed)
                if exit_signal:
                    exit_signal.strategy_name = strategy.name
                    exit_signals.append(exit_signal)
                    self._logger.info(
                        f"[{strategy.name}] 退出信号: {ctx.symbol} "
                        f"reason={exit_signal.reason.value}"
                    )
            except Exception as e:
                self._logger.error(
                    f"[{strategy_name}] evaluate_exit 异常 ({trade_data.get('symbol', '?')}): {e}",
                    exc_info=True,
                )

        return exit_signals
