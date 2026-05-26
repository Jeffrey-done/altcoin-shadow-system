#!/usr/bin/env python3
"""
因子 IC/IR 分析命令行工具
==========================

对指定币种（或批量）跑因子预测力分析，输出每个因子的 IC、IR、衰减情况和权重建议。

用法：
  # 单币种分析（默认90天1h数据）
  python3 analyze_factors.py PEPE/USDT

  # 多币种汇总
  python3 analyze_factors.py PEPE/USDT DOGE/USDT FLOKI/USDT

  # 自定义天数和输出
  python3 analyze_factors.py PEPE/USDT --days 60 --output report.csv

  # 使用所有配置中的回测币种
  python3 analyze_factors.py --all --days 90

  # 显示权重建议
  python3 analyze_factors.py PEPE/USDT --weights

输出示例：
  ═══════════════════════════════════════════════════════════════
  因子 IC/IR 分析报告
  数据量: 2160 bars | 因子数: 32 | 有效: 18 | 强预测力: 5 | 噪音: 8
  ═══════════════════════════════════════════════════════════════
  因子                           分类           IC均值      IR   质量       ...
  ─────────────────────────────────────────────────────────────────
  rsi_14                         momentum       +0.0823  1.34  strong     ...
  funding_rate_zscore            microstructure +0.0651  0.89  moderate   ...
  ...
"""

import argparse
import logging
import os
import sys

# 确保项目根目录在 path 中
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import numpy as np
import pandas as pd

from common import setup_logger

logger = setup_logger("analyze_factors")


def fetch_ohlcv(symbol: str, days: int = 90, timeframe: str = "1h") -> pd.DataFrame:
    """
    从交易所拉取 OHLCV 数据。

    优先用 ccxt（Binance），失败时尝试本地缓存。
    返回 DataFrame: [timestamp, open, high, low, close, volume]
    """
    bars_needed = days * 24  # 1h timeframe

    try:
        import ccxt
        exchange = ccxt.binance({"enableRateLimit": True})

        logger.info(f"从 Binance 拉取 {symbol} {days}天 {timeframe} 数据...")
        all_ohlcv = []
        since = int((pd.Timestamp.utcnow() - pd.Timedelta(days=days)).timestamp() * 1000)

        while len(all_ohlcv) < bars_needed:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not batch:
                break
            all_ohlcv.extend(batch)
            since = batch[-1][0] + 1
            if len(batch) < 1000:
                break

        if not all_ohlcv:
            raise ValueError(f"无法获取 {symbol} 数据")

        df = pd.DataFrame(all_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        logger.info(f"  获取到 {len(df)} 根 K 线")
        return df

    except Exception as e:
        logger.error(f"数据获取失败 ({symbol}): {e}")
        raise


def run_single_symbol(
    symbol: str,
    days: int = 90,
    show_weights: bool = False,
    output_csv: str = "",
) -> "FactorICReport":
    """对单个币种跑分析"""
    from backtesting.factor_ic import FactorICAnalyzer

    df = fetch_ohlcv(symbol, days=days)

    analyzer = FactorICAnalyzer(
        ohlcv_df=df,
        horizons=[1, 4, 8, 24],
        ic_window=60,
        period_days=30,
        direction="short",
    )

    report = analyzer.run()

    # 输出表格
    print(f"\n{'═' * 50}")
    print(f"  {symbol} — {days}天因子分析")
    print(f"{'═' * 50}")
    print(report.to_table())

    # 权重建议
    if show_weights:
        weights = report.get_weight_suggestions()
        if weights:
            print("\n📊 建议权重分配：")
            print("─" * 50)
            sorted_w = sorted(weights.items(), key=lambda x: x[1], reverse=True)
            for name, w in sorted_w:
                bar = "█" * int(w * 50)
                print(f"  {name:<28} {w:.3f}  {bar}")
            print()

    # 保存 CSV
    if output_csv:
        report.save_csv(output_csv)

    return report


def run_multi_symbol(
    symbols: list,
    days: int = 90,
    output_csv: str = "",
) -> None:
    """多币种汇总分析：对每个因子取所有币种的 IC 中位数"""
    from backtesting.factor_ic import FactorICReport

    all_results = {}  # factor_name -> [ic_values across symbols]

    for symbol in symbols:
        try:
            report = run_single_symbol(symbol, days=days)
            for r in report.results:
                all_results.setdefault(r.factor_name, []).append(r.mean_ic)
        except Exception as e:
            logger.warning(f"跳过 {symbol}: {e}")
            continue

    if not all_results:
        print("❌ 所有币种分析失败")
        return

    # 汇总
    print(f"\n{'═' * 70}")
    print(f"  多币种汇总（{len(symbols)} 个币种, {days}天）")
    print(f"{'═' * 70}")
    print(f"{'因子':<30} {'IC中位数':>10} {'IC均值':>10} {'一致性':>10} {'有效币种':>10}")
    print("─" * 70)

    summary_rows = []
    for factor_name, ics in sorted(all_results.items(), key=lambda x: abs(np.median(x[1])), reverse=True):
        median_ic = np.median(ics)
        mean_ic = np.mean(ics)
        # 一致性 = 同号比例
        if len(ics) > 1:
            signs = [1 if x > 0 else -1 for x in ics if abs(x) > 0.01]
            consistency = max(signs.count(1), signs.count(-1)) / len(signs) if signs else 0
        else:
            consistency = 1.0

        print(f"  {factor_name:<28} {median_ic:>+9.4f} {mean_ic:>+9.4f} {consistency:>9.0%} {len(ics):>9}")
        summary_rows.append({
            "factor": factor_name,
            "median_ic": median_ic,
            "mean_ic": mean_ic,
            "consistency": consistency,
            "n_symbols": len(ics),
        })

    print("─" * 70)
    print()

    if output_csv:
        pd.DataFrame(summary_rows).to_csv(output_csv, index=False)
        logger.info(f"汇总报告已保存: {output_csv}")


def main():
    parser = argparse.ArgumentParser(
        description="因子 IC/IR 预测力分析工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 analyze_factors.py PEPE/USDT
  python3 analyze_factors.py PEPE/USDT DOGE/USDT --days 60
  python3 analyze_factors.py --all --output factor_report.csv
  python3 analyze_factors.py PEPE/USDT --weights
        """,
    )

    parser.add_argument(
        "symbols",
        nargs="*",
        help="币种列表 (如 PEPE/USDT DOGE/USDT)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="使用配置中的所有回测币种",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="分析数据天数 (默认 90)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="",
        help="输出 CSV 文件路径",
    )
    parser.add_argument(
        "--weights",
        action="store_true",
        help="显示建议权重分配",
    )

    args = parser.parse_args()

    # 确定币种列表
    symbols = args.symbols
    if args.all or not symbols:
        try:
            import config
            symbols = getattr(config, "BATCH_BACKTEST_SYMBOLS", [])
        except Exception:
            symbols = ["PEPE/USDT", "DOGE/USDT", "FLOKI/USDT"]

    if not symbols:
        parser.error("请指定至少一个币种，或使用 --all")

    logger.info(f"分析币种: {symbols}")
    logger.info(f"数据天数: {args.days}")

    if len(symbols) == 1:
        run_single_symbol(
            symbols[0],
            days=args.days,
            show_weights=args.weights,
            output_csv=args.output,
        )
    else:
        run_multi_symbol(
            symbols,
            days=args.days,
            output_csv=args.output,
        )


if __name__ == "__main__":
    main()
