"""
实时价格 + background push（M1 拆分自 dashboard.py）

提供：
  * ``fetch_live_prices(symbols)``  — 从 Binance Futures fapi 批量取价
  * ``inject_live_prices(data)``    — 把缓存价格写入 dashboard data 中
                                      持仓的 ``current_price`` 字段
  * ``start_background_push(socketio, data_provider)`` — 在 socketio
                                      上启动每 10 秒推送的协程

为什么必须留在 dashboard.py 旁边
==============================
Eventlet hub 已经被 dashboard.py 入口 ``monkey_patch`` 过；这里只用 stdlib
+ ``socketio.sleep / start_background_task``，并不直接依赖 eventlet 的私有
API，所以独立一个模块没有耦合问题。
"""

from __future__ import annotations

import json
import threading
from typing import Callable, Dict, List


_live_prices: Dict[str, float] = {}
_price_lock = threading.Lock()


def fetch_live_prices(symbols: List[str]) -> Dict[str, float]:
    """
    从 Binance Futures (fapi) 批量获取持仓币种实时价格。

    复用与原 dashboard.py 完全相同的实现：
      * 只拉持仓里的 symbols（``?symbols=[...]`` 参数）— 避免拉全市场
      * ``timeout=3s`` — 失败时让前端 BinanceWS 直连兜底，不阻塞推送 loop
      * 返回 ``{ccxt_symbol: price}`` 字典；失败返回空 dict
    """
    import requests as _requests

    prices: Dict[str, float] = {}
    if not symbols:
        return prices

    binance_syms = [s.replace('/USDT', 'USDT').replace('/', '') for s in symbols]
    rev_map = dict(zip(binance_syms, symbols))

    try:
        params = {'symbols': json.dumps(binance_syms, separators=(',', ':'))}
        r = _requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/price",
            params=params,
            timeout=3,
        )
        if r.status_code == 200:
            payload = r.json()
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                bsym = item.get('symbol')
                if bsym in rev_map:
                    try:
                        prices[rev_map[bsym]] = float(item['price'])
                    except (TypeError, ValueError):
                        continue
    except Exception:
        pass

    return prices


def inject_live_prices(data: dict) -> dict:
    """把缓存中的 live price 写入 dashboard data 持仓的 ``current_price``"""
    with _price_lock:
        prices = _live_prices.copy()

    if not prices:
        return data

    for trade in data.get('short_trades', {}).get('open', []):
        sym = trade.get('symbol', '')
        if sym in prices:
            trade['current_price'] = prices[sym]

    for trade in data.get('long_trades', {}).get('open', []):
        sym = trade.get('symbol', '')
        if sym in prices:
            trade['current_price'] = prices[sym]

    return data


def make_background_push(socketio, data_provider: Callable[[], dict]):
    """
    返回一个可以传给 ``socketio.start_background_task`` 的协程函数。
    ``data_provider`` 必须是 zero-arg、返回 dashboard data 的可调用对象。

    职责（与原 ``background_push`` 完全一致）：
      * ``socketio.sleep(10)`` — 让出协程，避免 hub 卡死
      * 收集 open positions 的 symbols → ``fetch_live_prices`` → 缓存
      * ``inject_live_prices(data)`` → ``socketio.emit('update', data)``
      * 任何异常都被吞掉，保证推送 loop 永不退出
    """

    def _push_loop():
        while True:
            try:
                socketio.sleep(10)
                data = data_provider()

                open_symbols = set()
                for trade in data.get('short_trades', {}).get('open', []):
                    open_symbols.add(trade.get('symbol', ''))
                for trade in data.get('long_trades', {}).get('open', []):
                    open_symbols.add(trade.get('symbol', ''))
                open_symbols.discard('')

                if open_symbols:
                    prices = fetch_live_prices(list(open_symbols))
                    if prices:
                        with _price_lock:
                            _live_prices.update(prices)

                data = inject_live_prices(data)
                socketio.emit('update', data)
            except Exception as e:
                print(f"[Dashboard] 推送异常: {e}")

    return _push_loop


__all__ = [
    'fetch_live_prices',
    'inject_live_prices',
    'make_background_push',
]
