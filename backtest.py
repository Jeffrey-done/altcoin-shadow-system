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
from typing import List, Optional
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
    # M5: 新语义 — 从最高盈利回撤此比例触发（0.4 = 回撤 40%）
    trail_retrace_ratio: float = getattr(config, 'TRAIL_STOP_RETRACE_RATIO', 0.4)
    max_hold_bars: int = 24  # 24根1h K线 = 24小时
    leverage: int = config.LEVERAGE
    stake: float = config.DEFAULT_STAKE
    slippage_pct: float = config.BACKTEST_SLIPPAGE_PCT
    fee_pct: float = config.BACKTEST_FEE_PCT


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

    M7: 数据完整性校验 — 拉取完后检查时间戳断点
      - 1h K 线应严格每根间隔 3600000ms
      - 如果连续 3 根以上缺失（断点 ≥ 4 * interval），整段数据不可信，返回空列表
      - 只有零星 1-2 根断点（可能因为停盘/下架），则保留但 logger.warning
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

        # ── M7: 时间戳连续性校验 ──
        if len(all_ohlcv) >= 2:
            # 期望的 bar 毫秒间隔
            timeframe_ms = {
                '1m': 60_000, '5m': 300_000, '15m': 900_000, '1h': 3_600_000,
                '4h': 14_400_000, '1d': 86_400_000,
            }.get(timeframe, 3_600_000)

            gaps = []
            max_gap_bars = 0
            total_missing = 0
            for i in range(1, len(all_ohlcv)):
                delta = all_ohlcv[i][0] - all_ohlcv[i - 1][0]
                if delta > timeframe_ms * 1.5:
                    missing_bars = int(delta // timeframe_ms) - 1
                    total_missing += missing_bars
                    max_gap_bars = max(max_gap_bars, missing_bars)
                    gaps.append((all_ohlcv[i - 1][0], all_ohlcv[i][0], missing_bars))

            if max_gap_bars >= 4:
                # 严重断点（≥4 根） → 回测结果不可信，直接丢弃
                logger.warning(
                    f"  ⚠️ {symbol} 数据严重断点（最大连续缺失 {max_gap_bars} 根 K 线，"
                    f"共 {total_missing} 根缺失，{len(gaps)} 个断点），跳过该币种"
                )
                return []
            elif gaps:
                # 零星断点 → 警告但保留
                logger.warning(
                    f"  ⚠️ {symbol} 数据有 {len(gaps)} 个零星断点（共缺失 {total_missing} 根 K 线，"
                    f"最大连续 {max_gap_bars} 根），回测精度会略有偏差"
                )

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
        # 不在最后一根K线发信号（需要下一根bar作为入场价）
        if i + 1 >= len(rsi_series):
            continue

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
    用 entry_idx+1 的开盘价作为入场价（避免未来数据偏差）。
    用后续 K 线的 high/low 判断是否触发止盈/止损。
    应用滑点和手续费模拟真实执行环境。

    H9: bar-within 路径假设（避免总是假设最坏情况）：
      - 阳线（close >= open）：路径 open → low → high → close
        做空视角：先探底（TP 优先）、再冲顶（硬止损/移动止损在后）
      - 阴线（close < open）：路径 open → high → low → close
        做空视角：先冲顶（硬止损/移动止损优先）、再探底（TP 在后）

    同一根 K 线内如果同时满足 TP 和止损：
      按 bar 方向决定哪个先触发。相比原来总是先硬止损（悲观），
      这种路径假设对做空策略总体更公平（一半时间先打硬止损，
      一半时间先止盈），也更贴近实际。
    """
    # 使用下一根K线的开盘价作为入场价（修复 look-ahead bias）
    entry_price = klines[entry_idx + 1]['open']
    entry_time = klines[entry_idx + 1]['time']

    # 做空滑点：worse fill = higher entry for short（当前符号约定仅适用于做空交易）
    entry_price = entry_price * (1 + params.slippage_pct / 100)

    trade = BacktestTrade(
        symbol='',
        entry_price=entry_price,
        entry_time=entry_time,
    )

    # 计算关键价位（基于滑点后的入场价）
    tp1_price = entry_price * (1 - params.tp1_pct / 100)
    tp2_price = entry_price * (1 - params.tp2_pct / 100)
    hard_stop_price = entry_price * (1 + params.hard_stop_pct / 100)

    # 状态
    tp1_triggered = False
    best_pnl_pct = 0.0
    trail_stop_price = None
    stake_remaining_ratio = 1.0  # 剩余仓位比例

    notional = params.stake * params.leverage

    def _close_trade(exit_price_raw: float, exit_time: str, reason: str,
                     tp1_hit_flag: bool, bar_off: int) -> BacktestTrade:
        """统一的平仓计算函数"""
        exit_price = exit_price_raw * (1 + params.slippage_pct / 100)
        pnl_pct = (entry_price - exit_price) / entry_price * 100
        tp1_pnl = 0.0
        if tp1_hit_flag:
            tp1_pnl = notional * params.tp1_close_ratio * params.tp1_pct / 100
        remaining_pnl = notional * stake_remaining_ratio * pnl_pct / 100
        fee = notional * params.fee_pct / 100 * 2
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.pnl_pct = pnl_pct
        trade.pnl_usd = round(tp1_pnl + remaining_pnl - fee, 2)
        trade.exit_reason = reason
        trade.hold_bars = bar_off
        trade.tp1_hit = tp1_hit_flag
        return trade

    for bar_offset in range(1, params.max_hold_bars + 1):
        bar_idx = entry_idx + 1 + bar_offset
        if bar_idx >= len(klines):
            break

        bar = klines[bar_idx]
        bar_open = bar['open']
        bar_high = bar['high']
        bar_low = bar['low']
        bar_close = bar['close']

        # H9: bar 方向决定访问顺序
        #   阳线 (close >= open): open → low → high → close
        #   阴线 (close < open):  open → high → low → close
        bullish = bar_close >= bar_open

        # 提取本 bar 的触发事件按时间顺序排好
        # 每个事件: (阶段, 检查函数) 阶段 'low' 或 'high'
        # 对做空来说:
        #   low 阶段能触发: TP1 / TP2（价格跌到止盈）
        #   high 阶段能触发: 硬止损 / 移动止损（价格涨到止损）
        stages = ['low', 'high'] if bullish else ['high', 'low']

        exit_now = None  # (exit_price, reason)

        for stage in stages:
            if stage == 'high':
                # ── 硬止损 ──
                if bar_high >= hard_stop_price:
                    exit_now = (hard_stop_price, 'hard_stop')
                    break

                # ── 移动止损（同 bar 内，best_pnl_pct 可能已经在 'low' 阶段更新过）──
                if (trail_stop_price is not None
                    and bar_high >= trail_stop_price
                    and best_pnl_pct >= params.trail_activate_pct):
                    exit_now = (trail_stop_price, 'trail_stop')
                    break

            elif stage == 'low':
                # ── TP1 ──
                if not tp1_triggered and bar_low <= tp1_price:
                    tp1_triggered = True
                    stake_remaining_ratio = 1 - params.tp1_close_ratio
                    trade.tp1_hit = True
                    # TP1 不平仓，继续循环（同 bar 内可能还要触发 TP2）

                # ── TP2 ──
                if tp1_triggered and bar_low <= tp2_price:
                    exit_now = (tp2_price, 'tp2')
                    break

                # ── 更新 best_pnl_pct / 移动止损（本 bar 最好盈利）──
                current_pnl_pct = (entry_price - bar_low) / entry_price * 100
                if current_pnl_pct > best_pnl_pct:
                    best_pnl_pct = current_pnl_pct
                    if best_pnl_pct >= params.trail_activate_pct:
                        # M5: 相对回撤语义 — trigger = best * (1 - retrace_ratio)
                        trigger_pct = best_pnl_pct * (1 - params.trail_retrace_ratio)
                        trail_stop_price = entry_price * (1 - trigger_pct / 100)

        if exit_now is not None:
            return _close_trade(
                exit_now[0], bar['time'], exit_now[1],
                tp1_triggered, bar_offset,
            )

    # ── 时间止损（超时按收盘价平仓）──
    last_idx = min(entry_idx + 1 + params.max_hold_bars, len(klines) - 1)
    return _close_trade(
        klines[last_idx]['close'], klines[last_idx]['time'], 'time_stop',
        tp1_triggered, params.max_hold_bars,
    )



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
    # 过滤：确保 entry_idx+1 存在（需要下一根bar的open作为入场价）
    signals = [s for s in signals if s + 1 < len(klines)]
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


def calculate_monthly_breakdown(trades: List[BacktestTrade]) -> dict:
    """
    按月分解交易统计。
    返回: {
      "2024-01": {"trades": 5, "wins": 3, "pnl": 12.5, "win_rate": 60.0},
      "2024-02": {...},
      ...
    }
    """
    monthly = {}

    for t in trades:
        if not t.entry_time:
            continue
        # 提取月份 YYYY-MM
        month_key = t.entry_time[:7]
        if month_key not in monthly:
            monthly[month_key] = {
                "trades": 0, "wins": 0, "losses": 0,
                "pnl": 0.0, "win_rate": 0.0,
                "best_trade": 0.0, "worst_trade": 0.0,
                "tp1_hits": 0, "tp2_hits": 0,
            }
        m = monthly[month_key]
        m["trades"] += 1
        m["pnl"] += t.pnl_usd
        if t.pnl_usd > 0:
            m["wins"] += 1
        else:
            m["losses"] += 1
        if t.pnl_usd > m["best_trade"]:
            m["best_trade"] = t.pnl_usd
        if t.pnl_usd < m["worst_trade"]:
            m["worst_trade"] = t.pnl_usd
        if t.tp1_hit:
            m["tp1_hits"] += 1
        if t.exit_reason == 'tp2':
            m["tp2_hits"] += 1

    # 计算胜率
    for m in monthly.values():
        m["pnl"] = round(m["pnl"], 2)
        m["best_trade"] = round(m["best_trade"], 2)
        m["worst_trade"] = round(m["worst_trade"], 2)
        m["win_rate"] = round(m["wins"] / m["trades"] * 100, 1) if m["trades"] else 0

    return dict(sorted(monthly.items()))


def print_monthly_breakdown(trades: List[BacktestTrade], symbol: str = ''):
    """打印月度分解报告"""
    monthly = calculate_monthly_breakdown(trades)

    if not monthly:
        print("  无交易数据")
        return

    print(f"\n{'='*75}")
    print(f"  📅 月度分解报告 {symbol}")
    print(f"{'='*75}")
    print(f"  {'月份':<10} {'交易':>5} {'胜率':>7} {'盈亏':>10} {'最佳':>9} {'最差':>9} {'TP1':>4} {'TP2':>4}")
    print(f"  {'-'*68}")

    total_pnl = 0.0
    positive_months = 0
    negative_months = 0

    for month, data in monthly.items():
        total_pnl += data["pnl"]
        if data["pnl"] > 0:
            positive_months += 1
        elif data["pnl"] < 0:
            negative_months += 1

        pnl_emoji = "📈" if data["pnl"] > 0 else ("📉" if data["pnl"] < 0 else "➖")
        print(
            f"  {month:<10} {data['trades']:>4}  {data['win_rate']:>5.1f}%  "
            f"{pnl_emoji}{data['pnl']:>+8.2f}U  {data['best_trade']:>+7.2f}U  "
            f"{data['worst_trade']:>+7.2f}U  {data['tp1_hits']:>3}  {data['tp2_hits']:>3}"
        )

    print(f"  {'-'*68}")
    print(f"  {'合计':<10} {sum(d['trades'] for d in monthly.values()):>4}  "
          f"{'':>7} {total_pnl:>+9.2f}U")
    print(f"\n  盈利月份: {positive_months} | 亏损月份: {negative_months} | "
          f"月度胜率: {positive_months/(positive_months+negative_months)*100:.0f}%"
          if (positive_months + negative_months) > 0 else "")
    avg_monthly = total_pnl / len(monthly) if monthly else 0
    print(f"  月均盈亏: {avg_monthly:+.2f}U")
    print(f"{'='*75}\n")


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

    # 平仓原因分布
    if result.trades:
        from collections import Counter
        reasons = Counter(t.exit_reason for t in result.trades)
        print("\n  📊 平仓原因分布:")
        total = len(result.trades)
        for reason, count in reasons.most_common():
            pct = count / total * 100
            # 计算该原因的平均盈亏
            avg_pnl = sum(t.pnl_usd for t in result.trades if t.exit_reason == reason) / count
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            print(f"    {reason:<12} {count:>3}笔 ({pct:>5.1f}%) [{bar}] 均盈亏:{avg_pnl:>+.1f}U")

    print()

    # 实盘建议
    if result.win_rate >= 50 and result.profit_loss_ratio >= 1.5:
        print("  ✅ 参数达标！可以考虑小仓实盘验证")
    elif result.win_rate >= 40 and result.profit_loss_ratio >= 2.0:
        print("  ⚠️ 胜率偏低但盈亏比高，可以尝试但要严格执行止损")
    else:
        print("  ❌ 参数不佳，建议调优后重新回测")
    print()

    # 月度分解
    if result.trades:
        symbol = result.trades[0].symbol if result.trades[0].symbol else ''
        print_monthly_breakdown(result.trades, symbol)



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
        print("  🥇 最优参数:")
        print(f"     TP1={best.params['tp1_pct']}% | TP2={best.params['tp2_pct']}% | "
              f"止损={best.params['hard_stop_pct']}%")
        print(f"     RSI>{best.params['daily_rsi_min']} | 回落>{best.params['h4_rsi_drop']}点")
        print(f"     总盈亏: {best.total_pnl:+.1f}U | 胜率: {best.win_rate}% | "
              f"盈亏比: {best.profit_loss_ratio:.2f}")
    print()


# ══════════════════════════════════════════════════════════════════
#  批量回测 & 相关性分析
# ══════════════════════════════════════════════════════════════════

def run_batch_backtest(symbols: List[str], days: int = 90,
                       params: Optional[BacktestParams] = None) -> List[BacktestResult]:
    """对多个币种执行批量回测，返回每个币种的 BacktestResult"""
    if params is None:
        params = BacktestParams()

    results = []
    for symbol in symbols:
        logger.info(f"批量回测: {symbol} / {days}天")
        result = run_backtest(symbol, days, params)
        results.append(result)

    return results


def calculate_correlation_matrix(results: List[BacktestResult]) -> dict:
    """
    计算币种间权益曲线的 Pearson 相关系数。
    返回 dict: {(symbol_a, symbol_b): correlation_value}
    仅使用标准库 statistics 模块。
    """
    import statistics

    # 提取每个币种的权益曲线及对应 symbol
    curves = []
    for r in results:
        if r.trades and r.equity_curve:
            symbol = r.trades[0].symbol if r.trades else 'UNKNOWN'
            curves.append((symbol, r.equity_curve))

    correlation = {}

    for i in range(len(curves)):
        for j in range(i + 1, len(curves)):
            sym_a, curve_a = curves[i]
            sym_b, curve_b = curves[j]

            # 对齐长度（取较短的）
            min_len = min(len(curve_a), len(curve_b))
            if min_len < 3:
                correlation[(sym_a, sym_b)] = 0.0
                continue

            x = curve_a[:min_len]
            y = curve_b[:min_len]

            # Pearson 相关系数计算
            n = min_len
            mean_x = statistics.mean(x)
            mean_y = statistics.mean(y)

            numerator = sum((x[k] - mean_x) * (y[k] - mean_y) for k in range(n))
            denom_x = sum((x[k] - mean_x) ** 2 for k in range(n)) ** 0.5
            denom_y = sum((y[k] - mean_y) ** 2 for k in range(n)) ** 0.5

            if denom_x == 0 or denom_y == 0:
                correlation[(sym_a, sym_b)] = 0.0
            else:
                corr = numerator / (denom_x * denom_y)
                correlation[(sym_a, sym_b)] = round(corr, 4)

    return correlation


def rank_coins(results: List[BacktestResult]) -> List[dict]:
    """
    按复合评分对币种排名。
    评分公式: 0.4*win_rate/100 + 0.3*profit_loss_ratio/5 + 0.2*(1-max_drawdown/100) + 0.1*sharpe_ratio/3
    返回排序后的 list of dicts。
    """
    rankings = []
    for r in results:
        symbol = r.trades[0].symbol if r.trades else 'UNKNOWN'
        score = (
            0.4 * (r.win_rate / 100)
            + 0.3 * (min(r.profit_loss_ratio, 5) / 5)
            + 0.2 * (1 - r.max_drawdown / 100)
            + 0.1 * (min(r.sharpe_ratio, 3) / 3)
        )
        rankings.append({
            'symbol': symbol,
            'score': round(score, 4),
            'win_rate': r.win_rate,
            'profit_loss_ratio': r.profit_loss_ratio,
            'max_drawdown': r.max_drawdown,
            'sharpe_ratio': r.sharpe_ratio,
            'total_pnl': r.total_pnl,
            'total_trades': r.total_trades,
        })

    rankings.sort(key=lambda x: x['score'], reverse=True)
    return rankings


def generate_batch_report(results: List[BacktestResult], correlation: dict,
                          rankings: List[dict]) -> dict:
    """
    生成批量回测综合报告。
    返回包含 summary, per_coin_results, correlation_matrix, rankings,
    recommended_portfolio 的 dict。
    """
    # 汇总统计
    total_trades = sum(r.total_trades for r in results)
    total_wins = sum(r.wins for r in results)
    total_pnl = round(sum(r.total_pnl for r in results), 2)
    overall_win_rate = round(total_wins / total_trades * 100, 1) if total_trades else 0

    summary = {
        'total_coins': len(results),
        'total_trades': total_trades,
        'overall_win_rate': overall_win_rate,
        'total_pnl': total_pnl,
    }

    # 每币结果
    per_coin_results = []
    for r in results:
        symbol = r.trades[0].symbol if r.trades else 'UNKNOWN'
        per_coin_results.append({
            'symbol': symbol,
            'total_trades': r.total_trades,
            'win_rate': r.win_rate,
            'profit_loss_ratio': r.profit_loss_ratio,
            'total_pnl': r.total_pnl,
            'max_drawdown': r.max_drawdown,
            'sharpe_ratio': r.sharpe_ratio,
        })

    # 相关性矩阵（仅高于阈值的对）
    threshold = config.BATCH_CORRELATION_THRESHOLD
    corr_serializable = {}
    high_corr_pairs = {}
    for (sym_a, sym_b), val in correlation.items():
        corr_serializable[f"{sym_a}|{sym_b}"] = val
        if abs(val) >= threshold:
            high_corr_pairs[f"{sym_a}|{sym_b}"] = val

    # 推荐组合：选前N名且彼此相关性低于阈值的币
    recommended = []
    for coin in rankings:
        symbol = coin['symbol']
        # 检查与已选币的相关性
        conflict = False
        for selected in recommended:
            pair_key_1 = (symbol, selected['symbol'])
            pair_key_2 = (selected['symbol'], symbol)
            corr_val = correlation.get(pair_key_1, correlation.get(pair_key_2, 0))
            if abs(corr_val) >= threshold:
                conflict = True
                break
        if not conflict:
            recommended.append(coin)
        if len(recommended) >= 5:
            break

    report = {
        'summary': summary,
        'per_coin_results': per_coin_results,
        'correlation_matrix': corr_serializable,
        'high_correlation_pairs': high_corr_pairs,
        'rankings': rankings,
        'recommended_portfolio': recommended,
    }

    return report


def print_batch_report(report: dict, results: List[BacktestResult] = None):
    """打印批量回测报告"""
    summary = report['summary']
    per_coin = report['per_coin_results']
    high_corr = report.get('high_correlation_pairs', {})
    rankings = report['rankings']
    recommended = report['recommended_portfolio']

    print("\n" + "=" * 70)
    print("  📊 批量回测报告")
    print("=" * 70)
    print(f"  币种数量: {summary['total_coins']}")
    print(f"  总交易数: {summary['total_trades']}")
    print(f"  整体胜率: {summary['overall_win_rate']}%")
    print(f"  总盈亏: {summary['total_pnl']:+.2f}U")
    print("-" * 70)

    # 每币明细表
    print("\n  📋 各币种表现:")
    print(f"  {'币种':<14} {'交易数':>6} {'胜率':>6} {'盈亏比':>6} {'盈亏':>9} {'回撤':>6} {'排名':>4}")
    print("  " + "-" * 58)
    for coin in per_coin:
        # 找到排名
        rank_idx = next((i for i, r in enumerate(rankings) if r['symbol'] == coin['symbol']), -1)
        rank_str = str(rank_idx + 1) if rank_idx >= 0 else '-'
        print(f"  {coin['symbol']:<14} {coin['total_trades']:>5} "
              f"{coin['win_rate']:>5.1f}% {coin['profit_loss_ratio']:>5.2f}x "
              f"{coin['total_pnl']:>+8.2f}U {coin['max_drawdown']:>5.1f}% {rank_str:>4}")

    # 高相关性警告
    if high_corr:
        print(f"\n  ⚠️  高相关性币对 (>{config.BATCH_CORRELATION_THRESHOLD}):")
        for pair, val in high_corr.items():
            sym_a, sym_b = pair.split('|')
            print(f"    {sym_a} <-> {sym_b}: {val:.4f}")

    # 推荐组合
    print("\n  🏆 推荐组合 (低相关性 Top 币种):")
    for i, coin in enumerate(recommended, 1):
        print(f"    {i}. {coin['symbol']} (评分: {coin['score']:.4f}, "
              f"胜率: {coin['win_rate']}%, 盈亏比: {coin['profit_loss_ratio']:.2f})")

    # 月度分解（汇总所有币种的交易）
    if results:
        all_trades = []
        for r in results:
            all_trades.extend(r.trades)
        if all_trades:
            # 平仓原因分布（全币种汇总）
            from collections import Counter
            reasons = Counter(t.exit_reason for t in all_trades)
            total = len(all_trades)
            print(f"\n  📊 平仓原因分布（全币种 {total} 笔）:")
            for reason, count in reasons.most_common():
                pct = count / total * 100
                avg_pnl = sum(t.pnl_usd for t in all_trades if t.exit_reason == reason) / count
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                print(f"    {reason:<12} {count:>3}笔 ({pct:>5.1f}%) [{bar}] 均盈亏:{avg_pnl:>+.1f}U")

            print_monthly_breakdown(all_trades, "全币种汇总")

    print("\n" + "=" * 70 + "\n")

BACKTEST_RESULTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'backtest_results.json'
)

BATCH_BACKTEST_RESULTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'batch_backtest_results.json'
)


# ══════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='影子做空策略回测引擎')
    parser.add_argument('--symbol', default='PEPE/USDT', help='回测币种 (默认: PEPE/USDT)')
    parser.add_argument('--days', type=int, default=90, help='回测天数 (默认: 90)')
    parser.add_argument('--grid', action='store_true', help='启用参数网格搜索')
    parser.add_argument('--batch', action='store_true', help='批量回测所有配置币种')
    parser.add_argument('--monthly', action='store_true', help='仅输出月度分解（需先有回测数据）')
    parser.add_argument('--symbols', nargs='+', help='多币种回测')

    args = parser.parse_args()

    symbols = args.symbols or [args.symbol]

    if args.monthly:
        # 仅输出月度分解（对指定币种跑回测后只显示月度）
        print(f"\n📅 月度分解回测: {symbols} / {args.days}天")
        for symbol in symbols:
            result = run_backtest(symbol, args.days)
            if result.trades:
                print_monthly_breakdown(result.trades, symbol)
            else:
                print(f"  {symbol}: 无交易数据")

    elif args.batch:
        # 批量回测
        batch_symbols = config.BATCH_BACKTEST_SYMBOLS
        batch_days = args.days if args.days != 90 else config.BATCH_BACKTEST_DAYS
        print(f"\n🚀 批量回测: {len(batch_symbols)} 个币种 / {batch_days}天")

        results = run_batch_backtest(batch_symbols, batch_days)
        correlation = calculate_correlation_matrix(results)
        rankings = rank_coins(results)
        report = generate_batch_report(results, correlation, rankings)

        # 打印报告
        print_batch_report(report, results)

        # 保存结果（含参数快照，面板可对比是否过期）
        save_data = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'days': batch_days,
            'report': report,
            'config_snapshot': {
                'daily_rsi_min': config.DAILY_RSI_MIN,
                'tp1_pct': round((1 - config.TP1_MULTIPLIER) * 100, 2),
                'tp2_pct': round((1 - config.TP2_MULTIPLIER) * 100, 2),
                'hard_stop_pct': config.HARD_STOP_LOSS_PCT,
                'batch_symbols': config.BATCH_BACKTEST_SYMBOLS,
            },
        }
        atomic_write_json(BATCH_BACKTEST_RESULTS_FILE, save_data)
        logger.info(f"批量回测结果已保存到 {BATCH_BACKTEST_RESULTS_FILE}")

    elif args.grid:
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

        # 保存（含参数快照）
        save_data = {
            'symbols': symbols,
            'days': args.days,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'results': [r.to_dict() for r in all_results],
            'config_snapshot': {
                'daily_rsi_min': config.DAILY_RSI_MIN,
                'tp1_pct': round((1 - config.TP1_MULTIPLIER) * 100, 2),
                'tp2_pct': round((1 - config.TP2_MULTIPLIER) * 100, 2),
                'hard_stop_pct': config.HARD_STOP_LOSS_PCT,
                'h4_rsi_drop': config.H4_RSI_DROP,
                'leverage': config.LEVERAGE,
            },
        }
        atomic_write_json(BACKTEST_RESULTS_FILE, save_data)
        logger.info(f"结果已保存到 {BACKTEST_RESULTS_FILE}")
