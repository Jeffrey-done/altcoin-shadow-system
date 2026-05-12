#!/usr/bin/env python3
"""
实时止盈止损监控器 v2.0
通过 Binance WebSocket 7×24小时监控所有持仓价格，
触及止盈/止损位时立即执行平仓。

替代原来每小时一次的 altcoin_tracker 检查，延迟从60分钟降到 ~100ms。

启动：python3 realtime_monitor.py
Docker：作为独立服务运行

工作原理：
  1. 每30秒读取交易文件，收集持仓中的币种
  2. 连接 Binance WebSocket miniTicker 流
  3. 每收到价格更新就检查是否触及止盈/止损
  4. 触发后立即执行 evaluate_trade() 平仓逻辑
  5. 持仓变化时自动更新订阅列表
"""

import json
import os
import sys
import time
import threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from common import (
    TRADES_FILE,
    setup_logger, send_tg, atomic_write_json, load_json,
    utcnow_iso, to_binance_symbol, LockedJsonFile,
)
from models import Trade
from risk_control import record_trade_closed

logger = setup_logger("realtime_monitor")

# WebSocket 库（使用 websocket-client 同步版本，兼容性好）
try:
    import websocket
except ImportError:
    # 如果没有 websocket-client，回退用标准库
    websocket = None
    logger.warning("websocket-client 未安装，尝试使用 requests 轮询模式")

try:
    import requests
except ImportError:
    requests = None


# ══════════════════════════════════════════════════════════════════
#  持仓加载
# ══════════════════════════════════════════════════════════════════

def load_open_trades() -> list:
    """加载所有持仓中的交易"""
    trades_raw = load_json(TRADES_FILE, [])
    trades = [Trade.from_dict(t) for t in trades_raw]
    return [t for t in trades if t.status == 'open']


def get_all_open_symbols() -> set:
    """获取所有持仓中的币种集合"""
    symbols = set()
    for t in load_open_trades():
        symbols.add(t.symbol)
    symbols.discard('')
    return symbols


# ══════════════════════════════════════════════════════════════════
#  实时止盈止损检查
# ══════════════════════════════════════════════════════════════════

def check_main_trades(symbol: str, price: float):
    """
    检查交易是否触发止盈止损。
    触发后立即执行平仓并保存。
    使用 LockedJsonFile 确保 read-modify-write 原子性。
    """
    from altcoin_tracker import evaluate_trade

    with LockedJsonFile(TRADES_FILE, default=[]) as (trades_raw, save):
        trades = [Trade.from_dict(t) for t in trades_raw]

        any_updated = False
        for trade in trades:
            if trade.status != 'open' or trade.symbol != symbol:
                continue

            result = evaluate_trade(trade, price)

            if result.closed:
                any_updated = True
                # 记录风控
                record_trade_closed(result.pnl_usd, trade.stake_remaining)
                # TG推送
                if result.alert_msg:
                    send_tg(result.alert_msg)
                logger.info(
                    f"⚡ 实时平仓: {trade.symbol} | {trade.direction} | "
                    f"原因={trade.close_reason} | PnL={result.pnl_usd:+.2f}U"
                )
            elif result.updated:
                any_updated = True

        if any_updated:
            save([t.to_dict() for t in trades])


def on_price_update(symbol: str, price: float):
    """
    价格更新回调：检查所有持仓是否触发止盈止损。
    由 WebSocket 消息处理调用。
    """
    try:
        check_main_trades(symbol, price)
    except Exception as e:
        logger.error(f"价格更新处理异常 ({symbol}): {e}")


# ══════════════════════════════════════════════════════════════════
#  WebSocket 连接管理
# ══════════════════════════════════════════════════════════════════

class BinanceWSMonitor:
    """Binance WebSocket 实时价格监控器"""

    def __init__(self):
        self.ws = None
        self.current_symbols = []
        self.running = True
        self._lock = threading.Lock()

    def _build_url(self, symbols: list) -> str:
        """构建 combined stream URL"""
        streams = []
        for sym in symbols:
            bin_sym = sym.replace('/USDT', 'usdt').replace('/', '').lower()
            streams.append(f"{bin_sym}@miniTicker")
        return f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

    def _on_message(self, ws, message):
        """WebSocket 消息回调"""
        try:
            msg = json.loads(message)
            data = msg.get('data', {})
            if not data or 's' not in data or 'c' not in data:
                return

            bin_sym = data['s']  # e.g. "PEPEUSDT"
            price = float(data['c'])  # 最新价

            # 转回 ccxt 格式
            ccxt_sym = None
            for sym in self.current_symbols:
                if sym.replace('/USDT', 'USDT').replace('/', '') == bin_sym:
                    ccxt_sym = sym
                    break

            if ccxt_sym and price > 0:
                on_price_update(ccxt_sym, price)
        except Exception as e:
            pass  # 忽略解析错误，继续处理下一条

    def _on_error(self, ws, error):
        logger.warning(f"WebSocket 错误: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        logger.info(f"WebSocket 断开 (code={close_status_code})")

    def _on_open(self, ws):
        logger.info(f"WebSocket 已连接，监控 {len(self.current_symbols)} 个币种")

    def connect(self, symbols: list):
        """连接或重连 WebSocket"""
        with self._lock:
            # 关闭旧连接
            if self.ws:
                try:
                    self.ws.close()
                except:
                    pass
                self.ws = None

            if not symbols:
                logger.info("无持仓，等待...")
                return

            self.current_symbols = symbols
            url = self._build_url(symbols)

            logger.info(f"连接 Binance WS: {len(symbols)} 个流")
            logger.info(f"  监控币种: {', '.join(symbols)}")

            self.ws = websocket.WebSocketApp(
                url,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open,
            )

            # 在新线程中运行 WebSocket（阻塞式）
            ws_thread = threading.Thread(
                target=self.ws.run_forever,
                kwargs={'ping_interval': 20, 'ping_timeout': 10},
                daemon=True,
            )
            ws_thread.start()

    def stop(self):
        """停止监控"""
        self.running = False
        if self.ws:
            self.ws.close()


# ══════════════════════════════════════════════════════════════════
#  轮询模式（WebSocket 不可用时的备选方案）
# ══════════════════════════════════════════════════════════════════

def polling_mode():
    """
    如果 websocket-client 未安装，使用 REST API 轮询模式。
    每5秒获取一次价格（比原来60分钟好很多）。
    """
    logger.info("启动轮询模式（每5秒检查一次）")

    while True:
        try:
            symbols = get_all_open_symbols()
            if not symbols:
                time.sleep(30)
                continue

            # 批量获取价格
            r = requests.get(
                "https://api.binance.com/api/v3/ticker/price",
                timeout=5,
            )
            if r.status_code != 200:
                time.sleep(5)
                continue

            all_prices = {item['symbol']: float(item['price']) for item in r.json()}

            for sym in symbols:
                bin_sym = sym.replace('/USDT', 'USDT').replace('/', '')
                if bin_sym in all_prices:
                    on_price_update(sym, all_prices[bin_sym])

        except Exception as e:
            logger.error(f"轮询异常: {e}")

        time.sleep(5)


# ══════════════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════════════

def main():
    """实时监控主入口"""
    logger.info("=" * 50)
    logger.info("⚡ 实时止盈止损监控器启动")
    logger.info(f"   模式: {'WebSocket' if websocket else '轮询(5秒)'}")
    logger.info(f"   监控: 做空交易")
    logger.info("=" * 50)

    if not websocket:
        # 无 WebSocket 库，使用轮询模式
        polling_mode()
        return

    monitor = BinanceWSMonitor()

    # 每30秒检查持仓变化，必要时重新连接
    last_symbols = set()

    while monitor.running:
        try:
            current_symbols = get_all_open_symbols()

            if current_symbols != last_symbols:
                if current_symbols:
                    logger.info(f"持仓变化: {last_symbols} → {current_symbols}")
                    monitor.connect(list(current_symbols))
                else:
                    logger.info("所有持仓已平仓，断开 WebSocket")
                    if monitor.ws:
                        monitor.ws.close()
                last_symbols = current_symbols

        except Exception as e:
            logger.error(f"监控循环异常: {e}")

        time.sleep(30)


if __name__ == '__main__':
    main()
