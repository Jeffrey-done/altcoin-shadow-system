#!/usr/bin/env python3
"""
多因子评分回测验证脚本

对比：
  1. 旧评分系统（RSI 4×25 硬编码）的历史表现
  2. 新多因子评分系统（30因子 IC 加权）的历史表现

核心问题：如果我们过去用新评分来过滤信号，胜率和盈亏会更好吗？

用法：
  python3 backtest_multifactor.py

输出：
  - 新旧评分的胜率对比
  - 各评级(A/B/C)的真实盈亏
  - 因子重要性排名
  - 建议是否切换
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from common import setup_logger

logger = setup_logger("backtest_multifactor")


# ══════════════════════════════════════════════════════════════════
#  数据获取
# ══════════════════════════════════════════════════════════════════

def fetch_historical_data(symbol: str, days: int = 90) -> pd.DataFrame:
    """从 Binance 拉取历史 1h K 线"""
    try:
        import requests
        sym = symbol.replace('/USDT', 'USDT').replace('/', '')
        limit = min(days * 24, 1000)
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": sym, "interval": "1h", "limit": limit},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            df = pd.DataFrame(data, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignore'
            ])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df['timestamp'] = pd.to_numeric(df['timestamp'])
            return df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].copy()
    except Exception as e:
        logger.error(f"获取 {symbol} 数据失败: {e}")
    return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════
#  回测逻辑
# ══════════════════════════════════════════════════════════════════

def run_multifactor_backtest(
    symbol: str,
    df: pd.DataFrame,
    rsi_entry_threshold: float = 75.0,
    rsi_drop_threshold: float = 10.0,
    forward_hours: int = 24,
):
    """
    对历史数据逐 bar 扫描信号，用多因子评分打分，检查 N 小时后的盈亏。

    逻辑：
      1. 找到所有 RSI 超买回落点（和你现有策略一样的入场逻辑）
      2. 对每个信号点，用新旧两套评分系统打分
      3. 看 forward_hours 小时后价格变化（做空视角：跌了=赚）
      4. 统计各评级的真实胜率

    返回:
      DataFrame 包含每个信号的双评分和实际盈亏
    """
    from signals.factors import FactorRegistry
    from signals.factor_scorer import FactorScorer

    registry = FactorRegistry()
    scorer = FactorScorer()

    closes = df['close'].values
    n = len(closes)

    if n < 100:
        logger.warning(f"{symbol}: 数据不足 ({n} bars)")
        return pd.DataFrame()

    # 计算 RSI（与你系统一致的 Wilder RSI）
    delta = pd.Series(closes).diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = (100.0 - (100.0 / (1.0 + rs))).values

    # 找信号点：RSI 曾达到超买 + 当前回落足够
    signals = []
    rsi_peak = pd.Series(rsi).rolling(window=10, min_periods=1).max().values
    cooldown = 0

    for i in range(50, n - forward_hours):
        if cooldown > 0:
            cooldown -= 1
            continue

        # 信号条件（与你 altcoin_scanner 一致）
        if (rsi_peak[i] >= rsi_entry_threshold and
                rsi[i] < 70 and
                (rsi_peak[i] - rsi[i]) >= rsi_drop_threshold):

            signals.append(i)
            cooldown = 24  # 冷却 24 bar

    if not signals:
        logger.info(f"{symbol}: 无信号触发")
        return pd.DataFrame()

    logger.info(f"{symbol}: 找到 {len(signals)} 个历史信号点")

    # 对每个信号点打分
    results = []
    for sig_idx in signals:
        # 取信号点之前的数据用于评分
        lookback = min(sig_idx + 1, 200)
        df_slice = df.iloc[sig_idx - lookback + 1:sig_idx + 1].reset_index(drop=True)

        if len(df_slice) < 50:
            continue

        # 多因子评分
        try:
            mf_result = scorer.score(df_slice, symbol=symbol)
        except Exception:
            mf_result = None

        # 计算实际盈亏（做空视角）
        entry_price = closes[sig_idx + 1]  # 下一根 bar 开盘入场（避免 look-ahead）
        exit_price = closes[min(sig_idx + forward_hours, n - 1)]
        pnl_pct = (entry_price - exit_price) / entry_price * 100  # 做空：跌了赚

        results.append({
            'symbol': symbol,
            'bar_index': sig_idx,
            'timestamp': df['timestamp'].iloc[sig_idx],
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl_pct': round(pnl_pct, 2),
            'is_win': pnl_pct > 0,
            'rsi_at_signal': round(rsi[sig_idx], 1),
            'rsi_peak': round(rsi_peak[sig_idx], 1),
            # 多因子评分
            'mf_score': round(mf_result.score, 1) if mf_result else 0,
            'mf_grade': mf_result.grade if mf_result else 'N/A',
            'mf_confidence': round(mf_result.confidence, 2) if mf_result else 0,
            'mf_agreement': round(mf_result.factor_agreement, 2) if mf_result else 0,
            'mf_n_factors': mf_result.n_factors_used if mf_result else 0,
            'mf_stake_mult': round(mf_result.recommended_stake_multiplier, 2) if mf_result else 1.0,
        })

    return pd.DataFrame(results)


# ══════════════════════════════════════════════════════════════════
#  报告生成
# ══════════════════════════════════════════════════════════════════

def generate_report(all_results: pd.DataFrame):
    """生成对比报告"""
    if all_results.empty:
        print("\n❌ 无结果数据")
        return

    n_total = len(all_results)
    n_wins = all_results['is_win'].sum()
    total_pnl = all_results['pnl_pct'].sum()

    print("\n")
    print("═" * 60)
    print("  多因子评分回测验证报告")
    print("═" * 60)
    print(f"\n  数据: {all_results['symbol'].nunique()} 币种, {n_total} 个信号")
    print(f"  评估窗口: 入场后 24h 盈亏（做空视角）")

    # === 总体表现 ===
    print(f"\n{'─'*60}")
    print(f"  总体: 胜率 {n_wins}/{n_total} = {n_wins/n_total*100:.1f}%")
    print(f"  总PnL: {total_pnl:+.1f}% (均值 {all_results['pnl_pct'].mean():+.2f}%/笔)")

    # === 按多因子评级分组 ===
    print(f"\n{'─'*60}")
    print("  按多因子评级分组:")
    print(f"  {'评级':<6} {'信号数':<8} {'胜率':<10} {'均PnL':<10} {'建议'}")
    print(f"  {'─'*50}")

    for grade in ['A', 'B', 'C', 'SKIP']:
        subset = all_results[all_results['mf_grade'] == grade]
        if subset.empty:
            continue
        n = len(subset)
        wins = subset['is_win'].sum()
        wr = wins / n * 100
        avg_pnl = subset['pnl_pct'].mean()

        if grade == 'A':
            advice = "✅ 全仓" if wr >= 55 else "⚠️ 需观察"
        elif grade == 'B':
            advice = "✅ 半仓" if wr >= 50 else "⚠️ 谨慎"
        elif grade == 'C':
            advice = "⚠️ 减仓" if wr >= 45 else "❌ 不开"
        else:
            advice = "❌ 跳过"

        print(f"  {grade:<6} {n:<8} {wr:.1f}%{'':<5} {avg_pnl:+.2f}%{'':<4} {advice}")

    # === 按评分区间 ===
    print(f"\n{'─'*60}")
    print("  按多因子评分区间:")
    print(f"  {'区间':<12} {'信号数':<8} {'胜率':<10} {'均PnL':<10}")
    print(f"  {'─'*45}")

    bins = [(0, 40), (40, 55), (55, 70), (70, 85), (85, 101)]
    labels = ['0-40', '40-55', '55-70', '70-85', '85-100']

    for (lo, hi), label in zip(bins, labels):
        subset = all_results[(all_results['mf_score'] >= lo) & (all_results['mf_score'] < hi)]
        if subset.empty:
            continue
        n = len(subset)
        wins = subset['is_win'].sum()
        wr = wins / n * 100
        avg_pnl = subset['pnl_pct'].mean()
        print(f"  {label:<12} {n:<8} {wr:.1f}%{'':<5} {avg_pnl:+.2f}%")

    # === 评分 vs 胜率相关性 ===
    if len(all_results) >= 10:
        corr = all_results['mf_score'].corr(all_results['pnl_pct'])
        print(f"\n{'─'*60}")
        print(f"  评分-盈亏相关性: {corr:.3f}", end="")
        if corr > 0.1:
            print("  ✅ 正相关（评分越高越赚钱）")
        elif corr > -0.05:
            print("  ⚠️ 无明显相关")
        else:
            print("  ❌ 负相关（评分可能有问题）")

    # === 置信度与胜率 ===
    if 'mf_confidence' in all_results.columns:
        high_conf = all_results[all_results['mf_confidence'] >= 0.5]
        low_conf = all_results[all_results['mf_confidence'] < 0.5]
        print(f"\n{'─'*60}")
        print("  按置信度:")
        if not high_conf.empty:
            print(f"    高置信(≥0.5): {len(high_conf)}笔, 胜率 {high_conf['is_win'].mean()*100:.1f}%")
        if not low_conf.empty:
            print(f"    低置信(<0.5): {len(low_conf)}笔, 胜率 {low_conf['is_win'].mean()*100:.1f}%")

    # === 最终建议 ===
    print(f"\n{'═'*60}")

    a_grade = all_results[all_results['mf_grade'] == 'A']
    skip_grade = all_results[all_results['mf_grade'] == 'SKIP']

    if not a_grade.empty and a_grade['is_win'].mean() > 0.6:
        print("  🏆 建议: 多因子评分有效！A 级信号胜率显著高于平均")
        print("     → 可以开始以观察模式接入实盘")
    elif not a_grade.empty and a_grade['pnl_pct'].mean() > all_results['pnl_pct'].mean():
        print("  ✅ 建议: 多因子评分有一定区分度，A 级盈利优于平均")
        print("     → 建议 blend 模式（70%新 + 30%旧）试运行")
    else:
        print("  ⚠️ 建议: 当前数据下多因子评分区分度不明显")
        print("     → 继续用旧评分，同时记录新评分数据等待更多样本")

    print("═" * 60)


# ══════════════════════════════════════════════════════════════════
#  主函数
# ══════════════════════════════════════════════════════════════════

def main():
    """运行多因子回测验证"""
    print("🔬 多因子评分回测验证")
    print("─" * 40)

    # 回测币种（与你 config/system.yaml 一致）
    symbols = [
        'PEPE/USDT', 'DOGE/USDT', 'SHIB/USDT', 'FLOKI/USDT',
        'BONK/USDT', 'WIF/USDT', 'PEOPLE/USDT', 'ORDI/USDT',
    ]

    days = 60  # 回测 60 天
    all_results = []

    for symbol in symbols:
        print(f"\n  📡 获取 {symbol} ({days}天)...", end=" ")
        df = fetch_historical_data(symbol, days=days)

        if df.empty:
            print("❌ 无数据")
            continue

        print(f"✅ {len(df)} bars")
        result = run_multifactor_backtest(symbol, df)

        if not result.empty:
            all_results.append(result)
            wins = result['is_win'].sum()
            print(f"     → {len(result)} 信号, 胜率 {wins/len(result)*100:.0f}%")

    if all_results:
        combined = pd.concat(all_results, ignore_index=True)
        generate_report(combined)

        # 保存详细结果
        output_file = os.path.join(os.path.dirname(__file__), 'multifactor_backtest_results.csv')
        combined.to_csv(output_file, index=False)
        print(f"\n  📁 详细结果已保存: {output_file}")
    else:
        print("\n  ❌ 所有币种均无有效信号")


if __name__ == '__main__':
    main()
