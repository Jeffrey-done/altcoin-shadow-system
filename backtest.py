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
from signal_score import calculate_signal_score

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
    stake: float = config.DEFAULT_STAKE   # 基础保证金（开仓时若启用复利会按状态调整）
    slippage_pct: float = config.BACKTEST_SLIPPAGE_PCT
    fee_pct: float = config.BACKTEST_FEE_PCT
    # M-3 修复：资金费率持仓成本（做空 → 正费率收钱、负费率付钱；按 8h 周期扣算）
    # 单位：%/8h（与交易所 funding rate 单位一致）
    # 默认 0.01%/8h ≈ 行业平均；保守起见在回测里把做空者支付的费率成本计入。
    # 调用方可通过传入实际历史 funding rate 进一步精确化。
    funding_rate_pct: float = 0.01

    # ══════════════════════════════════════════════════════════════════
    #  M-3 完整版：事件驱动主循环参数（2026-05）
    # ══════════════════════════════════════════════════════════════════
    # 启用后 run_backtest 走 run_backtest_event_driven，逐 bar 推进状态：
    #   - 复利仓位（按当前已实现盈亏动态调整 stake）
    #   - BTC 暴跌时跳过新开仓（与实盘 signal_score.check_btc_filter 同款逻辑）
    #   - 单日亏损 / 单日开仓 / 连亏暂停 / 同币冷却（与实盘 risk_control 对齐）
    #   - 资金费率成本（与旧版 _close_trade 内置算法相同，事件驱动里按 bar 累计）
    # 关闭则走 v1 独立交易模式（旧测试 / grid_search 仍走 v1）。
    use_event_driven_engine: bool = True

    # ── 账户与复利 ──
    account_balance: float = float(getattr(config, 'ACCOUNT_BALANCE', 100))
    compound_enabled: bool = bool(getattr(config, 'AUTO_COMPOUND_ENABLED', True))
    compound_step: float = float(getattr(config, 'COMPOUND_STEP', 50))
    compound_increase: float = float(getattr(config, 'COMPOUND_INCREASE', 25))
    compound_max_stake: float = float(getattr(config, 'COMPOUND_MAX_STAKE', 300))

    # ── BTC 过滤 ──
    btc_filter_enabled: bool = bool(getattr(config, 'BTC_FILTER_ENABLED', True))
    btc_crash_threshold: float = float(getattr(config, 'BTC_CRASH_THRESHOLD', -5.0))
    btc_pump_threshold: float = float(getattr(config, 'BTC_PUMP_THRESHOLD', 8.0))
    btc_symbol: str = 'BTC/USDT'

    # ── 风控（与 risk_control 对齐）──
    max_daily_loss: float = float(getattr(config, 'RISK_MAX_DAILY_LOSS', 30))
    max_daily_trades: int = int(getattr(config, 'RISK_MAX_DAILY_TRADES', 3))
    consecutive_loss_pause: int = int(getattr(config, 'RISK_CONSECUTIVE_LOSS_PAUSE', 3))
    pause_hours: int = int(getattr(config, 'RISK_PAUSE_HOURS', 24))
    max_position_pct: float = float(getattr(config, 'RISK_MAX_POSITION_PCT', 0.5))
    cooldown_hours: int = int(getattr(config, 'COOLDOWN_HOURS', 24))


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
#  量价背离检测（回测版：直接用 K 线数据，不调 API）
# ══════════════════════════════════════════════════════════════════

def detect_volume_divergence_backtest(klines: List[dict], signal_idx: int,
                                      vol_div_bonus_override: Optional[dict] = None) -> dict:
    """
    回测版量价背离检测：用信号点前24根1H K线检测。
    逻辑与 altcoin_scanner.py 中 detect_volume_divergence 完全一致。

    参数:
      klines: 完整K线列表
      signal_idx: 信号触发的K线索引
      vol_div_bonus_override: 可选，覆盖加分值 {"strong": 12, "medium": 8, "mild": 6}

    返回:
      {"divergence": bool, "shrink_ratio": float, "score_bonus": int, "reason": str}
    """
    # 默认加分值（原始配置）
    bonus_strong = 8   # 缩量60%+
    bonus_medium = 5   # 缩量45%+
    bonus_mild = 3     # 缩量30%+

    if vol_div_bonus_override:
        bonus_strong = vol_div_bonus_override.get("strong", bonus_strong)
        bonus_medium = vol_div_bonus_override.get("medium", bonus_medium)
        bonus_mild = vol_div_bonus_override.get("mild", bonus_mild)

    result = {"divergence": False, "shrink_ratio": 1.0, "score_bonus": 0, "reason": ""}

    # 取信号点前24根K线（丢弃最后一根未收盘）
    end_idx = signal_idx  # 信号触发时这根已收盘
    start_idx = max(0, end_idx - 23)  # 24根

    if end_idx - start_idx < 10:
        return result

    segment = klines[start_idx:end_idx + 1]

    highs = [k['high'] for k in segment]
    volumes = [k['volume'] for k in segment]

    # 找局部高点（高于前后2根K线的high）
    peaks = []  # [(index, high_price, volume)]
    for i in range(2, len(highs) - 2):
        if (highs[i] >= highs[i-1] and highs[i] >= highs[i-2]
                and highs[i] >= highs[i+1] and highs[i] >= highs[i+2]):
            peaks.append((i, highs[i], volumes[i]))

    if len(peaks) < 2:
        return result

    # 比较最近两个高点
    prev_peak = peaks[-2]
    last_peak = peaks[-1]

    prev_price, prev_vol = prev_peak[1], prev_peak[2]
    last_price, last_vol = last_peak[1], last_peak[2]

    # 价格创新高或持平（差距<1%）
    price_higher = last_price >= prev_price * 0.99

    if not price_higher:
        return result

    # 成交量缩减检查
    if prev_vol <= 0:
        return result

    vol_ratio = last_vol / prev_vol  # <1 表示缩量

    if vol_ratio < 0.70:
        # 量价背离成立
        shrink_pct = round((1 - vol_ratio) * 100, 0)
        result["divergence"] = True
        result["shrink_ratio"] = round(vol_ratio, 2)

        if vol_ratio < 0.40:
            result["score_bonus"] = bonus_strong   # 缩量60%+
        elif vol_ratio < 0.55:
            result["score_bonus"] = bonus_medium   # 缩量45%+
        else:
            result["score_bonus"] = bonus_mild     # 缩量30%+

        result["reason"] = f"量价背离：价格新高但成交量缩{shrink_pct:.0f}%"

    return result


def score_filter_signal(klines: List[dict], rsi_series: List[float],
                        signal_idx: int, params: 'BacktestParams',
                        vol_div_bonus_override: Optional[dict] = None,
                        score_threshold: int = None) -> dict:
    """
    对回测中的一个信号做评分过滤。

    参数:
      klines: 完整K线列表
      rsi_series: 完整RSI序列
      signal_idx: 信号触发的K线索引
      params: 回测参数
      vol_div_bonus_override: 量价背离加分覆盖
      score_threshold: 评分阈值（低于此分跳过），默认用 config.SCORE_HALF_THRESHOLD

    返回:
      {"passed": bool, "score": int, "grade": str, "stake": float,
       "vol_divergence": dict, "details": dict}
    """
    if score_threshold is None:
        score_threshold = config.SCORE_HALF_THRESHOLD

    # ── 从K线上下文中提取评分所需参数 ──

    # RSI 相关
    current_rsi = rsi_series[signal_idx]
    lookback = 20
    peak_start = max(0, signal_idx - lookback)
    rsi_peak = max(rsi_series[peak_start:signal_idx])

    # 用 RSI peak 近似 rsi_1d（因为回测中 1H RSI peak ≈ 日线超买）
    rsi_1d_approx = rsi_peak
    rsi_4h_approx = current_rsi
    rsi_4h_peak_approx = rsi_peak

    # 24h 涨跌幅（用24根前的收盘价 vs 当前）
    pct_24h = 0.0
    if signal_idx >= 24:
        old_close = klines[signal_idx - 24]['close']
        cur_close = klines[signal_idx]['close']
        if old_close > 0:
            pct_24h = (cur_close - old_close) / old_close * 100

    # 量价背离检测
    vol_div = detect_volume_divergence_backtest(klines, signal_idx, vol_div_bonus_override)

    # 调用 signal_score 评分（回测中无法获取 OI / funding / yao_score，使用保守默认值）
    score_result = calculate_signal_score(
        rsi_1d=rsi_1d_approx,
        rsi_4h=rsi_4h_approx,
        rsi_4h_peak=rsi_4h_peak_approx,
        pct_24h=pct_24h,
        oi_change=0.0,          # 回测中无 OI 数据，保守给0
        funding_rate=0.01,      # 假设中性费率
        yao_score=0,            # 回测中无妖币判定
        trigger_type='4h_rsi',  # 回测信号均为 RSI 回落触发
        abandon_oi_declining=False,
        btc_24h_pct=0.0,        # 回测中无 BTC 数据，中性
        cross_validate_bonus=0, # 回测中无 OKX 交叉验证
        vol_divergence_bonus=vol_div.get("score_bonus", 0),
    )

    passed = score_result["score"] >= score_threshold

    return {
        "passed": passed,
        "score": score_result["score"],
        "grade": score_result["grade"],
        "stake": score_result["stake"],
        "vol_divergence": vol_div,
        "details": score_result["details"],
    }



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
        from exchange_manager import make_exchange
        exchange = make_exchange('binance')

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
        # M-3 修复：资金费率持仓成本
        # 做空 + 正 funding rate → 多头付空头 → 收益（不扣费）
        # 做空 + 负 funding rate → 空头付多头 → 亏损（扣费）
        # 此处保守地按"做空总是支付费率"假设（与做空策略下负费率/正费率
        # 概率分布对策略不利方向保守计算）
        # 持仓时长 ≈ bar_off 小时；每 8h 计一次费率
        funding_periods = bar_off / 8.0
        funding_cost = abs(notional) * (params.funding_rate_pct / 100) * funding_periods
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.pnl_pct = pnl_pct
        trade.pnl_usd = round(tp1_pnl + remaining_pnl - fee - funding_cost, 2)
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
#  M-3 完整版：事件驱动主循环
# ══════════════════════════════════════════════════════════════════
#
# 旧 simulate_trade 把每笔交易当独立计算，无法在交易间携带状态。
# 新引擎以"bar 时间步"为基本单位，每根 K 线先推进开放仓位、再决定是否
# 触发新开仓。开仓时基于 BacktestState 当前的：
#   - realized_pnl  → 复利仓位（compute_compound_stake）
#   - daily_loss / daily_trades_opened → 风控限频
#   - consecutive_losses / paused_until → 连亏暂停
#   - cooldown_until_ms[symbol] → 同币冷却期
#   - total_open_stake → 持仓占比（防止 stake 总和超过 balance × max_position_pct）
#   - btc_index 当前 24h 涨跌幅 → BTC 暴跌过滤
# 这些是实盘 risk_control + signal_score + common.get_compound_stake 的对应物，
# 让回测口径与实盘对齐（解决 M-3 audit 问题）。
# ══════════════════════════════════════════════════════════════════


@dataclass
class _OpenTrade:
    """事件驱动主循环内的"在仓"交易，可逐 bar 演化状态"""
    symbol: str
    entry_idx: int            # 信号 bar 的索引（开仓 bar 是 entry_idx + 1）
    entry_price: float        # 含滑点
    entry_time: str
    stake: float              # 该笔保证金（开仓时由复利决定）
    leverage: int
    notional: float
    tp1_price: float
    tp2_price: float
    hard_stop_price: float
    max_hold_bars: int
    # 演化状态
    tp1_triggered: bool = False
    stake_remaining_ratio: float = 1.0
    best_pnl_pct: float = 0.0
    trail_stop_price: Optional[float] = None
    bars_held: int = 0


@dataclass
class _BacktestState:
    """事件驱动主循环的全局状态"""
    initial_balance: float
    realized_pnl: float = 0.0

    # 风控
    daily_loss: float = 0.0
    daily_trades_opened: int = 0
    last_day: str = ''
    consecutive_losses: int = 0
    paused_until_ms: int = 0
    # 同币冷却到期时间（毫秒），仅在止损平仓时设置
    cooldown_until_ms: dict = field(default_factory=dict)

    # 持仓
    open_trades: List[_OpenTrade] = field(default_factory=list)
    closed_trades: List[BacktestTrade] = field(default_factory=list)

    # 调试统计：每个跳过原因被命中多少次（便于审计回测口径与实盘是否对齐）
    skip_counters: dict = field(default_factory=dict)

    @property
    def current_equity(self) -> float:
        """当前净值 = 初始 + 已实现盈亏（不含浮动）"""
        return self.initial_balance + self.realized_pnl

    @property
    def total_open_stake(self) -> float:
        """所有持仓的剩余保证金合计"""
        return sum(t.stake * t.stake_remaining_ratio for t in self.open_trades)

    def bump_skip(self, reason: str) -> None:
        self.skip_counters[reason] = self.skip_counters.get(reason, 0) + 1


# ── 时间工具 ──────────────────────────────────────────────────────

def _iso_to_ms(iso_str: str) -> int:
    """ISO 时间字符串 → unix 毫秒"""
    try:
        dt = datetime.fromisoformat(iso_str)
    except Exception:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ── BTC 24h 涨跌幅索引 ────────────────────────────────────────────
# 启动时一次性加载 BTC/USDT 1h K 线，按 bar 时间戳建索引；查询某 bar 时
# 用 close[t] vs close[t-24h] 计算 24h 涨跌幅。

def load_btc_index(days: int, symbol: str = 'BTC/USDT') -> dict:
    """
    加载 BTC 1h K 线并建索引。
    返回 {time_ms: {'close': float, 'pct_24h': float or None}}
    pct_24h 在前 24 根 K 线为 None（缺前 24h 数据）。

    无法获取（网络/API 失败）时返回空 dict，调用方应跳过 BTC 过滤。
    """
    klines = load_cached_klines(symbol, '1h', days)
    if not klines:
        logger.warning(f"BTC 数据获取失败，BTC 过滤将被跳过")
        return {}

    # 按时间戳排序后建索引
    sorted_kl = sorted(klines, key=lambda k: k['time'])
    index = {}
    closes = [k['close'] for k in sorted_kl]
    times_ms = [_iso_to_ms(k['time']) for k in sorted_kl]
    for i, k in enumerate(sorted_kl):
        ts_ms = times_ms[i]
        if i >= 24 and closes[i - 24] > 0:
            pct_24h = (closes[i] - closes[i - 24]) / closes[i - 24] * 100
        else:
            pct_24h = None
        index[ts_ms] = {'close': closes[i], 'pct_24h': pct_24h}
    logger.info(f"BTC 索引已加载: {len(index)} 个 bar，覆盖 {days} 天")
    return index


def get_btc_24h_change_at(btc_index: dict, bar_time_ms: int,
                          tolerance_ms: int = 3_600_000) -> Optional[float]:
    """
    在 BTC 索引中查询给定时间点的 24h 涨跌幅。
    允许 tolerance_ms 容差（默认 1h），找最近的 BTC bar。
    BTC index 为空 / 找不到匹配 bar 时返回 None（调用方应放行）。
    """
    if not btc_index:
        return None
    # 直接命中
    if bar_time_ms in btc_index:
        return btc_index[bar_time_ms]['pct_24h']
    # 容差搜索：找最接近的 BTC bar
    closest_ts = min(btc_index.keys(), key=lambda t: abs(t - bar_time_ms))
    if abs(closest_ts - bar_time_ms) <= tolerance_ms:
        return btc_index[closest_ts]['pct_24h']
    return None


# ── 复利仓位 ──────────────────────────────────────────────────────

def compute_compound_stake(state: _BacktestState, params: BacktestParams) -> float:
    """
    根据当前已实现盈亏计算"应当用的 stake"。
    与 common.get_compound_stake 同款平滑线性公式。

    亏损或持平 → 返回基础 stake
    盈利 → stake = base + (realized_pnl / step) * increase，封顶 max_stake
    """
    base = float(params.stake)
    if not params.compound_enabled or state.realized_pnl <= 0:
        return base
    step = max(params.compound_step, 1)
    ratio = state.realized_pnl / step
    s = base + ratio * params.compound_increase
    s = min(s, params.compound_max_stake)
    return round(s)


# ── 风控网关（与 risk_control.can_open_trade 对齐）─────────────────

def _check_risk_gates(state: _BacktestState, params: BacktestParams,
                      bar_time_ms: int, symbol: str,
                      proposed_stake: float) -> tuple:
    """
    返回 (allowed: bool, reason: str)。
    与实盘 risk_control.can_open_trade 检查项对齐：
      1. 暂停期（连亏后 paused_until）
      2. 单日亏损上限
      3. 单日开仓次数上限
      4. 同币冷却期
      5. 持仓占比上限
    """
    if state.paused_until_ms > bar_time_ms:
        return False, 'pause'
    if state.daily_loss >= params.max_daily_loss:
        return False, 'daily_loss_limit'
    if state.daily_trades_opened >= params.max_daily_trades:
        return False, 'daily_trades_limit'
    cd = state.cooldown_until_ms.get(symbol, 0)
    if cd > bar_time_ms:
        return False, 'cooldown'
    max_position = state.current_equity * params.max_position_pct
    if state.total_open_stake + proposed_stake > max_position:
        return False, 'position_pct'
    return True, ''


# ── 单 bar 事件循环：推进开放仓位 ──────────────────────────────────

def _step_open_trade(ot: _OpenTrade, bar: dict,
                     params: BacktestParams) -> Optional[tuple]:
    """
    将一笔开放仓位推进到下一根 bar（即"消费这根 K 线"），返回平仓事件
    `(exit_price, reason)` 若触发，否则 None（继续持仓）。

    路径假设与旧 simulate_trade 完全一致（H9）：
      阳线 (close >= open): open → low → high → close
      阴线 (close < open):  open → high → low → close
    做空策略下：low 阶段触发 TP1/TP2/更新 best_pnl，high 阶段触发硬止损/移动止损。
    """
    bar_open = bar['open']
    bar_high = bar['high']
    bar_low = bar['low']
    bar_close = bar['close']

    # 推进 bar 计数
    ot.bars_held += 1

    bullish = bar_close >= bar_open
    stages = ['low', 'high'] if bullish else ['high', 'low']

    for stage in stages:
        if stage == 'high':
            # 硬止损
            if bar_high >= ot.hard_stop_price:
                return (ot.hard_stop_price, 'hard_stop')
            # 移动止损
            if (ot.trail_stop_price is not None
                and bar_high >= ot.trail_stop_price
                and ot.best_pnl_pct >= params.trail_activate_pct):
                return (ot.trail_stop_price, 'trail_stop')
        else:  # low
            # TP1
            if not ot.tp1_triggered and bar_low <= ot.tp1_price:
                ot.tp1_triggered = True
                ot.stake_remaining_ratio = 1 - params.tp1_close_ratio
            # TP2
            if ot.tp1_triggered and bar_low <= ot.tp2_price:
                return (ot.tp2_price, 'tp2')
            # 更新 best_pnl_pct + trail
            current_pnl_pct = (ot.entry_price - bar_low) / ot.entry_price * 100
            if current_pnl_pct > ot.best_pnl_pct:
                ot.best_pnl_pct = current_pnl_pct
                if ot.best_pnl_pct >= params.trail_activate_pct:
                    trigger_pct = ot.best_pnl_pct * (1 - params.trail_retrace_ratio)
                    ot.trail_stop_price = ot.entry_price * (1 - trigger_pct / 100)

    # 时间止损
    if ot.bars_held >= ot.max_hold_bars:
        return (bar_close, 'time_stop')

    return None


def _finalize_open_trade(ot: _OpenTrade, exit_price_raw: float, exit_time: str,
                         reason: str, params: BacktestParams) -> BacktestTrade:
    """
    把 _OpenTrade + 平仓事件 → BacktestTrade（用与 simulate_trade._close_trade 完全
    一致的盈亏 + 滑点 + 手续费 + 资金费率公式，保证两个引擎数值口径对齐）。
    """
    # 滑点：做空平仓滑点 = 实际买回价更高
    exit_price = exit_price_raw * (1 + params.slippage_pct / 100)
    pnl_pct = (ot.entry_price - exit_price) / ot.entry_price * 100

    tp1_pnl = 0.0
    if ot.tp1_triggered:
        tp1_pnl = ot.notional * params.tp1_close_ratio * params.tp1_pct / 100
    remaining_pnl = ot.notional * ot.stake_remaining_ratio * pnl_pct / 100
    fee = ot.notional * params.fee_pct / 100 * 2

    # 资金费率持仓成本（与旧版同算法）
    funding_periods = ot.bars_held / 8.0
    funding_cost = abs(ot.notional) * (params.funding_rate_pct / 100) * funding_periods

    pnl_usd = round(tp1_pnl + remaining_pnl - fee - funding_cost, 2)

    return BacktestTrade(
        symbol=ot.symbol,
        entry_price=ot.entry_price,
        entry_time=ot.entry_time,
        exit_price=exit_price,
        exit_time=exit_time,
        pnl_pct=pnl_pct,
        pnl_usd=pnl_usd,
        exit_reason=reason,
        hold_bars=ot.bars_held,
        tp1_hit=ot.tp1_triggered,
    )


def _open_position(klines: List[dict], entry_idx: int, symbol: str,
                   stake: float, params: BacktestParams) -> Optional[_OpenTrade]:
    """信号触发 → 用 entry_idx+1 的 open 价开仓，返回 _OpenTrade（不含模拟）"""
    if entry_idx + 1 >= len(klines):
        return None
    raw_entry = klines[entry_idx + 1]['open']
    # 做空开仓滑点：成交价更高
    entry_price = raw_entry * (1 + params.slippage_pct / 100)
    notional = stake * params.leverage
    return _OpenTrade(
        symbol=symbol,
        entry_idx=entry_idx,
        entry_price=entry_price,
        entry_time=klines[entry_idx + 1]['time'],
        stake=stake,
        leverage=params.leverage,
        notional=notional,
        tp1_price=entry_price * (1 - params.tp1_pct / 100),
        tp2_price=entry_price * (1 - params.tp2_pct / 100),
        hard_stop_price=entry_price * (1 + params.hard_stop_pct / 100),
        max_hold_bars=params.max_hold_bars,
    )


# ══════════════════════════════════════════════════════════════════
#  事件驱动主循环
# ══════════════════════════════════════════════════════════════════

def run_backtest_event_driven(klines: List[dict], symbol: str,
                              params: BacktestParams,
                              btc_index: Optional[dict] = None,
                              signals: Optional[List[int]] = None,
                              window_from_idx: int = 0,
                              window_to_idx: Optional[int] = None,
                              score_filter_enabled: bool = False,
                              vol_div_bonus_override: Optional[dict] = None,
                              score_threshold: Optional[int] = None,
                              ) -> tuple:
    """
    事件驱动主循环。

    Args:
      klines: 完整 1h K 线序列（含 RSI 热身段）
      symbol: 交易对（用于冷却期 key 与 trade.symbol 标记）
      params: 回测参数
      btc_index: 由 load_btc_index() 生成的 BTC 24h 涨跌幅索引；None = 跳过 BTC 过滤
      signals: 预计算的信号 idx 列表（不传则内部检测）
      window_from_idx / window_to_idx: 仅在此范围内允许"开新仓"（出场可延伸到窗口外）
      score_filter_enabled: 是否启用评分过滤
      vol_div_bonus_override / score_threshold: 转发给 score_filter_signal

    Returns:
      (trades: List[BacktestTrade], state: _BacktestState)

    主循环逻辑（每根 K 线）：
      1. 跨日重置 daily_loss / daily_trades_opened
      2. 推进所有 open_trades；触发平仓事件 → 更新 realized_pnl / 风控状态
      3. 若 bar 在信号集合且窗口内：
         - 风控前置（pause/daily_loss/daily_trades/cooldown/position_pct）
         - BTC 过滤
         - 评分过滤（可选；grade=B 时 stake 减半）
         - 复利计算 actual_stake
         - 开仓 → 加入 open_trades
      4. 收尾：未平仓的强制按最后一根 close 出场（time_stop 兜底）
    """
    state = _BacktestState(initial_balance=params.account_balance)

    if signals is None:
        signals = detect_entry_signals(klines, params)
    signal_set = set(s for s in signals if s + 1 < len(klines))

    if window_to_idx is None:
        window_to_idx = len(klines)

    # 评分过滤的预计算（仅在启用时）
    rsi_series_full = None
    if score_filter_enabled:
        closes = [k['close'] for k in klines]
        rsi_series_full = calc_rsi_series(closes, params.rsi_period)

    for bar_idx in range(len(klines)):
        bar = klines[bar_idx]
        bar_time_ms = _iso_to_ms(bar['time'])

        # ── 1. 跨日重置 ──
        bar_day = bar['time'][:10]
        if bar_day != state.last_day:
            state.last_day = bar_day
            state.daily_loss = 0.0
            state.daily_trades_opened = 0

        # ── 2. 推进所有持仓 ──
        still_open = []
        for ot in state.open_trades:
            close_event = _step_open_trade(ot, bar, params)
            if close_event is None:
                still_open.append(ot)
                continue
            exit_price, reason = close_event
            bt = _finalize_open_trade(ot, exit_price, bar['time'], reason, params)
            state.closed_trades.append(bt)

            # 更新风控状态
            state.realized_pnl += bt.pnl_usd
            if bt.pnl_usd < 0:
                state.daily_loss += abs(bt.pnl_usd)
                state.consecutive_losses += 1
                if state.consecutive_losses >= params.consecutive_loss_pause:
                    state.paused_until_ms = bar_time_ms + params.pause_hours * 3600 * 1000
            else:
                state.consecutive_losses = 0

            # 同币冷却：仅止损类（与实盘 CloseType.is_stop_loss 对齐）+ 亏损平仓
            stop_kinds = ('hard_stop', 'trail_stop', 'time_stop')
            if reason in stop_kinds and bt.pnl_usd < 0:
                state.cooldown_until_ms[ot.symbol] = (
                    bar_time_ms + params.cooldown_hours * 3600 * 1000
                )

        state.open_trades = still_open

        # ── 3. 信号触发：仅当 bar 在信号集合且在窗口内 ──
        if bar_idx not in signal_set:
            continue
        if bar_idx < window_from_idx or bar_idx >= window_to_idx:
            continue

        # 风控前置
        proposed_stake = compute_compound_stake(state, params)

        # 评分过滤（先做，决定 stake 是否减半）
        if score_filter_enabled and rsi_series_full is not None:
            sf = score_filter_signal(
                klines, rsi_series_full, bar_idx, params,
                vol_div_bonus_override=vol_div_bonus_override,
                score_threshold=score_threshold,
            )
            if not sf['passed']:
                state.bump_skip('score_skip')
                continue
            if sf.get('grade') == 'B':
                proposed_stake = max(1, round(proposed_stake * 0.5))

        # BTC 过滤
        if params.btc_filter_enabled and btc_index:
            btc_pct = get_btc_24h_change_at(btc_index, bar_time_ms)
            if btc_pct is not None and btc_pct <= params.btc_crash_threshold:
                state.bump_skip('btc_filter')
                continue

        # 风控网关
        allowed, reason = _check_risk_gates(
            state, params, bar_time_ms, symbol, proposed_stake
        )
        if not allowed:
            state.bump_skip(reason)
            continue

        # 开仓
        ot = _open_position(klines, bar_idx, symbol, proposed_stake, params)
        if ot is None:
            state.bump_skip('no_next_bar')
            continue
        state.open_trades.append(ot)
        state.daily_trades_opened += 1

    # ── 4. 收尾：未平仓的强制按最后一根 close 出场 ──
    if state.open_trades:
        last_bar = klines[-1]
        for ot in state.open_trades:
            bt = _finalize_open_trade(
                ot, last_bar['close'], last_bar['time'], 'eos', params,
            )
            state.closed_trades.append(bt)
            state.realized_pnl += bt.pnl_usd
        state.open_trades = []

    return state.closed_trades, state


def _legacy_run_backtest(klines: List[dict], symbol: str, params: BacktestParams,
                         signals: List[int],
                         score_filter_enabled: bool = False,
                         vol_div_bonus_override: Optional[dict] = None,
                         score_threshold: Optional[int] = None) -> List[BacktestTrade]:
    """
    旧版独立交易模式（每笔信号单独 simulate_trade，不维护跨交易状态）。
    保留用于：grid_search、向后兼容、与新引擎的口径对照。
    """
    trades = []
    skipped_by_score = 0
    rsi_series_full = None
    if score_filter_enabled:
        closes = [k['close'] for k in klines]
        rsi_series_full = calc_rsi_series(closes, params.rsi_period)

    original_stake = params.stake
    try:
        for sig_idx in signals:
            if score_filter_enabled and rsi_series_full is not None:
                sf = score_filter_signal(
                    klines, rsi_series_full, sig_idx, params,
                    vol_div_bonus_override=vol_div_bonus_override,
                    score_threshold=score_threshold,
                )
                if not sf['passed']:
                    skipped_by_score += 1
                    continue
                if sf.get('grade') == 'B':
                    params.stake = round(original_stake * 0.5)
                else:
                    params.stake = original_stake
            t = simulate_trade(klines, sig_idx, params)
            t.symbol = symbol
            trades.append(t)
    finally:
        params.stake = original_stake

    if score_filter_enabled:
        logger.info(
            f"  📊 评分过滤(legacy): {len(signals)} 信号 → {len(trades)} 通过, "
            f"{skipped_by_score} 跳过"
        )
    return trades



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

def _parse_date(s: str) -> datetime:
    """'YYYY-MM-DD' → UTC midnight datetime"""
    return datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=timezone.utc)


def filter_klines_by_date(klines: List[dict],
                          date_from: Optional[str] = None,
                          date_to: Optional[str] = None) -> List[dict]:
    """
    把 K 线裁剪到 [date_from 00:00, date_to 24:00) UTC 区间内。
    保持顺序，不修改原列表。任意端为空表示不限。
    """
    if not date_from and not date_to:
        return klines
    out = []
    from_dt = _parse_date(date_from) if date_from else None
    to_dt = (_parse_date(date_to) + timedelta(days=1)) if date_to else None
    for k in klines:
        # k['time'] 形如 '2026-05-13T12:00:00+00:00'
        try:
            t = datetime.fromisoformat(k['time'])
        except Exception:
            continue
        if from_dt and t < from_dt:
            continue
        if to_dt and t >= to_dt:
            continue
        out.append(k)
    return out


def run_backtest(symbol: str, days: int = 90,
                 params: Optional[BacktestParams] = None,
                 date_from: Optional[str] = None,
                 date_to: Optional[str] = None,
                 score_filter: bool = False,
                 vol_div_bonus_override: Optional[dict] = None,
                 score_threshold: Optional[int] = None,
                 btc_index: Optional[dict] = None) -> BacktestResult:
    """
    对单个币种执行完整回测。

    时间窗口选择两选一：
      - 用 days：拉过去 N 天数据，全部参与回测
      - 用 date_from/date_to：拉足够长的数据（确保 RSI 窗口够热身），
        但只在 [date_from, date_to] 区间内检测信号 + 模拟交易

    评分过滤（可选）：
      - score_filter=True：对每个信号执行评分过滤（含量价背离），低于阈值跳过
      - vol_div_bonus_override: 覆盖量价背离加分值 {"strong": 12, "medium": 8, "mild": 6}
      - score_threshold: 覆盖评分阈值（默认 config.SCORE_HALF_THRESHOLD=40）

    给 date_from/date_to 时，days 会被自动放大到覆盖范围 + 30 天预热，
    保证 RSI 序列在窗口起点已经稳定。

    ──────────────────────────────────────────────────────────────────
    M-3 完整版（2026-05）：
      - 默认走 run_backtest_event_driven 事件驱动引擎，与实盘口径对齐
        （复利仓位 / BTC 过滤 / 单日亏损/开仓限频 / 连亏暂停 / 同币冷却 /
         资金费率成本）
      - 老用户若需对照 v1 行为，传 params.use_event_driven_engine=False
    ──────────────────────────────────────────────────────────────────

    btc_index 可以由调用方预先 load_btc_index() 一次然后传入（批量回测时
    避免重复下载 BTC 数据）；不传时函数内部按需懒加载（仅当
    params.btc_filter_enabled 时）。
    """
    if params is None:
        params = BacktestParams()

    # ── 决定要拉多少天数据 ──
    if date_from or date_to:
        from_dt = _parse_date(date_from) if date_from else _parse_date(date_to)
        to_dt = _parse_date(date_to) if date_to else _parse_date(date_from)
        # 从 from_dt 往前预热 30 天，到 to_dt 截止
        now_utc = datetime.now(timezone.utc)
        end_dt = min(to_dt + timedelta(days=1), now_utc)
        fetch_days = max(30, (now_utc - (from_dt - timedelta(days=30))).days + 1)
    else:
        fetch_days = days

    klines_full = load_cached_klines(symbol, '1h', fetch_days)
    if not klines_full:
        logger.error(f"无法获取 {symbol} 历史数据")
        return BacktestResult(params=asdict(params))

    # 信号检测必须用完整序列（前面要 RSI 热身）；
    # 只是过滤掉发生在窗口外的信号
    signals_all = detect_entry_signals(klines_full, params)
    signals_all = [s for s in signals_all if s + 1 < len(klines_full)]

    # 计算窗口边界（事件驱动引擎按 idx 范围内"开新仓"过滤）
    window_from_idx = 0
    window_to_idx = len(klines_full)
    if date_from or date_to:
        from_dt = _parse_date(date_from) if date_from else _parse_date(date_to)
        to_dt_excl = ((_parse_date(date_to) + timedelta(days=1))
                      if date_to else _parse_date(date_from) + timedelta(days=1))
        # 找 from / to 对应的 idx
        for i, k in enumerate(klines_full):
            try:
                t = datetime.fromisoformat(k['time'])
            except Exception:
                continue
            if t < from_dt:
                window_from_idx = i + 1
            elif t < to_dt_excl:
                window_to_idx = i + 1
            else:
                break

    # ── 选择引擎 ──
    use_event_driven = bool(getattr(params, 'use_event_driven_engine', True))

    if use_event_driven:
        # BTC 索引：仅在启用过滤且未传入时加载
        if btc_index is None and params.btc_filter_enabled:
            btc_index = load_btc_index(fetch_days, symbol=params.btc_symbol)

        # 窗口内信号数（仅日志用）
        if date_from or date_to:
            in_window_signals = sum(
                1 for s in signals_all
                if window_from_idx <= s < window_to_idx
            )
            logger.info(
                f"窗口 [{date_from or '...'}, {date_to or '...'}] 内 "
                f"{in_window_signals} 个候选信号，事件驱动引擎执行..."
            )
        else:
            logger.info(
                f"检测到 {len(signals_all)} 个候选信号，事件驱动引擎执行..."
            )

        trades, state = run_backtest_event_driven(
            klines_full, symbol, params,
            btc_index=btc_index,
            signals=signals_all,
            window_from_idx=window_from_idx,
            window_to_idx=window_to_idx,
            score_filter_enabled=score_filter,
            vol_div_bonus_override=vol_div_bonus_override,
            score_threshold=score_threshold,
        )

        # 日志：跳过原因分布（便于审计与实盘对齐）
        if state.skip_counters:
            skip_msg = ", ".join(
                f"{k}={v}" for k, v in sorted(state.skip_counters.items(),
                                              key=lambda kv: -kv[1])
            )
            logger.info(f"  ⏩ 跳过统计: {skip_msg}")
        logger.info(
            f"  ✅ 已平仓 {len(trades)} 笔 | 累计盈亏 {state.realized_pnl:+.2f}U"
        )

    else:
        # ── 旧版独立交易引擎（兼容路径）──
        if date_from or date_to:
            signals_in_window = [
                s for s in signals_all
                if window_from_idx <= s < window_to_idx
            ]
            logger.info(
                f"窗口内 {len(signals_in_window)} 个信号 (legacy 引擎)"
            )
        else:
            signals_in_window = signals_all
            logger.info(
                f"检测到 {len(signals_in_window)} 个入场信号 (legacy 引擎)"
            )

        trades = _legacy_run_backtest(
            klines_full, symbol, params, signals_in_window,
            score_filter_enabled=score_filter,
            vol_div_bonus_override=vol_div_bonus_override,
            score_threshold=score_threshold,
        )

    # 统计
    return calculate_stats(trades, params)


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
                       params: Optional[BacktestParams] = None,
                       date_from: Optional[str] = None,
                       date_to: Optional[str] = None,
                       score_filter: bool = False,
                       vol_div_bonus_override: Optional[dict] = None,
                       score_threshold: Optional[int] = None) -> List[BacktestResult]:
    """对多个币种执行批量回测，返回每个币种的 BacktestResult

    M-3 完整版：批量回测共享一份 BTC 索引（避免每个币种重复下载）。
    BTC 索引只在事件驱动引擎 + BTC 过滤启用时才加载。
    """
    if params is None:
        params = BacktestParams()

    # 决定 fetch_days（足够覆盖 BTC 索引）
    if date_from or date_to:
        from_dt = _parse_date(date_from) if date_from else _parse_date(date_to)
        now_utc = datetime.now(timezone.utc)
        fetch_days = max(30, (now_utc - (from_dt - timedelta(days=30))).days + 1)
    else:
        fetch_days = days

    # 共享 BTC 索引
    shared_btc_index = None
    use_event_driven = bool(getattr(params, 'use_event_driven_engine', True))
    if use_event_driven and params.btc_filter_enabled:
        shared_btc_index = load_btc_index(fetch_days, symbol=params.btc_symbol)

    results = []
    for symbol in symbols:
        if date_from or date_to:
            logger.info(f"批量回测: {symbol} / 窗口 {date_from or '...'} ~ {date_to or '...'}")
        else:
            logger.info(f"批量回测: {symbol} / {days}天")
        result = run_backtest(symbol, days, params, date_from=date_from, date_to=date_to,
                              score_filter=score_filter,
                              vol_div_bonus_override=vol_div_bonus_override,
                              score_threshold=score_threshold,
                              btc_index=shared_btc_index)
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

    # ── 时间窗口（与 --days 二选一；同时给则窗口生效，days 自动扩展为预热）──
    parser.add_argument('--date-from', dest='date_from', type=str, default=None,
                        help='窗口起始日 YYYY-MM-DD（含），不传 = 不限')
    parser.add_argument('--date-to', dest='date_to', type=str, default=None,
                        help='窗口结束日 YYYY-MM-DD（含），不传 = 不限。'
                             '只回测此区间内开仓的信号；出场可延伸到区间外')
    parser.add_argument('--day', type=str, default=None,
                        help='便捷参数：等价于 --date-from X --date-to X')

    # ── 单次回测的参数覆盖（不修改 config.py，方便对比） ──
    parser.add_argument('--daily-rsi-min', dest='daily_rsi_min', type=float, default=None,
                        help='临时覆盖 daily_rsi_min（不改 config）')
    parser.add_argument('--h4-rsi-drop', dest='h4_rsi_drop', type=float, default=None,
                        help='临时覆盖 h4_rsi_drop')
    parser.add_argument('--h4-rsi-enter', dest='h4_rsi_enter', type=float, default=None,
                        help='临时覆盖 h4_rsi_enter')
    parser.add_argument('--tp1', type=float, default=None, help='临时覆盖 tp1_pct')
    parser.add_argument('--tp2', type=float, default=None, help='临时覆盖 tp2_pct')
    parser.add_argument('--hard-stop', dest='hard_stop_pct', type=float, default=None,
                        help='临时覆盖 hard_stop_pct')

    # ── 评分过滤（含量价背离权重对比）──
    parser.add_argument('--score-filter', dest='score_filter', action='store_true',
                        help='启用评分过滤（含量价背离），低于阈值的信号跳过')
    parser.add_argument('--score-threshold', dest='score_threshold', type=int, default=None,
                        help='评分阈值（默认40），低于此分的信号不开单')
    parser.add_argument('--vol-div-bonus', dest='vol_div_bonus', type=str, default=None,
                        help='量价背离加分覆盖，格式: "mild,medium,strong" 如 "6,8,12"（默认 3,5,8）')
    parser.add_argument('--compare-bonus', dest='compare_bonus', action='store_true',
                        help='对比模式：自动跑两次（原始权重 vs 新权重），输出对比报告')
    # M-3：事件驱动主循环开关（默认开启；用此 flag 切回旧版独立交易模式做对照）
    parser.add_argument('--legacy-engine', dest='legacy_engine', action='store_true',
                        help='使用旧版独立交易引擎（不含复利/BTC过滤/风控限频/资金费率累计），'
                             '主要用于和事件驱动引擎对照看口径差异')
    parser.add_argument('--no-btc-filter', dest='no_btc_filter', action='store_true',
                        help='关闭 BTC 暴跌过滤（默认开启；只对事件驱动引擎生效）')
    parser.add_argument('--no-compound', dest='no_compound', action='store_true',
                        help='关闭自动复利（默认开启；只对事件驱动引擎生效）')

    args = parser.parse_args()

    # --day 作为快捷方式
    if args.day:
        args.date_from = args.date_from or args.day
        args.date_to = args.date_to or args.day

    # 构造覆盖后的参数
    def _build_params() -> BacktestParams:
        p = BacktestParams()
        if args.daily_rsi_min is not None:
            p.daily_rsi_min = args.daily_rsi_min
        if args.h4_rsi_drop is not None:
            p.h4_rsi_drop = args.h4_rsi_drop
        if args.h4_rsi_enter is not None:
            p.h4_rsi_enter = args.h4_rsi_enter
        if args.tp1 is not None:
            p.tp1_pct = args.tp1
        if args.tp2 is not None:
            p.tp2_pct = args.tp2
        if args.hard_stop_pct is not None:
            p.hard_stop_pct = args.hard_stop_pct
        # M-3 引擎开关
        if args.legacy_engine:
            p.use_event_driven_engine = False
        if args.no_btc_filter:
            p.btc_filter_enabled = False
        if args.no_compound:
            p.compound_enabled = False
        return p

    custom_params = _build_params()
    has_overrides = any([
        args.daily_rsi_min is not None, args.h4_rsi_drop is not None,
        args.h4_rsi_enter is not None, args.tp1 is not None,
        args.tp2 is not None, args.hard_stop_pct is not None,
    ])
    if has_overrides:
        print(f"\n⚙️  参数覆盖: daily_rsi_min={custom_params.daily_rsi_min}, "
              f"h4_rsi_drop={custom_params.h4_rsi_drop}, "
              f"h4_rsi_enter={custom_params.h4_rsi_enter}, "
              f"tp1={custom_params.tp1_pct}%, tp2={custom_params.tp2_pct}%, "
              f"hard_stop={custom_params.hard_stop_pct}%")

    # 引擎模式横幅
    if custom_params.use_event_driven_engine:
        feats = []
        if custom_params.compound_enabled:
            feats.append("复利")
        if custom_params.btc_filter_enabled:
            feats.append(f"BTC过滤(<{custom_params.btc_crash_threshold}%)")
        feats.append(f"风控(日亏≤{custom_params.max_daily_loss}U/日开仓≤{custom_params.max_daily_trades})")
        feats.append(f"funding={custom_params.funding_rate_pct}%/8h")
        print(f"🚀 引擎: 事件驱动 (event-driven) | 启用: {' / '.join(feats)}")
    else:
        print("🐢 引擎: legacy 独立交易模式（不含复利/BTC过滤/风控限频）")

    # ── 解析量价背离加分覆盖 ──
    vol_div_bonus_override = None
    if args.vol_div_bonus:
        parts = [int(x.strip()) for x in args.vol_div_bonus.split(',')]
        if len(parts) == 3:
            vol_div_bonus_override = {"mild": parts[0], "medium": parts[1], "strong": parts[2]}
            print(f"📊 量价背离加分覆盖: 轻微={parts[0]} 中等={parts[1]} 强烈={parts[2]}")
        else:
            print("⚠️  --vol-div-bonus 格式错误，应为 'mild,medium,strong' 如 '6,8,12'")
            sys.exit(1)

    if args.score_filter:
        threshold = args.score_threshold or config.SCORE_HALF_THRESHOLD
        print(f"📊 评分过滤已启用（阈值={threshold}分）")

    if args.date_from or args.date_to:
        print(f"📅 时间窗口: [{args.date_from or '...'}, {args.date_to or '...'}] UTC")

    symbols = args.symbols or [args.symbol]

    # ══════════════════════════════════════════════════════════════════
    #  对比模式：原始权重 vs 新权重，自动跑两轮
    # ══════════════════════════════════════════════════════════════════
    if args.compare_bonus:
        if not vol_div_bonus_override:
            vol_div_bonus_override = {"mild": 6, "medium": 8, "strong": 12}
            print(f"📊 对比模式: 未指定 --vol-div-bonus，使用推荐新权重 (6,8,12)")

        print(f"\n{'='*70}")
        print(f"  🔄 对比回测: {symbols} / {args.days}天")
        print(f"     原始权重: mild=3, medium=5, strong=8")
        print(f"     新权重:   mild={vol_div_bonus_override['mild']}, "
              f"medium={vol_div_bonus_override['medium']}, "
              f"strong={vol_div_bonus_override['strong']}")
        print(f"{'='*70}")

        threshold = args.score_threshold or config.SCORE_HALF_THRESHOLD

        for symbol in symbols:
            print(f"\n{'─'*60}")
            print(f"  📌 {symbol}")
            print(f"{'─'*60}")

            # A) 原始权重 + 评分过滤
            print(f"\n  ▶ [A] 原始权重 (3,5,8) + 评分过滤(阈值={threshold}):")
            result_old = run_backtest(
                symbol, args.days, params=custom_params,
                date_from=args.date_from, date_to=args.date_to,
                score_filter=True,
                vol_div_bonus_override={"mild": 3, "medium": 5, "strong": 8},
                score_threshold=threshold,
            )

            # B) 新权重 + 评分过滤
            print(f"\n  ▶ [B] 新权重 ({vol_div_bonus_override['mild']},"
                  f"{vol_div_bonus_override['medium']},"
                  f"{vol_div_bonus_override['strong']}) + 评分过滤(阈值={threshold}):")
            result_new = run_backtest(
                symbol, args.days, params=custom_params,
                date_from=args.date_from, date_to=args.date_to,
                score_filter=True,
                vol_div_bonus_override=vol_div_bonus_override,
                score_threshold=threshold,
            )

            # C) 无评分过滤（基线）
            print(f"\n  ▶ [C] 无评分过滤（基线）:")
            result_base = run_backtest(
                symbol, args.days, params=custom_params,
                date_from=args.date_from, date_to=args.date_to,
                score_filter=False,
            )

            # 打印对比表
            print(f"\n  {'─'*55}")
            print(f"  📊 对比结果: {symbol}")
            print(f"  {'─'*55}")
            print(f"  {'模式':<22} {'交易数':>6} {'胜率':>7} {'盈亏比':>6} {'总盈亏':>9} {'回撤':>6}")
            print(f"  {'─'*55}")
            print(f"  {'[C] 无过滤(基线)':<20} {result_base.total_trades:>5} "
                  f"{result_base.win_rate:>6.1f}% {result_base.profit_loss_ratio:>5.2f}x "
                  f"{result_base.total_pnl:>+8.2f}U {result_base.max_drawdown:>5.1f}%")
            print(f"  {'[A] 原始(3,5,8)':<20} {result_old.total_trades:>5} "
                  f"{result_old.win_rate:>6.1f}% {result_old.profit_loss_ratio:>5.2f}x "
                  f"{result_old.total_pnl:>+8.2f}U {result_old.max_drawdown:>5.1f}%")
            new_label = f"[B] 新({vol_div_bonus_override['mild']},{vol_div_bonus_override['medium']},{vol_div_bonus_override['strong']})"
            print(f"  {new_label:<20} {result_new.total_trades:>5} "
                  f"{result_new.win_rate:>6.1f}% {result_new.profit_loss_ratio:>5.2f}x "
                  f"{result_new.total_pnl:>+8.2f}U {result_new.max_drawdown:>5.1f}%")
            print(f"  {'─'*55}")

            # 差值分析
            if result_new.total_trades > result_old.total_trades:
                extra = result_new.total_trades - result_old.total_trades
                pnl_diff = result_new.total_pnl - result_old.total_pnl
                print(f"  💡 新权重多放入 {extra} 笔交易，盈亏变化: {pnl_diff:+.2f}U")
            elif result_new.total_trades == result_old.total_trades:
                print(f"  ℹ️  交易数相同，量价背离加分未改变过滤结果")
            print()

        sys.exit(0)

    if args.monthly:
        # 仅输出月度分解（对指定币种跑回测后只显示月度）
        print(f"\n📅 月度分解回测: {symbols} / {args.days}天")
        for symbol in symbols:
            result = run_backtest(symbol, args.days, params=custom_params,
                                  date_from=args.date_from, date_to=args.date_to,
                                  score_filter=args.score_filter,
                                  vol_div_bonus_override=vol_div_bonus_override,
                                  score_threshold=args.score_threshold)
            if result.trades:
                print_monthly_breakdown(result.trades, symbol)
            else:
                print(f"  {symbol}: 无交易数据")

    elif args.batch:
        # 批量回测
        batch_symbols = config.BATCH_BACKTEST_SYMBOLS
        batch_days = args.days if args.days != 90 else config.BATCH_BACKTEST_DAYS
        if args.date_from or args.date_to:
            print(f"\n🚀 批量回测: {len(batch_symbols)} 个币种 / 窗口 "
                  f"{args.date_from or '...'} ~ {args.date_to or '...'}")
        else:
            print(f"\n🚀 批量回测: {len(batch_symbols)} 个币种 / {batch_days}天")

        results = run_batch_backtest(batch_symbols, batch_days, params=custom_params,
                                     date_from=args.date_from, date_to=args.date_to,
                                     score_filter=args.score_filter,
                                     vol_div_bonus_override=vol_div_bonus_override,
                                     score_threshold=args.score_threshold)
        correlation = calculate_correlation_matrix(results)
        rankings = rank_coins(results)
        report = generate_batch_report(results, correlation, rankings)

        # 打印报告
        print_batch_report(report, results)

        # 保存结果（含参数快照，面板可对比是否过期）
        save_data = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'days': batch_days,
            'date_from': args.date_from,
            'date_to': args.date_to,
            'report': report,
            'config_snapshot': {
                'daily_rsi_min': custom_params.daily_rsi_min,
                'tp1_pct': custom_params.tp1_pct,
                'tp2_pct': custom_params.tp2_pct,
                'hard_stop_pct': custom_params.hard_stop_pct,
                'h4_rsi_drop': custom_params.h4_rsi_drop,
                'batch_symbols': config.BATCH_BACKTEST_SYMBOLS,
                'overrides_applied': has_overrides,
            },
        }
        atomic_write_json(BATCH_BACKTEST_RESULTS_FILE, save_data)
        logger.info(f"批量回测结果已保存到 {BATCH_BACKTEST_RESULTS_FILE}")

    elif args.grid:
        # 网格搜索
        if args.date_from or args.date_to:
            print(f"\n⚠️  --grid 暂不支持 --date-from/--date-to（已忽略，仍按 --days 跑）")
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
            if args.date_from or args.date_to:
                print(f"\n🎯 回测: {symbol} / 窗口 {args.date_from or '...'} ~ {args.date_to or '...'}")
            else:
                print(f"\n🎯 回测: {symbol} / {args.days}天")
            result = run_backtest(symbol, args.days, params=custom_params,
                                  date_from=args.date_from, date_to=args.date_to,
                                  score_filter=args.score_filter,
                                  vol_div_bonus_override=vol_div_bonus_override,
                                  score_threshold=args.score_threshold)
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
            'date_from': args.date_from,
            'date_to': args.date_to,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'results': [r.to_dict() for r in all_results],
            'config_snapshot': {
                'daily_rsi_min': custom_params.daily_rsi_min,
                'tp1_pct': custom_params.tp1_pct,
                'tp2_pct': custom_params.tp2_pct,
                'hard_stop_pct': custom_params.hard_stop_pct,
                'h4_rsi_drop': custom_params.h4_rsi_drop,
                'leverage': custom_params.leverage,
                'overrides_applied': has_overrides,
            },
        }
        atomic_write_json(BACKTEST_RESULTS_FILE, save_data)
        logger.info(f"结果已保存到 {BACKTEST_RESULTS_FILE}")
