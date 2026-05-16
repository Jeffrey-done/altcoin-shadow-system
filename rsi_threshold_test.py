#!/usr/bin/env python3
"""
日线 RSI 阈值诊断工具
─────────────────────────────────────────────────────────────────
针对"扫到的币满足前 3 个条件、但日线 RSI 没到阈值、一整天没信号"
这种情况，用 Binance 市场数据回答两个问题：
  1) 那一天通过前 3 个过滤（vol/price/24h%）的币，日线 RSI 都是多少？
  2) 不同阈值（70/72/75/78/80/82/85）下，那一天能放出几个候选？

支持模式：
  - 实时（默认）：用当前 fetch_tickers，最快
  - 历史日期 --date YYYY-MM-DD：重建那一天 UTC 收盘时刻的市场快照
                                 （从历史 K 线算 24h 成交量/涨幅/RSI）

用法:
  python3 rsi_threshold_test.py
  python3 rsi_threshold_test.py --date 2026-05-14            # 回看 5/14 那天
  python3 rsi_threshold_test.py --thresholds 70 72 75 78 80 82 85
  python3 rsi_threshold_test.py --vol 300000 --price 1 --pct 8
  python3 rsi_threshold_test.py --top 50                     # 输出明细前 N 行
  python3 rsi_threshold_test.py --date 2026-05-14 --universe PEPE/USDT,DOGE/USDT,...

输出：
  ① 各候选币的日线 RSI 排行（按 RSI 降序）
  ② 每个阈值下的候选数 + 占比柱状图
  ③ 当前 config.DAILY_RSI_MIN 标记 + 推荐阈值（让那天至少有 N 个候选的最高值）

不会改 config，只是诊断。看完心里有数后再手动调 config.DAILY_RSI_MIN。
"""

import argparse
import sys
import os
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ccxt

import config
from altcoin_scanner import calc_rsi_wilder
from common import setup_logger

logger = setup_logger("rsi_test")


# ─────────────────────────────────────────────────────────────────
#  实时模式：fetch_tickers + fetch_ohlcv
# ─────────────────────────────────────────────────────────────────
def fetch_daily_rsi(exchange, symbol: str, limit: int = 50):
    """拉取日线 RSI（最新一根未收盘 K 线丢弃，与扫描器一致）"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '1d', limit=limit + 1)
        if len(ohlcv) >= 2:
            ohlcv = ohlcv[:-1]
        closes = [c[4] for c in ohlcv]
        if len(closes) < config.RSI_PERIOD + 1:
            return None
        return calc_rsi_wilder(closes)
    except Exception as e:
        logger.debug(f"拉取 {symbol} 日线 RSI 失败: {e}")
        return None


# ─────────────────────────────────────────────────────────────────
#  历史模式：根据某一天的日线 K 线重建快照
# ─────────────────────────────────────────────────────────────────
def parse_target_date(date_str: str) -> datetime:
    """把 'YYYY-MM-DD' 解析为该日 UTC 00:00 时刻；不允许未来日"""
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit(f"❌ 日期格式错误: {date_str}（应为 YYYY-MM-DD）")
    today_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if dt > today_utc:
        raise SystemExit(f"❌ 日期 {date_str} 是未来，没数据")
    return dt


def build_universe(exchange, custom_symbols=None):
    """
    构造待评估币种全集：
      - 如果用户指定了 --universe，直接返回那个列表
      - 否则：用现货 markets 里所有 USDT 永续/现货里以 /USDT 结尾、价格 < 1 的币
        （价格过滤后续 pre-filter 还会再做，这里粗筛减少 API 量）
    """
    if custom_symbols:
        return custom_symbols
    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        raise SystemExit(f"❌ 拉取 ticker 列表失败: {e}")
    universe = []
    for symbol, t in tickers.items():
        if not symbol.endswith('/USDT'):
            continue
        last = t.get('last') or 0
        # 当前价 ≤ 5U 的都进 universe（历史价格可能比当前低很多，留足余量）
        if 0 < last <= 5:
            universe.append(symbol)
    return universe


def historical_snapshot(exchange, symbol: str, target_dt: datetime,
                        rsi_period: int = config.RSI_PERIOD):
    """
    返回 target_dt 那一天 UTC 00:00 收盘时刻的快照：
      {
        'symbol': 'XXX/USDT',
        'price':  当日收盘价,
        'vol':    当日 quote-volume（USDT），
        'pct':    当日涨幅 %（close - prev_close）/prev_close * 100,
        'rsi_1d': 当日收盘后的日线 RSI,
      }
    target_dt 必须是 UTC 00:00。日线 K 线开盘时间 = 该日 00:00。
    我们要的是该日"已收盘"的状态，所以等价于 K 线开盘时间 == target_dt 的那根。

    返回 None 表示数据不够。
    """
    try:
        # 拉到 target_dt 之前 + 当日 + 之后一根（缓冲），覆盖 RSI 所需历史
        # 取 RSI_PERIOD + 30 根做缓冲
        bars_needed = rsi_period + 30
        since_ms = int((target_dt - timedelta(days=bars_needed)).timestamp() * 1000)
        end_ms = int((target_dt + timedelta(days=2)).timestamp() * 1000)

        ohlcv = exchange.fetch_ohlcv(symbol, '1d', since=since_ms, limit=bars_needed + 5)
        if not ohlcv:
            return None

        # 切掉 target_dt 之后的所有 bar（保留含 target_dt 那根，因为那是已收盘的）
        target_ms = int(target_dt.timestamp() * 1000)
        kept = [b for b in ohlcv if b[0] <= target_ms]
        if len(kept) < rsi_period + 1:
            return None

        # 必须有一根 bar 的开盘时间正好 = target_ms（说明那一天有交易）
        if kept[-1][0] != target_ms:
            # 那天可能没数据（停盘 / 上线前），跳过
            return None

        closes = [b[4] for b in kept]
        if len(closes) < rsi_period + 1:
            return None

        rsi = calc_rsi_wilder(closes, period=rsi_period)
        last = kept[-1]
        prev_close = kept[-2][4]
        pct = (last[4] - prev_close) / prev_close * 100 if prev_close else 0

        return {
            'symbol': symbol,
            'price': last[4],
            # quote volume = base_volume * close（K 线没直接给 quote vol，近似）
            'vol': last[5] * last[4],
            'pct': pct,
            'rsi_1d': rsi,
        }
    except Exception as e:
        logger.debug(f"拉取 {symbol} 历史快照失败: {e}")
        return None


# ─────────────────────────────────────────────────────────────────
#  渲染辅助
# ─────────────────────────────────────────────────────────────────
def render_bar(pct: float, width: int = 30) -> str:
    filled = min(width, int(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


# ─────────────────────────────────────────────────────────────────
#  主流程
# ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='日线 RSI 阈值诊断（实时或历史日期）'
    )
    parser.add_argument('--date', type=str, default=None,
                        help='历史日期 YYYY-MM-DD（不传 = 实时）')
    parser.add_argument('--vol', type=float, default=config.VOL_MIN,
                        help=f'最小 24h 成交量 USDT (默认 {config.VOL_MIN:,.0f})')
    parser.add_argument('--price', type=float, default=config.PRICE_MAX,
                        help=f'最大价格 USDT (默认 {config.PRICE_MAX})')
    parser.add_argument('--pct', type=float, default=config.PCT_24H_MIN,
                        help=f'最小 24h 涨幅 %% (默认 {config.PCT_24H_MIN})')
    parser.add_argument('--thresholds', type=float, nargs='+',
                        default=[65, 70, 72, 75, 78, 80, 82, 85],
                        help='待评估的 RSI 阈值列表')
    parser.add_argument('--limit', type=int, default=120,
                        help='前 3 条件通过后，最多评估多少个币')
    parser.add_argument('--top', type=int, default=30,
                        help='明细表显示前 N 行（按 RSI 降序）')
    parser.add_argument('--min-candidates', type=int, default=3,
                        help='推荐阈值时要求至少有几个候选（默认 3）')
    parser.add_argument('--universe', type=str, default=None,
                        help='历史模式专用：指定币种列表（逗号分隔），不传 = 自动用全市场 USDT 对')
    args = parser.parse_args()

    mode_str = "📅 历史" if args.date else "🔴 实时"
    target_dt = parse_target_date(args.date) if args.date else None

    print(f"\n{'='*78}")
    print(f"  🔬 日线 RSI 阈值诊断  [{mode_str}{(' ' + args.date) if args.date else ''}]")
    print(f"{'='*78}")
    print(f"  前置过滤: vol ≥ {args.vol:,.0f}U | price ≤ {args.price}U | "
          f"24h 涨幅 ≥ {args.pct}%")
    print(f"  当前 config.DAILY_RSI_MIN = {config.DAILY_RSI_MIN}")
    print(f"  待评估阈值: {sorted(args.thresholds)}")
    print(f"{'='*78}\n")

    from exchange_manager import make_exchange
    exchange = make_exchange('binance')

    rows = []

    if target_dt is None:
        # ── 实时模式 ──
        print("→ 拉取 Binance 全市场 ticker ...")
        try:
            tickers = exchange.fetch_tickers()
        except Exception as e:
            print(f"❌ 拉取 ticker 失败: {e}")
            return 1

        pre_filtered = []
        for symbol, t in tickers.items():
            if not symbol.endswith('/USDT'):
                continue
            price = t.get('last') or 0
            vol = t.get('quoteVolume') or 0
            pct = t.get('percentage') or 0
            if price <= 0 or price > args.price:
                continue
            if vol < args.vol:
                continue
            if pct < args.pct:
                continue
            pre_filtered.append({'symbol': symbol, 'price': price, 'vol': vol, 'pct': pct})

        pre_filtered.sort(key=lambda x: x['pct'], reverse=True)
        total_pre = len(pre_filtered)
        print(f"→ 通过前 3 个条件: {total_pre} 个币")

        if total_pre == 0:
            print("\n❌ 没有任何币通过前置过滤")
            print("   建议放宽 --vol / --price / --pct 后重试")
            return 0

        if total_pre > args.limit:
            print(f"  （超过 --limit {args.limit}，只评估按 24h 涨幅 Top {args.limit}）")
            pre_filtered = pre_filtered[:args.limit]

        print(f"→ 开始拉取 {len(pre_filtered)} 个币的日线 RSI...\n")
        for i, item in enumerate(pre_filtered, 1):
            rsi = fetch_daily_rsi(exchange, item['symbol'])
            if rsi is None:
                continue
            rows.append({**item, 'rsi_1d': rsi})
            if i % 20 == 0:
                print(f"  进度: {i}/{len(pre_filtered)}")
            time.sleep(0.05)
    else:
        # ── 历史模式 ──
        if args.universe:
            symbols = [s.strip() for s in args.universe.split(',') if s.strip()]
            print(f"→ 使用自定义 universe: {len(symbols)} 个币")
        else:
            symbols = build_universe(exchange)
            print(f"→ 自动构造 universe: {len(symbols)} 个 USDT 对（粗筛 last≤5U）")

        # 历史模式 API 调用更贵（每个币要拉日线序列），保险起见限制总数
        # 先按 universe 大小做 RPS 估算
        max_eval = max(args.limit * 3, 200)  # 历史模式放宽点
        if len(symbols) > max_eval:
            print(f"  （超过软上限 {max_eval}，截断到前 {max_eval} 个）")
            symbols = symbols[:max_eval]

        print(f"→ 重建 {args.date} UTC 收盘时刻的市场快照（约 {len(symbols)*0.15:.0f} 秒）...")
        snapshots = []
        for i, symbol in enumerate(symbols, 1):
            snap = historical_snapshot(exchange, symbol, target_dt)
            if snap:
                snapshots.append(snap)
            if i % 30 == 0:
                print(f"  进度: {i}/{len(symbols)}  (已得到 {len(snapshots)} 个有效快照)")
            time.sleep(0.05)

        # 应用前 3 个过滤
        pre_filtered = [
            s for s in snapshots
            if s['price'] <= args.price
            and s['vol'] >= args.vol
            and s['pct'] >= args.pct
        ]
        pre_filtered.sort(key=lambda x: x['pct'], reverse=True)
        print(f"\n→ 那天通过前 3 个条件: {len(pre_filtered)} 个币（共评估 {len(snapshots)}）")

        if len(pre_filtered) > args.limit:
            print(f"  （超过 --limit {args.limit}，只评估按当日涨幅 Top {args.limit}）")
            pre_filtered = pre_filtered[:args.limit]

        rows = pre_filtered  # 历史模式 RSI 已经在 snapshot 里了

    if not rows:
        print("\n❌ 没有任何币的有效数据")
        return 1

    rows.sort(key=lambda x: x['rsi_1d'], reverse=True)

    # ── 明细表 Top N ──
    title_suffix = f"@ {args.date}" if args.date else "（实时）"
    print(f"\n{'='*78}")
    print(f"  📋 候选币日线 RSI 排行 {title_suffix} (Top {min(args.top, len(rows))})")
    print(f"{'='*78}")
    print(f"  {'币种':<16} {'价格':>11} {'24h 成交量':>15} {'24h 涨幅':>9} {'日线 RSI':>9}")
    print(f"  {'-'*72}")
    for r in rows[:args.top]:
        marker = "  ✅" if r['rsi_1d'] >= config.DAILY_RSI_MIN else ""
        print(f"  {r['symbol']:<16} {r['price']:>11.6f} {r['vol']:>14,.0f} "
              f"{r['pct']:>+8.1f}% {r['rsi_1d']:>8.1f}{marker}")
    if len(rows) > args.top:
        print(f"  ...（还有 {len(rows) - args.top} 个未显示，加 --top {len(rows)} 看全部）")

    # ── 阈值分布 ──
    print(f"\n{'='*78}")
    print(f"  📊 不同 RSI 阈值下的候选数（共评估 {len(rows)} 个币）")
    print(f"{'='*78}")
    print(f"  {'阈值':<12} {'候选数':>7} {'占比':>7}   分布")
    print(f"  {'-'*72}")

    threshold_counts = {}
    for thr in sorted(args.thresholds, reverse=True):
        cnt = sum(1 for r in rows if r['rsi_1d'] >= thr)
        pct = cnt / len(rows) * 100
        threshold_counts[thr] = cnt
        bar = render_bar(pct, width=30)
        marker = "  ← 当前 config" if abs(thr - config.DAILY_RSI_MIN) < 0.01 else ""
        print(f"  RSI ≥ {thr:<6.1f} {cnt:>5}    {pct:>5.1f}%   [{bar}]{marker}")

    # ── 推荐 ──
    print(f"\n{'='*78}")
    print("  💡 阈值建议")
    print(f"{'='*78}")
    cur_cnt = sum(1 for r in rows if r['rsi_1d'] >= config.DAILY_RSI_MIN)
    day_str = args.date if args.date else "今天"

    if cur_cnt >= args.min_candidates:
        print(f"  ✅ 当前 DAILY_RSI_MIN={config.DAILY_RSI_MIN} {day_str}有 {cur_cnt} 个候选，无需调整")
    else:
        print(f"  ⚠️  当前 DAILY_RSI_MIN={config.DAILY_RSI_MIN} {day_str}只有 {cur_cnt} 个候选 "
              f"(< {args.min_candidates})")
        recommend = None
        for thr in sorted(args.thresholds, reverse=True):
            if threshold_counts.get(thr, 0) >= args.min_candidates:
                recommend = thr
                break
        if recommend is not None and recommend < config.DAILY_RSI_MIN:
            cnt = threshold_counts[recommend]
            print(f"  💡 建议尝试 DAILY_RSI_MIN = {recommend}（{day_str} {cnt} 个候选）")
            print()
            print("  ⚠️ 但要注意：")
            print("     - 阈值降低 → 候选变多 → 可能引入更多假信号、胜率下降")
            print("     - 强烈建议改之前先用历史窗口回测验证：")
            if args.date:
                # 给个直接可拷贝的命令
                d = args.date
                print(f"       python3 backtest.py --date-from {d} --date-to {d} "
                      f"--daily-rsi-min {recommend} --batch")
            else:
                print(f"       python3 backtest.py --grid --symbol PEPE/USDT --days 90")
                print(f"       python3 backtest.py --batch --days 90")
        elif recommend is None:
            print(f"  ❌ 即使最低阈值也凑不够候选，说明{day_str}整体没行情")
            print("     更可能的原因：24h 涨幅过滤太严，可以临时放低 --pct 到 5 试试")
    print()

    # ── 提示 ──
    print(f"{'='*78}")
    print("  📝 接下来怎么验证？")
    print(f"{'='*78}")
    if args.date:
        d = args.date
        print(f"  1) 看那天降低 RSI 阈值能不能赚钱（当天窗口）：")
        print(f"     python3 backtest.py --date-from {d} --date-to {d} --daily-rsi-min 75 --batch")
        print()
        print(f"  2) 看该周/该月窗口效果（更有统计意义）：")
        print(f"     python3 backtest.py --date-from 2026-05-01 --date-to {d} --daily-rsi-min 75 --batch")
        print()
    print("  3) 在 90 天内做 RSI×TP×止损 网格搜索（找最优组合）：")
    print("     python3 backtest.py --grid --symbol PEPE/USDT --days 90")
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
