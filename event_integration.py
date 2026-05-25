#!/usr/bin/env python3
"""
事件总线集成层 — 为旧模块提供即插即用的 Event Bus 接入。

旧模块（scheduler / realtime_monitor / dashboard）在启动时调用:
  from event_integration import init_event_system
  init_event_system('scheduler')

之后在关键操作点调用 emit_* 函数即可。
本模块同时负责：
  - 初始化 Event Bus
  - 加载 YAML 配置覆盖到 config.py
  - 启动 Portfolio Risk 的价格历史喂数据线程
  - 注册 config.changed 事件监听器（热加载）
"""

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger("event_integration")

_initialized = False
_price_feed_thread: Optional[threading.Thread] = None


def init_event_system(source: str = 'unknown'):
    """
    初始化事件系统 — 在进程启动时调用一次。

    执行：
      1. 加载 YAML 配置 → 注入 config.py
      2. 启动 Event Bus
      3. 注册配置热加载监听
      4. 启动 Portfolio Risk 价格喂数据（后台线程）
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    # 1. YAML 配置注入
    try:
        from config import apply_yaml_to_config
        apply_yaml_to_config()
    except Exception as e:
        logger.warning(f"YAML 配置加载失败（使用 config.py 默认值）: {e}")

    # 2. 启动 Event Bus
    try:
        from event_bus import get_event_bus
        bus = get_event_bus(source=source)
        bus.start()
        logger.info(f"📡 Event Bus 已启动 (source={source}, backend={bus.backend_type})")
    except Exception as e:
        logger.warning(f"Event Bus 启动失败（不影响核心业务）: {e}")

    # 3. 注册配置热加载
    try:
        from event_bus import get_event_bus, Event

        def _on_config_changed(event: Event):
            """收到配置变更事件时重新加载 YAML"""
            try:
                from config import apply_yaml_to_config
                apply_yaml_to_config()
                logger.info("🔄 配置热加载完成")
            except Exception as ex:
                logger.warning(f"配置热加载失败: {ex}")

        bus = get_event_bus()
        bus.subscribe('config.changed', _on_config_changed, subscriber_id=f'{source}_config')
    except Exception:
        pass

    # 4. 启动价格历史喂数据（Portfolio Risk 用）
    _start_price_feed()


def _start_price_feed():
    """后台线程：定期获取持仓币种价格历史，喂给 PortfolioRiskManager"""
    global _price_feed_thread

    def _feed_loop():
        while True:
            try:
                _update_portfolio_price_history()
            except Exception as e:
                logger.debug(f"价格历史更新失败: {e}")
            time.sleep(300)  # 每 5 分钟更新一次

    _price_feed_thread = threading.Thread(
        target=_feed_loop,
        name='portfolio_price_feed',
        daemon=True,
    )
    _price_feed_thread.start()


def _update_portfolio_price_history():
    """获取所有候选/持仓币种的 1h 收盘价序列，喂给 PortfolioRisk"""
    try:
        from db.compat import load_open_trades, load_all_trades
        from risk.portfolio import PortfolioRiskManager, PortfolioRiskConfig
        import config as cfg

        # 获取当前持仓的 symbols
        open_trades = load_open_trades()
        symbols = list({t.get('symbol', '') for t in open_trades if t.get('symbol')})

        if not symbols:
            return

        # 获取价格历史
        from exchange_manager import get_binance
        exchange = get_binance()

        # 获取/创建 portfolio risk 实例
        from engine_adapter import _get_portfolio_risk
        portfolio_mgr = _get_portfolio_risk()

        for symbol in symbols[:10]:  # 限制 API 调用数
            try:
                klines = exchange.fetch_ohlcv(symbol, '1h', limit=168)  # 7 天
                if klines:
                    closes = [k[4] for k in klines]
                    portfolio_mgr.update_price_history(symbol, closes)
            except Exception:
                pass
            time.sleep(0.2)  # Rate limit

        # 更新余额
        balance = getattr(cfg, 'ACCOUNT_BALANCE', 100)
        portfolio_mgr.update_balance(balance)

    except ImportError:
        pass  # 模块不可用时静默
    except Exception as e:
        logger.debug(f"Portfolio 价格更新异常: {e}")


# ══════════════════════════════════════════════════════════════════
#  便捷集成函数（旧模块在关键位置调用）
# ══════════════════════════════════════════════════════════════════

def on_trade_opened(trade_id: str, symbol: str, direction: str,
                    stake: float, exchange: str = 'shadow',
                    account_id: str = '', **extra):
    """在 altcoin_scanner 开仓成功后调用"""
    try:
        from event_bus import emit_trade_opened
        emit_trade_opened(trade_id, symbol, direction, stake, exchange, account_id, **extra)
    except Exception:
        pass


def on_trade_closed(trade_id: str, symbol: str, pnl: float,
                    close_type: str, close_reason: str = '',
                    exchange: str = 'shadow', **extra):
    """在 altcoin_tracker / realtime_monitor 平仓完成后调用"""
    try:
        from event_bus import emit_trade_closed
        emit_trade_closed(trade_id, symbol, pnl, close_type, close_reason, exchange, **extra)
    except Exception:
        pass


def on_candidate_added(symbol: str, rsi_1d: float, score: float = 0, **extra):
    """在 altcoin_scanner scan_daily 新增候选后调用"""
    try:
        from event_bus import emit_candidate_added
        emit_candidate_added(symbol, rsi_1d, score, **extra)
    except Exception:
        pass


def on_risk_alert(alert_type: str, message: str, account_id: str = '', **extra):
    """在 risk_control 触发告警时调用"""
    try:
        from event_bus import emit_risk_alert
        emit_risk_alert(alert_type, message, account_id, **extra)
    except Exception:
        pass


def on_signal_scored(symbol: str, strategy: str, score: int,
                     grade: str, triggered: bool = False, **extra):
    """在 signal_score 评分完成后调用"""
    try:
        from event_bus import emit_signal_scored
        emit_signal_scored(symbol, strategy, score, grade, triggered, **extra)
    except Exception:
        pass
