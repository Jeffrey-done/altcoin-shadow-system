#!/usr/bin/env python3
"""
快速预筛模块：WebSocket 全市场 ticker 流
标记 24h 涨幅 > 15% 的币，供 scan_daily() 优先处理。

不做任何交易决策，只负责"标记热门币"。
RSI 计算仍然由 scan_daily() 在 K 线闭合后进行。
"""

import json
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from common import setup_logger

logger = setup_logger("hot_scanner")

try:
    import websocket
except ImportError:
    websocket = None
    logger.warning("websocket-client 未安装，快速预筛不可用")

# ══════════════════════════════════════════════════════════════════
#  共享状态：热门币集合
# ══════════════════════════════════════════════════════════════════

_hot_symbols_lock = threading.Lock()
_hot_symbols: dict = {}  # {symbol: {'pct24h': float, 'price': float, 'vol': float, 'marked_at': float}}


def get_hot_symbols() -> dict:
    """获取当前热门币快照（线程安全）"""
    with _hot_symbols_lock:
        return _hot_symbols.copy()


def clear_hot_symbols():
    """清空热门币集合（scan_daily 处理完后调用）"""
    with _hot_symbols_lock:
        _hot_symbols.clear()


def get_hot_symbol_list() -> list:
    """返回热门币的 symbol 列表（ccxt 格式），供 scan_daily 优先处理"""
    with _hot_symbols_lock:
        return list(_hot_symbols.keys())


# ══════════════════════════════════════════════════════════════════
#  WebSocket 处理
# ══════════════════════════════════════════════════════════════════

def _on_message(ws, message):
    """处理全市场 miniTicker 流"""
    try:
        tickers = json.loads(message)
        if not isinstance(tickers, list):
            return

        now = time.time()
        for t in tickers:
            symbol_raw = t.get('s', '')  # e.g. "PEPEUSDT"
            if not symbol_raw.endswith('USDT'):
                continue

            # 计算 24h 涨幅
            open_price = float(t.get('o', 0))  # open price 24h ago
            close_price = float(t.get('c', 0))  # current price
            if open_price <= 0:
                continue

            pct24h = (close_price - open_price) / open_price * 100
            quote_vol = float(t.get('q', 0))  # 24h quote volume

            # 只标记满足条件的
            if pct24h >= config.PCT_24H_MIN and close_price <= config.PRICE_MAX and quote_vol >= config.VOL_MIN:
                # 转换为 ccxt 格式
                base = symbol_raw.replace('USDT', '')
                ccxt_symbol = f"{base}/USDT"

                with _hot_symbols_lock:
                    _hot_symbols[ccxt_symbol] = {
                        'pct24h': round(pct24h, 1),
                        'price': close_price,
                        'vol': round(quote_vol),
                        'marked_at': now,
                    }

    except Exception:
        pass  # 忽略解析错误


def _on_error(ws, error):
    logger.warning(f"Hot scanner WS 错误: {error}")


def _on_close(ws, code, msg):
    logger.info(f"Hot scanner WS 断开 (code={code})")


def _on_open(ws):
    logger.info("Hot scanner WS 已连接，监控全市场 ticker")


def _cleanup_stale_entries():
    """清理超过 2 小时的旧标记（防止内存泄漏）"""
    now = time.time()
    with _hot_symbols_lock:
        stale = [sym for sym, data in _hot_symbols.items()
                 if now - data['marked_at'] > 7200]  # 2小时
        for sym in stale:
            del _hot_symbols[sym]


# ══════════════════════════════════════════════════════════════════
#  主循环
# ══════════════════════════════════════════════════════════════════

def run_hot_scanner():
    """启动快速预筛 WebSocket（阻塞式，适合独立线程运行）"""
    if not websocket:
        logger.error("websocket-client 未安装，无法启动快速预筛")
        return

    url = "wss://stream.binance.com:9443/ws/!miniTicker@arr"

    while True:
        try:
            logger.info("连接 Binance 全市场 miniTicker 流...")
            ws = websocket.WebSocketApp(
                url,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
                on_open=_on_open,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            logger.error(f"Hot scanner 异常: {e}")

        # 断线后等 5 秒重连
        logger.info("Hot scanner 断线，5秒后重连...")
        time.sleep(5)


def start_hot_scanner_thread():
    """在后台线程启动快速预筛（非阻塞）"""
    if not websocket:
        logger.warning("websocket-client 未安装，跳过快速预筛")
        return None

    thread = threading.Thread(target=run_hot_scanner, daemon=True, name="hot-scanner")
    thread.start()
    logger.info("快速预筛后台线程已启动")

    # 同时启动清理线程
    def cleanup_loop():
        while True:
            time.sleep(300)  # 每5分钟清理一次
            _cleanup_stale_entries()

    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True, name="hot-scanner-cleanup")
    cleanup_thread.start()

    return thread


if __name__ == '__main__':
    run_hot_scanner()
