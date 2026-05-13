#!/usr/bin/env python3
"""
实时止盈止损监控器 v2.1
通过 Binance WebSocket 7×24小时监控所有持仓价格，
触及止盈/止损位时立即执行平仓。

替代原来每小时一次的 altcoin_tracker 检查，延迟从60分钟降到 ~100ms。

启动：python3 realtime_monitor.py
Docker：作为独立服务运行

工作原理：
  1. 每30秒读取交易文件，把持仓的关键阈值（TP1/TP2/硬止损/trail）
     缓存到内存（不含 best_pnl_pct 等浮动字段）
  2. 连接 Binance WebSocket miniTicker 流
  3. 每收到价格更新：
     - 先在内存里用缓存阈值做"是否可能触发"快速判断（零磁盘 IO）
     - 只有价格确实进入触发区间时，才抢文件锁做完整 evaluate_trade
  4. 触发后立即执行 evaluate_trade() 平仓逻辑
  5. 持仓变化时自动更新订阅列表

v2.1 优化：
  - 回调中不再每 tick 都读写 JSON 文件（原来 5 持仓 × 1Hz ≈ 每秒 5 次磁盘 IO）
  - best_pnl_pct 这类纯统计字段由 snapshot 刷新线程批量 flush
  - 触发判断在内存完成，磁盘 IO 从"每 tick 一次"降到"每次真正触发一次"
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
#  内存快照：避免每 tick 都读磁盘
# ══════════════════════════════════════════════════════════════════

# 内存快照：{symbol: [thresholds_dict, ...]}  多个 trade 共享同一个 symbol 时也覆盖
_snapshot_lock = threading.Lock()
_trade_snapshots: dict = {}  # symbol -> list of threshold dicts

# H4: 跨进程 snapshot 失效机制
#   - _snapshot_mtime: 本进程上次刷新 snapshot 时读到的 TRADES_FILE mtime
#   - _snapshot_version: 同进程的 tracker / scanner 修改后递增版本号，
#     通过 _refresh_snapshot_from_trades 立即推送到内存（同进程路径）
#   - on_price_update 入口每次都比对 TRADES_FILE mtime，变化则强制 refresh
#     → 跨进程（tracker 进程和 monitor 进程分开部署）也能秒级同步
_snapshot_mtime: float = 0.0
_snapshot_version: int = 0


def _current_trades_mtime() -> float:
    """读 TRADES_FILE 的 mtime，不存在时返回 0"""
    try:
        return os.path.getmtime(TRADES_FILE)
    except OSError:
        return 0.0


def _build_trade_snapshot(trade: Trade) -> dict:
    """把一笔持仓的"触发阈值"提炼成内存字典（不含浮动字段）"""
    return {
        'id': trade.id,
        'symbol': trade.symbol,
        'direction': trade.direction,
        'entry_price': trade.entry_price,
        'tp1': trade.take_profit_1,
        'tp2': trade.take_profit_2,
        'tp1_triggered': trade.tp1_triggered,
        'hard_stop': trade.hard_stop_price,
        'trail_stop': trade.trail_stop_price,
    }


def refresh_snapshot():
    """扫描 trades 文件，重建内存快照。每 30s 调用一次即可。

    H4：强制读取 TRADES_FILE mtime 作为 snapshot 版本基线，
    on_price_update 入口若检测到 mtime > _snapshot_mtime 会强制调这个函数，
    使得跨进程（tracker 进程改了 trades.json，monitor 进程监听价格）也能秒级看到
    保本止损价的更新，而不是等 30s 定时轮询。
    """
    global _snapshot_mtime, _snapshot_version
    # 顺带热加载 runtime_config；admin 面板改了 config 后 30s 内在本进程生效
    try:
        from runtime_config import apply_overrides as _apply_rc
        _apply_rc()
    except Exception:
        pass
    # 读 mtime 先于读文件内容：若文件在读期间被改，下次 on_price_update 仍会比对到
    mtime_before = _current_trades_mtime()
    try:
        trades_raw = load_json(TRADES_FILE, [])
    except Exception as e:
        logger.warning(f"读取 trades 失败: {e}")
        return

    snapshots: dict = {}
    for t in trades_raw:
        if t.get('status') != 'open':
            continue
        try:
            trade = Trade.from_dict(t)
            snap = _build_trade_snapshot(trade)
            snapshots.setdefault(trade.symbol, []).append(snap)
        except Exception as e:
            logger.debug(f"构建快照失败: {e}")

    with _snapshot_lock:
        _trade_snapshots.clear()
        _trade_snapshots.update(snapshots)
        _snapshot_mtime = mtime_before
        _snapshot_version += 1


def _refresh_snapshot_from_trades(trades: list):
    """
    从已在内存中的 Trade 对象列表立刻刷新快照。
    用于 TP1 触发后立刻让保本止损价进入内存，不用等 30s 定时刷新。

    H4：同进程路径（tracker + monitor 在同一 python 进程）直接改内存；
    跨进程路径依赖 mtime 比对（trades 文件被其他进程修改后，本进程下次 tick 会
    检测到 mtime 变化并强制 refresh_snapshot）。
    """
    global _snapshot_mtime, _snapshot_version
    snapshots: dict = {}
    for trade in trades:
        if trade.status != 'open':
            continue
        try:
            snap = _build_trade_snapshot(trade)
            snapshots.setdefault(trade.symbol, []).append(snap)
        except Exception:
            pass

    with _snapshot_lock:
        _trade_snapshots.clear()
        _trade_snapshots.update(snapshots)
        # 同进程路径：同步把 mtime 更新到当前值，避免 on_price_update 又去抢锁刷一次
        _snapshot_mtime = _current_trades_mtime()
        _snapshot_version += 1


def _price_crosses_threshold(snap: dict, price: float) -> bool:
    """
    内存快速判断：当前价格是否进入任何关闭/TP 触发区间。
    命中则回 True，上层才会去抢锁做完整 evaluate。
    """
    direction = snap['direction']

    # 硬止损
    hs = snap.get('hard_stop')
    if hs:
        if direction == 'SHORT' and price >= hs:
            return True
        if direction == 'LONG' and price <= hs:
            return True

    # 移动止损（只有设置过才算）
    ts = snap.get('trail_stop')
    if ts:
        if direction == 'SHORT' and price >= ts:
            return True
        if direction == 'LONG' and price <= ts:
            return True

    # TP1（未触发才判）
    if not snap.get('tp1_triggered'):
        tp1 = snap.get('tp1')
        if tp1:
            if direction == 'SHORT' and price <= tp1:
                return True
            if direction == 'LONG' and price >= tp1:
                return True
    else:
        # TP1 已触发，继续看 TP2
        tp2 = snap.get('tp2')
        if tp2:
            if direction == 'SHORT' and price <= tp2:
                return True
            if direction == 'LONG' and price >= tp2:
                return True

    return False


# ══════════════════════════════════════════════════════════════════
#  持仓加载（用于 WebSocket 订阅列表）
# ══════════════════════════════════════════════════════════════════

def load_open_trades() -> list:
    """加载所有持仓中的交易"""
    trades_raw = load_json(TRADES_FILE, [])
    trades = [Trade.from_dict(t) for t in trades_raw]
    return [t for t in trades if t.status == 'open']


def get_all_open_symbols() -> set:
    """获取所有持仓中的币种集合"""
    with _snapshot_lock:
        return set(_trade_snapshots.keys())


# ══════════════════════════════════════════════════════════════════
#  实时止盈止损检查
# ══════════════════════════════════════════════════════════════════

def check_main_trades(symbol: str, price: float):
    """
    检查交易是否触发止盈止损。
    触发后立即执行平仓并保存。
    使用 LockedJsonFile 确保 read-modify-write 原子性。

    注意：副作用（record_trade_closed / send_tg / 交易所 execute_close）必须在
    save() 成功后、出锁再执行，否则崩溃时会出现"风控记了账但交易没落盘"的幽灵亏损。
    """
    from altcoin_tracker import evaluate_trade, _perform_exchange_close

    pending_risk_updates = []   # [(pnl_usd, stake_remaining, close_reason, symbol, direction), ...]
    pending_alerts = []
    pending_exchange_closes = []  # [(trade_ref, action, amount), ...]

    with LockedJsonFile(TRADES_FILE, default=[]) as (trades_raw, save):
        trades = [Trade.from_dict(t) for t in trades_raw]

        any_updated = False
        for trade in trades:
            if trade.status != 'open' or trade.symbol != symbol:
                continue

            result = evaluate_trade(trade, price)

            if result.closed:
                any_updated = True
                pending_risk_updates.append((
                    result.pnl_usd, trade.stake_remaining,
                    trade.close_reason, trade.symbol, trade.direction,
                    trade.account_id,
                ))
                if result.alert_msg:
                    pending_alerts.append(result.alert_msg)
            elif result.updated:
                any_updated = True

            # 真实平仓动作（TP1 半仓 or 全仓）
            if result.pending_exchange_action and trade.exchange != 'shadow':
                pending_exchange_closes.append((
                    trade, result.pending_exchange_action, result.pending_close_amount,
                ))

        if any_updated:
            save([t.to_dict() for t in trades])
            # 立即刷新内存快照：TP1 触发后 trail_stop_price 已变成保本止损，
            # 必须立刻反映到内存里，否则 30s 内价格反弹不会触发保本止损
            _refresh_snapshot_from_trades(trades)

    # ══ 出锁后才触发副作用 ══
    # 1) 实盘发平仓单（JSON 已持久化，失败只会让交易所有悬仓但不会污染 risk_state）
    for trade, action, close_amount in pending_exchange_closes:
        _perform_exchange_close(trade, action, close_amount)

    # 2) 改风控（此时交易已经落盘）
    for pnl_usd, stake_remaining, close_reason, sym, direction, acc_id in pending_risk_updates:
        record_trade_closed(pnl_usd, stake_remaining, account_id=acc_id or None)
        logger.info(
            f"⚡ 实时平仓: {sym} | {direction} | "
            f"原因={close_reason} | PnL={pnl_usd:+.2f}U"
        )
    # 3) 再推送
    for msg in pending_alerts:
        send_tg(msg)


# 最后一次 mtime 比对的时间戳（节流：每秒最多比对一次，不在每个 tick 都 stat）
_last_mtime_check: float = 0.0
_mtime_check_interval: float = 1.0  # 秒


def _maybe_refresh_on_mtime_change():
    """
    H4：检查 TRADES_FILE mtime 是否变化，变化则强制刷新 snapshot。
    节流到每秒最多一次 stat，高频价格 tick 时 CPU 开销可忽略。
    """
    global _last_mtime_check
    now_ts = time.time()
    if now_ts - _last_mtime_check < _mtime_check_interval:
        return
    _last_mtime_check = now_ts

    current_mtime = _current_trades_mtime()
    # 第一次启动时 _snapshot_mtime=0，refresh_snapshot 会被 main() 首次调用触发
    # 之后只要 mtime 推进就强制 refresh
    if current_mtime > 0 and current_mtime != _snapshot_mtime:
        logger.debug(
            f"检测到 trades 文件 mtime 变化 ({_snapshot_mtime} → {current_mtime})，强制刷新 snapshot"
        )
        refresh_snapshot()


def on_price_update(symbol: str, price: float):
    """
    价格更新回调：先做内存快速判断，只有价格进入触发区间才抢锁做完整评估。
    由 WebSocket 消息处理调用，每秒可能数次，必须零磁盘 IO 在无触发路径。

    H4：入口增加 mtime 检查，感知其他进程（tracker / scanner / tg_bot）对
    trades.json 的修改，秒级同步新的 trail_stop_price / tp1_triggered 等关键阈值。
    """
    try:
        # 感知跨进程修改（节流每秒一次 stat）
        _maybe_refresh_on_mtime_change()

        with _snapshot_lock:
            snaps = list(_trade_snapshots.get(symbol, []))
        if not snaps:
            return

        # 快速过滤：任一 snap 命中才继续
        if not any(_price_crosses_threshold(s, price) for s in snaps):
            return

        # 命中了 — 抢锁做完整 evaluate_trade + 可能的平仓
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
        self._connected = False
        self._disconnected_since = time.time()
        self._disconnect_alerted = False

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
        self._connected = False
        self._disconnected_since = time.time()
        logger.info(f"WebSocket 断开 (code={close_status_code})")

    def _on_open(self, ws):
        self._connected = True
        self._disconnect_alerted = False
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

    last_refresh = 0
    while True:
        try:
            # 每 30s 刷新一次内存快照
            now_ts = time.time()
            if now_ts - last_refresh > 30:
                refresh_snapshot()
                last_refresh = now_ts

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
    logger.info("⚡ 实时止盈止损监控器启动 v2.1")
    logger.info(f"   模式: {'WebSocket' if websocket else '轮询(5秒)'}")
    logger.info(f"   监控: 做空交易")
    logger.info("=" * 50)

    # 先做一次快照，否则首次 WebSocket 连接时拿不到 symbols
    refresh_snapshot()

    if not websocket:
        # 无 WebSocket 库，使用轮询模式
        polling_mode()
        return

    monitor = BinanceWSMonitor()

    # 每30秒刷新 snapshot + 检查持仓变化
    last_symbols: set = set()

    while monitor.running:
        try:
            # 每轮都刷新内存快照（已平仓的会被 tracker 写出，refresh 时自动剔除）
            refresh_snapshot()
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

        # WebSocket 断线告警（仅在有持仓且确认断开时检查）
        if last_symbols and not monitor._connected:
            disconnect_duration = time.time() - monitor._disconnected_since
            if disconnect_duration > config.WS_DISCONNECT_ALERT_MINUTES * 60 and not monitor._disconnect_alerted:
                monitor._disconnect_alerted = True
                send_tg(
                    f"🔌 <b>WebSocket 断线告警</b>\n\n"
                    f"已断开 {disconnect_duration/60:.1f} 分钟\n"
                    f"监控币种: {', '.join(last_symbols)}\n"
                    f"正在尝试重连..."
                )
                logger.error(f"WebSocket 断线超 {config.WS_DISCONNECT_ALERT_MINUTES} 分钟")
                # 尝试重连
                monitor.connect(list(last_symbols))


if __name__ == '__main__':
    main()
