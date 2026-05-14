#!/usr/bin/env python3
"""
日线 RSI 阈值诊断工具
─────────────────────────────────────────────────────────────────
针对"扫到的币满足前 3 个条件、但日线 RSI 没到阈值、一整天没信号"
这种情况，直接用今天实时的 Binance 市场数据回答两个问题：
  1) 今天通过前 3 个过滤（vol/price/24h%）的币，日线 RSI 都是多少？
  2) 不同阈值（70/72/75/78/80/82/85）下，今天能放出几个候选？

用法:
  python3 rsi_threshold_test.py
  python3 rsi_threshold_test.py --thresholds 70 72 75 78 80 82 85
  python3 rsi_threshold_test.py --vol 300000 --price 1 --pct 8
  python3 rsi_threshold_test.py --top 50           # 输出明细前 N 行

输出：
  ① 各候选币的日线 RSI 排行（按 RSI 降序）
  ② 每个阈值下的候选数 + 占比柱状图
  ③ 当前 config.DAILY_RSI_MIN 标记 + 推荐阈值（让今天至少有 3 个候选的最高值）

不会改 config，只是诊断。看完心里有数后再手动调 config.DAILY_RSI_MIN。
"""

import argparse
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ccxt

import config
from altcoin_scanner import calc_rsi_wilder
from common import setup_logger

logger = setup_logger("rsi_test")


def fetch_daily_rsi(exchange, symbol: str, limit: int = 50):
    """拉取日线 RSI，丢弃未收盘的最后一根，与扫描器逻辑一致"""
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


def render_bar(pct: float, width: int = 30) -> str:
    filled = min(width, int(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


def main():
    parser = argparse.ArgumentParser(
        description='日线 RSI 阈值诊断（针对当日实时市场）'
    )
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
                        help='前 3 条件通过后，最多评估多少个币（避免 API 过多）')
    parser.add_argument('--top', type=int, default=30,
                        help='明细表显示前 N 行（按 RSI 降序）')
    parser.add_argument('--min-candidates', type=int, default=3,
                        help='推荐阈值时要求至少有几个候选（默认 3）')
    args = parser.parse_args()

    print(f"\n{'='*78}")
    print(f"  🔬 日线 RSI 阈值诊断")
    print(f"{'='*78}")
    print(f"  前置过滤: vol ≥ {args.vol:,.0f}U | price ≤ {args.price}U | "
          f"24h 涨幅 ≥ {args.pct}%")
    print(f"  当前 config.DAILY_RSI_MIN = {config.DAILY_RSI_MIN}")
    print(f"  待评估阈值: {sorted(args.thresholds)}")
    print(f"{'='*78}\n")

    exchange = ccxt.binance({'enableRateLimit': True})

    # ── 第一步：拉全市场 ticker，按前 3 个条件预筛 ──
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
        pre_filtered.append({
            'symbol': symbol, 'price': price, 'vol': vol, 'pct': pct,
        })

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

    # ── 第二步：对每个币拉日线 RSI ──
    print(f"→ 开始拉取 {len(pre_filtered)} 个币的日线 RSI（约 {len(pre_filtered)*0.15:.0f} 秒）...\n")
    rows = []
    for i, item in enumerate(pre_filtered, 1):
        rsi = fetch_daily_rsi(exchange, item['symbol'])
        if rsi is None:
            continue
        rows.append({**item, 'rsi_1d': rsi})
        if i % 20 == 0:
            print(f"  进度: {i}/{len(pre_filtered)}")
        time.sleep(0.05)

    if not rows:
        print("\n❌ 所有币的日线 RSI 都拉取失败")
        return 1

    rows.sort(key=lambda x: x['rsi_1d'], reverse=True)

    # ── 第三步：明细表 Top N ──
    print(f"\n{'='*78}")
    print(f"  📋 候选币日线 RSI 排行（Top {min(args.top, len(rows))}）")
    print(f"{'='*78}")
    print(f"  {'币种':<16} {'价格':>11} {'24h 成交量':>15} {'24h 涨幅':>9} {'日线 RSI':>9}")
    print(f"  {'-'*72}")
    for r in rows[:args.top]:
        marker = ""
        if r['rsi_1d'] >= config.DAILY_RSI_MIN:
            marker = "  ✅"
        print(f"  {r['symbol']:<16} {r['price']:>11.6f} {r['vol']:>14,.0f} "
              f"{r['pct']:>+8.1f}% {r['rsi_1d']:>8.1f}{marker}")
    if len(rows) > args.top:
        print(f"  ...（还有 {len(rows) - args.top} 个未显示，加 --top {len(rows)} 看全部）")

    # ── 第四步：阈值分布 ──
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
        marker = ""
        if abs(thr - config.DAILY_RSI_MIN) < 0.01:
            marker = "  ← 当前 config"
        print(f"  RSI ≥ {thr:<6.1f} {cnt:>5}    {pct:>5.1f}%   [{bar}]{marker}")

    # ── 第五步：推荐 ──
    print(f"\n{'='*78}")
    print("  💡 阈值建议")
    print(f"{'='*78}")
    cur_cnt = sum(1 for r in rows if r['rsi_1d'] >= config.DAILY_RSI_MIN)

    if cur_cnt >= args.min_candidates:
        print(f"  ✅ 当前 DAILY_RSI_MIN={config.DAILY_RSI_MIN} 今天有 {cur_cnt} 个候选，无需调整")
    else:
        print(f"  ⚠️  当前 DAILY_RSI_MIN={config.DAILY_RSI_MIN} 今天只有 {cur_cnt} 个候选 "
              f"(< {args.min_candidates})")
        # 找让候选数 >= min-candidates 的最高阈值（保持选择性）
        recommend = None
        for thr in sorted(args.thresholds, reverse=True):
            if threshold_counts.get(thr, 0) >= args.min_candidates:
                recommend = thr
                break
        if recommend is not None and recommend < config.DAILY_RSI_MIN:
            cnt = threshold_counts[recommend]
            print(f"  💡 建议尝试 DAILY_RSI_MIN = {recommend}（今日 {cnt} 个候选）")
            print(f"     在 config.py 把 DAILY_RSI_MIN 改成 {recommend} 即可")
            print()
            print("  ⚠️ 但要注意：")
            print("     - 阈值降低 → 候选变多 → 可能引入更多假信号、胜率下降")
            print("     - 强烈建议改之前先跑一次回测验证：")
            print(f"       python3 backtest.py --grid --symbol PEPE/USDT --days 90")
            print(f"       python3 backtest.py --batch --days 90")
        elif recommend is None:
            print("  ❌ 即使最低阈值也凑不够候选，说明今天整体没行情")
            print("     更可能的原因：24h 涨幅过滤太严，可以临时放低 --pct 到 5 试试")
    print()

    # ── 提示 ──
    print(f"{'='*78}")
    print("  📝 接下来怎么验证？")
    print(f"{'='*78}")
    print("  1) 想看历史上哪个 RSI 阈值最赚钱（带胜率/盈亏比/回撤）：")
    print("     python3 backtest.py --grid --symbol PEPE/USDT --days 90")
    print("     （会枚举 RSI=72/75/78/82 × TP × 止损 等组合）")
    print()
    print("  2) 想看一组币种在某个阈值下整体表现：")
    print("     python3 backtest.py --batch --days 90")
    print()
    print("  3) 单独验证某个阈值（修改 config.DAILY_RSI_MIN 后）：")
    print("     python3 backtest.py --symbol PEPE/USDT --days 30")
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
