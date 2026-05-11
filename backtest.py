#!/usr/bin/env python3
"""
回测引擎 v1.0
用 Binance 历史 K 线数据回放策略，验证参数有效性。

用法：
  python3 backtest.py                    # 默认参数回测
  python3 backtest.py --grid             # 参数网格搜索
  python3 backtest.py --symbol PEPE/USDT # 指定币种
  python3 backtest.py --days 90          # 指定天数

输出：
  - 胜率、盈亏比、最大回撤、夏普率、连亏次数
  - 每笔交易明细
  - 网格搜索时输出 Top10 参数组合
"""

import sys
import os
import time
import json
import itertools
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from common import setup_logger, atomic_write_json

logger = setup_logger("backtest")


# ══════════════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class BacktestParams:
    """回测参数集（可被网格搜索替换）"""
    rsi_period: int = config.RSI_PERIOD
    daily_rsi_min: float = config.DAILY_RSI_MIN
    h4_rsi_enter: float = config.H4_RSI_ENTER
    h4_rsi_drop: float = config.H4_RSI_DROP
    tp1_pct: float = round((1 - config.TP1_MULTIPLIER) * 100, 2)  # 5%
    tp2_pct: float = round((1 - config.TP2_MULTIPLIER) * 100, 2)  # 10%
    tp1_close_ratio: float = config.TP1_CLOSE_RATIO
    hard_stop_pct: float = config.HARD_STOP_LOSS_PCT
    trail_activate_pct: float = config.TRAIL_STOP_ACTIVATE_PCT
    trail_drawdown_pct: float = config.TRAIL_STOP_DRAWDOWN_PCT
    max_hold_bars: int = 24  # 24根1h K线 = 24小时
    leverage: int = config.LEVERAGE
    stake: float = config.DEFAULT_STAKE


@dataclass
class BacktestTrade:
    """回测单笔交易记录"""
    symbol: str
    entry_price: float
    entry_time: str
    exit_price: float = 0.0
    exit_time: str = ''
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    exit_reason: str = ''
    hold_bars: int = 0
    tp1_hit: bool = False


@dataclass
class BacktestResult:
    """回测结果汇总"""
    params: dict
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_loss_ratio: float = 0.0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    max_consecutive_losses: int = 0
    sharpe_ratio: float = 0.0
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d['trades'] = [asdict(t) for t in self.trades]
        return d


# ══════════════════════════════════════════════════════════════════
#  RSI 计算（Wilder）
# ══════════════════════════════════════════════════════════════════

def calc_rsi_series(closes: List[float], period: int = 14) -> List[float]:
    """计算完整 RSI 序列（Wilder 平滑），返回与 closes 等长的列表（前 period 个为 NaN）"""
    n = len(closes)
    rsi = [50.0] * n  # 默认50

    if n < period + 1:
        return rsi

    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [max(d, 0) for d in deltas]
    losses_arr = [max(-d, 0) for d in deltas]

    # SMA 初始化
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses_arr[:period]) / period

    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rsi[period] = 100 - (100 / (1 + avg_gain / avg_loss))

    # Wilder 递推
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses_arr[i]) / period
        idx = i + 1  # closes 的索引
        if avg_loss == 0:
            rsi[idx] = 100.0
        else:
            rsi[idx] = round(100 - (100 / (1 + avg_gain / avg_loss)), 2)

    return rsi



# ══════════════════════════════════════════════════════════════════
#  历史数据加载
# ══════════════════════════════════════════════════════════════════

def fetch_historical_klines(symbol: str, timeframe: str = '1h',
                            days: int = 90) -> List[dict]:
    """
    从 Binance 获取历史 K 线数据。
    返回: [{"time": "2025-01-01T00:00", "open": x, "high": x, "low": x, "close": x, "volume": x}, ...]
    """
    try:
        import ccxt
        exchange = ccxt.binance({'enableRateLimit': True})

        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        all_ohlcv = []
        limit = 1000

        logger.info(f"获取 {symbol} 最近 {days} 天 {timeframe} K线...")

        while True:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
            if not ohlcv:
                break
            all_ohlcv.extend(ohlcv)
            since = ohlcv[-1][0] + 1
            if len(ohlcv) < limit:
                break
            time.sleep(0.1)

        klines = []
        for o in all_ohlcv:
            klines.append({
                "time": datetime.fromtimestamp(o[0] / 1000, tz=timezone.utc).isoformat(),
                "open": o[1],
                "high": o[2],
                "low": o[3],
                "close": o[4],
                "volume": o[5],
            })

        logger.info(f"  共获取 {len(klines)} 根 K 线")
        return klines

    except Exception as e:
        logger.error(f"获取历史数据失败: {e}")
        return []


def load_cached_klines(symbol: str, timeframe: str = '1h', days: int = 90) -> List[dict]:
    """加载缓存的 K 线，不存在则从 API 获取并缓存"""
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_cache')
    os.makedirs(cache_dir, exist_ok=True)

    safe_symbol = symbol.replace('/', '_')
    cache_file = os.path.join(cache_dir, f"{safe_symbol}_{timeframe}_{days}d.json")

    if os.path.exists(cache_file):
        # 检查缓存是否过期（超过1天重新拉取）
        mtime = os.path.getmtime(cache_file)
        if time.time() - mtime < 86400:
            logger.info(f"使用缓存: {cache_file}")
            with open(cache_file, 'r') as f:
                return json.load(f)

    klines = fetch_historical_klines(symbol, timeframe, days)
    if klines:
        with open(cache_file, 'w') as f:
            json.dump(klines, f)
    return klines


# ══════════════════════════════════════════════════════════════════
#  信号检测（回测版：用预计算 RSI 序列，不调 API）
# ══════════════════════════════════════════════════════════════════

def detect_entry_signals(klines_1h: List[dict], params: BacktestParams) -> List[int]:
    """
    在 1H K 线上检测做空入场信号。
    简化逻辑（回测用）：
      1. 计算 RSI（用24根1H近似4H RSI 效果）
      2. RSI 曾达到高位（>daily_rsi_min）后回落到 h4_rsi_enter 以下
      3. 回落幅度 >= h4_rsi_drop

    返回触发信号的 K 线索引列表。
    """
    closes = [k['close'] for k in klines_1h]
    rsi_series = calc_rsi_series(closes, params.rsi_period)

    signals = []
    lookback = 20  # 往回看20根找峰值
    min_gap = params.max_hold_bars  # 两次信号之间最少间隔

    last_signal_idx = -min_gap  # 初始化

    for i in range(lookback + params.rsi_period, len(rsi_series)):
        current_rsi = rsi_series[i]

        # 当前 RSI 需低于进入阈值
        if current_rsi >= params.h4_rsi_enter:
            continue

        # 近期峰值
        peak_start = max(0, i - lookback)
        recent_peak = max(rsi_series[peak_start:i])

        # 峰值需超过日线阈值
        if recent_peak < params.daily_rsi_min:
            continue

        # 回落幅度
        drop = recent_peak - current_rsi
        if drop < params.h4_rsi_drop:
            continue

        # 信号间隔
        if i - last_signal_idx < min_gap:
            continue

        signals.append(i)
        last_signal_idx = i

    return signals



# ══════════════════════════════════════════════════════════════════
#  模拟交易执行
# ══════════════════════════════════════════════════════════════════

def simulate_trade(klines: List[dict], entry_idx: int,
                   params: BacktestParams) -> BacktestTrade:
    """
    从 entry_idx 开始模拟一笔做空交易。
    用后续 K 线的 high/low 判断是否触发止盈/止损。

    优先级：硬止损 > TP1 > TP2 > 移动止损 > 时间止损
    """
    entry_price = klines[entry_idx]['close']
    entry_time = klines[entry_idx]['time']

    trade = BacktestTrade(
        symbol='',
        entry_price=entry_price,
        entry_time=entry_time,
    )

    # 计算关键价位
    tp1_price = entry_price * (1 - params.tp1_pct / 100)
    tp2_price = entry_price * (1 - params.tp2_pct / 100)
    hard_stop_price = entry_price * (1 + params.hard_stop_pct / 100)

    # 状态
    tp1_triggered = False
    best_pnl_pct = 0.0
    trail_stop_price = None
    stake_remaining_ratio = 1.0  # 剩余仓位比例

    notional = params.stake * params.leverage

    for bar_offset in range(1, params.max_hold_bars + 1):
        bar_idx = entry_idx + bar_offset
        if bar_idx >= len(klines):
            break

        bar = klines[bar_idx]
        high = bar['high']
        low = bar['low']
        close_price = bar['close']

        # 做空：high 越高越亏，low 越低越赚
        # 最差情况用 high，最好情况用 low

        # ── 1. 硬止损检查（价格涨到 hard_stop）──
        if high >= hard_stop_price:
            exit_price = hard_stop_price
            pnl_pct = (entry_price - exit_price) / entry_price * 100
            # 如果 TP1 已触发，只算剩余仓位
            remaining_pnl = notional * stake_remaining_ratio * pnl_pct / 100
            tp1_pnl = 0
            if tp1_triggered:
                tp1_pnl = notional * params.tp1_close_ratio * params.tp1_pct / 100
            trade.exit_price = exit_price
            trade.exit_time = bar['time']
            trade.pnl_pct = pnl_pct
            trade.pnl_usd = round(tp1_pnl + remaining_pnl, 2)
            trade.exit_reason = 'hard_stop'
            trade.hold_bars = bar_offset
            trade.tp1_hit = tp1_triggered
            return trade

        # ── 2. TP1 检查（价格跌到 tp1）──
        if not tp1_triggered and low <= tp1_price:
            tp1_triggered = True
            stake_remaining_ratio = 1 - params.tp1_close_ratio
            trade.tp1_hit = True
            # 不退出，继续持有剩余仓位

        # ── 3. TP2 检查（价格跌到 tp2）──
        if tp1_triggered and low <= tp2_price:
            exit_price = tp2_price
            pnl_pct = (entry_price - exit_price) / entry_price * 100
            tp1_pnl = notional * params.tp1_close_ratio * params.tp1_pct / 100
            remaining_pnl = notional * stake_remaining_ratio * pnl_pct / 100
            trade.exit_price = exit_price
            trade.exit_time = bar['time']
            trade.pnl_pct = pnl_pct
            trade.pnl_usd = round(tp1_pnl + remaining_pnl, 2)
            trade.exit_reason = 'tp2'
            trade.hold_bars = bar_offset
            return trade

        # ── 4. 更新移动止损 ──
        current_pnl_pct = (entry_price - low) / entry_price * 100  # 最好盈利
        if current_pnl_pct > best_pnl_pct:
            best_pnl_pct = current_pnl_pct
            if best_pnl_pct >= params.trail_activate_pct:
                trail_stop_price = entry_price * (1 - (best_pnl_pct / 100 - params.trail_drawdown_pct))

        # ── 5. 移动止损触发 ──
        if trail_stop_price and high >= trail_stop_price and best_pnl_pct >= params.trail_activate_pct:
            exit_price = trail_stop_price
            pnl_pct = (entry_price - exit_price) / entry_price * 100
            tp1_pnl = 0
            if tp1_triggered:
                tp1_pnl = notional * params.tp1_close_ratio * params.tp1_pct / 100
            remaining_pnl = notional * stake_remaining_ratio * pnl_pct / 100
            trade.exit_price = exit_price
            trade.exit_time = bar['time']
            trade.pnl_pct = pnl_pct
            trade.pnl_usd = round(tp1_pnl + remaining_pnl, 2)
            trade.exit_reason = 'trail_stop'
            trade.hold_bars = bar_offset
            trade.tp1_hit = tp1_triggered
            return trade

    # ── 6. 时间止损（超时按收盘价平仓）──
    last_idx = min(entry_idx + params.max_hold_bars, len(klines) - 1)
    exit_price = klines[last_idx]['close']
    pnl_pct = (entry_price - exit_price) / entry_price * 100
    tp1_pnl = 0
    if tp1_triggered:
        tp1_pnl = notional * params.tp1_close_ratio * params.tp1_pct / 100
    remaining_pnl = notional * stake_remaining_ratio * pnl_pct / 100

    trade.exit_price = exit_price
    trade.exit_time = klines[last_idx]['time']
    trade.pnl_pct = pnl_pct
    trade.pnl_usd = round(tp1_pnl + remaining_pnl, 2)
    trade.exit_reason = 'time_stop'
    trade.hold_bars = params.max_hold_bars
    trade.tp1_hit = tp1_triggered
    return trade



# ══════════════════════════════════════════════════════════════════
#  统计分析
# ══════════════════════════════════════════════════════════════════

def calculate_stats(trades: List[BacktestTrade], params: BacktestParams) -> BacktestResult:
    """计算回测统计指标"""
    result = BacktestResult(params=asdict(params), trades=trades)

    if not trades:
        return result

    result.total_trades = len(trades)
    pnl_list = [t.pnl_usd for t in trades]

    result.wins = sum(1 for p in pnl_list if p > 0)
    result.losses = sum(1 for p in pnl_list if p <= 0)
    result.win_rate = round(result.wins / result.total_trades * 100, 1) if result.total_trades else 0

    win_pnls = [p for p in pnl_list if p > 0]
    loss_pnls = [p for p in pnl_list if p <= 0]

    result.avg_win = round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0
    result.avg_loss = round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0
    result.profit_loss_ratio = round(
        abs(result.avg_win / result.avg_loss), 2
    ) if result.avg_loss != 0 else 999

    result.total_pnl = round(sum(pnl_list), 2)

    # 权益曲线 & 最大回撤
    equity = [config.ACCOUNT_BALANCE]
    for pnl in pnl_list:
        equity.append(equity[-1] + pnl)
    result.equity_curve = [round(e, 2) for e in equity]

    peak = equity[0]
    max_dd = 0
    for e in equity:
        if e > peak:
            peak = e
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    result.max_drawdown = round(max_dd, 2)

    # 最大连亏
    max_consec = 0
    current_consec = 0
    for pnl in pnl_list:
        if pnl <= 0:
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0
    result.max_consecutive_losses = max_consec

    # 夏普率（简化版：无风险利率=0，用日收益标准差）
    if len(pnl_list) > 1:
        import statistics
        avg_pnl = statistics.mean(pnl_list)
        std_pnl = statistics.stdev(pnl_list)
        if std_pnl > 0:
            # 假设平均每天1~2笔，年化 = sqrt(365) ≈ 19
            result.sharpe_ratio = round((avg_pnl / std_pnl) * (365 ** 0.5 / 10), 2)
        else:
            result.sharpe_ratio = 0
    else:
        result.sharpe_ratio = 0

    return result


# ══════════════════════════════════════════════════════════════════
#  回测主流程
# ══════════════════════════════════════════════════════════════════

def run_backtest(symbol: str, days: int = 90,
                 params: Optional[BacktestParams] = None) -> BacktestResult:
    """对单个币种执行完整回测"""
    if params is None:
        params = BacktestParams()

    klines = load_cached_klines(symbol, '1h', days)
    if not klines:
        logger.error(f"无法获取 {symbol} 历史数据")
        return BacktestResult(params=asdict(params))

    # 检测信号
    signals = detect_entry_signals(klines, params)
    logger.info(f"检测到 {len(signals)} 个入场信号")

    # 模拟每笔交易
    trades = []
    for sig_idx in signals:
        trade = simulate_trade(klines, sig_idx, params)
        trade.symbol = symbol
        trades.append(trade)

    # 统计
    result = calculate_stats(trades, params)

    return result


def print_result(result: BacktestResult):
    """打印回测结果"""
    print("\n" + "=" * 60)
    print("  📊 回测结果")
    print("=" * 60)
    print(f"  总交易数：{result.total_trades}")
    print(f"  胜率：{result.win_rate}%（{result.wins}胜 / {result.losses}负）")
    print(f"  平均盈利：{result.avg_win:+.2f}U")
    print(f"  平均亏损：{result.avg_loss:+.2f}U")
    print(f"  盈亏比：{result.profit_loss_ratio:.2f}")
    print(f"  总盈亏：{result.total_pnl:+.2f}U")
    print(f"  最大回撤：{result.max_drawdown:.1f}%")
    print(f"  最大连亏：{result.max_consecutive_losses}次")
    print(f"  夏普率：{result.sharpe_ratio:.2f}")
    print("-" * 60)

    if result.trades:
        print("\n  📝 交易明细（最近10笔）:")
        print(f"  {'时间':<18} {'盈亏':>8} {'原因':<12} {'持仓':>4} {'TP1':>4}")
        for t in result.trades[-10:]:
            tp1_str = "✓" if t.tp1_hit else ""
            print(f"  {t.entry_time[:16]:<18} {t.pnl_usd:>+7.2f}U {t.exit_reason:<12} {t.hold_bars:>3}h {tp1_str:>4}")

    print("\n" + "=" * 60)

    # 实盘建议
    if result.win_rate >= 50 and result.profit_loss_ratio >= 1.5:
        print("  ✅ 参数达标！可以考虑小仓实盘验证")
    elif result.win_rate >= 40 and result.profit_loss_ratio >= 2.0:
        print("  ⚠️ 胜率偏低但盈亏比高，可以尝试但要严格执行止损")
    else:
        print("  ❌ 参数不佳，建议调优后重新回测")
    print()



# ══════════════════════════════════════════════════════════════════
#  参数网格搜索
# ══════════════════════════════════════════════════════════════════

def grid_search(symbol: str, days: int = 90) -> List[BacktestResult]:
    """
    遍历参数组合，找最优参数。
    搜索维度：
      - TP1: 3%, 5%, 7%
      - TP2: 8%, 10%, 15%
      - 硬止损: 2%, 3%, 5%
      - RSI 阈值: 72, 75, 78, 82
      - 回落点数: 8, 10, 12
    """
    tp1_options = [3, 5, 7]
    tp2_options = [8, 10, 15]
    stop_options = [2, 3, 5]
    rsi_options = [72, 75, 78, 82]
    drop_options = [8, 10, 12]

    total = len(tp1_options) * len(tp2_options) * len(stop_options) * len(rsi_options) * len(drop_options)
    logger.info(f"网格搜索: {total} 个参数组合")

    # 预加载数据（只下载一次）
    klines = load_cached_klines(symbol, '1h', days)
    if not klines:
        logger.error("无法获取数据")
        return []

    results = []
    count = 0

    for tp1, tp2, stop, rsi_min, drop in itertools.product(
        tp1_options, tp2_options, stop_options, rsi_options, drop_options
    ):
        if tp2 <= tp1:  # TP2 必须大于 TP1
            continue

        count += 1
        if count % 50 == 0:
            logger.info(f"  进度: {count}/{total}")

        params = BacktestParams(
            tp1_pct=tp1,
            tp2_pct=tp2,
            hard_stop_pct=stop,
            daily_rsi_min=rsi_min,
            h4_rsi_drop=drop,
        )

        signals = detect_entry_signals(klines, params)
        trades = [simulate_trade(klines, idx, params) for idx in signals]
        for t in trades:
            t.symbol = symbol
        result = calculate_stats(trades, params)
        results.append(result)

    # 按总盈亏排序
    results.sort(key=lambda r: r.total_pnl, reverse=True)

    return results


def print_grid_results(results: List[BacktestResult], top_n: int = 10):
    """打印网格搜索 Top N 结果"""
    print("\n" + "=" * 80)
    print(f"  🏆 参数网格搜索 Top {top_n}")
    print("=" * 80)
    print(f"  {'#':<3} {'PnL':>8} {'胜率':>6} {'盈亏比':>6} {'回撤':>6} {'连亏':>4} "
          f"{'TP1':>4} {'TP2':>4} {'止损':>4} {'RSI':>4} {'Drop':>5} {'单数':>4}")
    print("-" * 80)

    for i, r in enumerate(results[:top_n], 1):
        p = r.params
        print(
            f"  {i:<3} {r.total_pnl:>+7.1f}U {r.win_rate:>5.1f}% "
            f"{r.profit_loss_ratio:>5.2f}x {r.max_drawdown:>5.1f}% "
            f"{r.max_consecutive_losses:>3}次 "
            f"{p['tp1_pct']:>3.0f}% {p['tp2_pct']:>3.0f}% "
            f"{p['hard_stop_pct']:>3.0f}% {p['daily_rsi_min']:>3.0f} "
            f"{p['h4_rsi_drop']:>4.0f}  {r.total_trades:>3}"
        )

    print()
    if results:
        best = results[0]
        print(f"  🥇 最优参数:")
        print(f"     TP1={best.params['tp1_pct']}% | TP2={best.params['tp2_pct']}% | "
              f"止损={best.params['hard_stop_pct']}%")
        print(f"     RSI>{best.params['daily_rsi_min']} | 回落>{best.params['h4_rsi_drop']}点")
        print(f"     总盈亏: {best.total_pnl:+.1f}U | 胜率: {best.win_rate}% | "
              f"盈亏比: {best.profit_loss_ratio:.2f}")
    print()


# ══════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════

BACKTEST_RESULTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'backtest_results.json'
)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='影子做空策略回测引擎')
    parser.add_argument('--symbol', default='PEPE/USDT', help='回测币种 (默认: PEPE/USDT)')
    parser.add_argument('--days', type=int, default=90, help='回测天数 (默认: 90)')
    parser.add_argument('--grid', action='store_true', help='启用参数网格搜索')
    parser.add_argument('--symbols', nargs='+', help='多币种回测')

    args = parser.parse_args()

    symbols = args.symbols or [args.symbol]

    if args.grid:
        # 网格搜索
        print(f"\n🔍 参数网格搜索: {symbols[0]} / {args.days}天")
        results = grid_search(symbols[0], args.days)
        print_grid_results(results)

        # 保存结果
        if results:
            save_data = {
                'symbol': symbols[0],
                'days': args.days,
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'top_results': [r.to_dict() for r in results[:20]],
            }
            atomic_write_json(BACKTEST_RESULTS_FILE, save_data)
            logger.info(f"结果已保存到 {BACKTEST_RESULTS_FILE}")

    else:
        # 单次回测
        all_results = []
        for symbol in symbols:
            print(f"\n🎯 回测: {symbol} / {args.days}天")
            result = run_backtest(symbol, args.days)
            print_result(result)
            all_results.append(result)

        # 多币种汇总
        if len(all_results) > 1:
            total_trades = sum(r.total_trades for r in all_results)
            total_pnl = sum(r.total_pnl for r in all_results)
            total_wins = sum(r.wins for r in all_results)
            overall_wr = round(total_wins / total_trades * 100, 1) if total_trades else 0
            print(f"\n{'='*60}")
            print(f"  📊 多币种汇总: {len(symbols)} 个币种")
            print(f"  总交易: {total_trades} | 总盈亏: {total_pnl:+.2f}U | 胜率: {overall_wr}%")
            print(f"{'='*60}\n")

        # 保存
        save_data = {
            'symbols': symbols,
            'days': args.days,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'results': [r.to_dict() for r in all_results],
        }
        atomic_write_json(BACKTEST_RESULTS_FILE, save_data)
        logger.info(f"结果已保存到 {BACKTEST_RESULTS_FILE}")
