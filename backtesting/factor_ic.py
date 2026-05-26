#!/usr/bin/env python3
"""
因子 IC/IR 批量分析框架
========================

对所有注册因子跑 IC（Information Coefficient）和 IR（Information Ratio）分析，
输出一份结构化报告，告诉你：
  - 哪些因子真的能预测未来收益（IC 显著）
  - 哪些因子预测力稳定（IR 高）
  - 哪些因子已衰减（该降权或移除）
  - 最优持仓周期是多少 bar（IC 衰减半衰期）

用法：
    from backtesting.factor_ic import FactorICAnalyzer

    analyzer = FactorICAnalyzer(ohlcv_df)
    report = analyzer.run()
    print(report.to_table())
    report.save_csv("factor_ic_report.csv")

依赖: numpy, pandas, signals.factors, signals.factor_ic
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("backtesting.factor_ic")


# ══════════════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class FactorICResult:
    """单个因子的 IC 分析结果"""
    factor_name: str
    category: str
    mean_ic: float              # IC 均值（越高预测力越强）
    ic_std: float               # IC 标准差
    ic_ir: float                # IR = mean_ic / ic_std（稳定性）
    best_horizon: int           # IC 最高的前瞻周期（bar 数）
    ic_at_1h: float             # 1h 前瞻 IC
    ic_at_4h: float             # 4h 前瞻 IC
    ic_at_8h: float             # 8h 前瞻 IC
    ic_at_24h: float            # 24h 前瞻 IC
    decay_half_life: float      # IC 衰减半衰期（bar）
    is_decaying: bool           # IC 是否在随时间衰减
    quality: str                # strong / moderate / weak / noise
    direction: str              # positive（因子高→收益高）/ negative（因子高→收益低）
    recommendation: str         # keep / reduce_weight / remove / monitor


@dataclass
class FactorICReport:
    """全量因子 IC 分析报告"""
    results: List[FactorICResult] = field(default_factory=list)
    total_factors: int = 0
    useful_factors: int = 0
    strong_factors: int = 0
    noise_factors: int = 0
    analysis_date: str = ""
    data_bars: int = 0
    elapsed_sec: float = 0.0

    def to_table(self, sort_by: str = "ic_ir") -> str:
        """输出格式化表格字符串"""
        if not self.results:
            return "无因子分析结果"

        # 排序
        sorted_results = sorted(
            self.results,
            key=lambda r: abs(getattr(r, sort_by, 0)),
            reverse=True,
        )

        lines = [
            "═" * 100,
            "因子 IC/IR 分析报告",
            f"数据量: {self.data_bars} bars | 因子数: {self.total_factors} | "
            f"有效: {self.useful_factors} | 强预测力: {self.strong_factors} | 噪音: {self.noise_factors}",
            "═" * 100,
            f"{'因子':<30} {'分类':<14} {'IC均值':>8} {'IR':>6} {'质量':<10} "
            f"{'最优周期':>8} {'半衰期':>8} {'衰减':>4} {'建议':<14}",
            "─" * 100,
        ]

        for r in sorted_results:
            decay_mark = "⚠️" if r.is_decaying else "  "
            ic_sign = "+" if r.mean_ic >= 0 else ""
            lines.append(
                f"{r.factor_name:<30} {r.category:<14} "
                f"{ic_sign}{r.mean_ic:>7.4f} {r.ic_ir:>5.2f}  {r.quality:<10} "
                f"{r.best_horizon:>6}h {r.decay_half_life:>7.1f}  {decay_mark} {r.recommendation:<14}"
            )

        lines.append("─" * 100)
        lines.append("")
        lines.append("解读指南：")
        lines.append("  IC均值: 因子值与未来收益的秩相关 (|IC|>0.05 有用, >0.07 强)")
        lines.append("  IR:     IC均值/IC标准差 (>1.0 非常稳定, >0.5 可用)")
        lines.append("  最优周期: IC 最高的前瞻 bar 数（做空策略推荐 4-8h）")
        lines.append("  半衰期: IC 衰减到一半的 bar 数（越长越好）")
        lines.append("  衰减⚠️: 该因子近期 IC 在下降，可能需要降权")
        lines.append("")

        return "\n".join(lines)

    def to_dataframe(self) -> pd.DataFrame:
        """转为 DataFrame 方便进一步分析"""
        if not self.results:
            return pd.DataFrame()

        rows = []
        for r in self.results:
            rows.append({
                "factor": r.factor_name,
                "category": r.category,
                "mean_ic": r.mean_ic,
                "ic_std": r.ic_std,
                "ic_ir": r.ic_ir,
                "best_horizon": r.best_horizon,
                "ic_1h": r.ic_at_1h,
                "ic_4h": r.ic_at_4h,
                "ic_8h": r.ic_at_8h,
                "ic_24h": r.ic_at_24h,
                "decay_half_life": r.decay_half_life,
                "is_decaying": r.is_decaying,
                "quality": r.quality,
                "direction": r.direction,
                "recommendation": r.recommendation,
            })
        return pd.DataFrame(rows).sort_values("ic_ir", key=abs, ascending=False)

    def save_csv(self, filepath: str) -> None:
        """保存为 CSV 文件"""
        df = self.to_dataframe()
        df.to_csv(filepath, index=False)
        logger.info(f"报告已保存: {filepath}")

    def get_weight_suggestions(self) -> Dict[str, float]:
        """
        基于 IC/IR 生成因子权重建议。

        返回 dict: factor_name -> suggested_weight (归一化到总和=1)
        只包含 quality != 'noise' 的因子。
        """
        weights: Dict[str, float] = {}
        for r in self.results:
            if r.quality == "noise":
                continue
            # 权重 = |IC| × |IR| × (1 - 0.3 if decaying else 1.0)
            w = abs(r.mean_ic) * max(abs(r.ic_ir), 0.1)
            if r.is_decaying:
                w *= 0.7
            weights[r.factor_name] = w

        # 归一化
        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total for k, v in weights.items()}

        return weights

    @property
    def summary(self) -> str:
        """一句话总结"""
        return (
            f"因子分析完成: {self.useful_factors}/{self.total_factors} 个因子有效 "
            f"(强={self.strong_factors}, 噪音={self.noise_factors}), "
            f"耗时 {self.elapsed_sec:.1f}s"
        )


# ══════════════════════════════════════════════════════════════════
#  分析器
# ══════════════════════════════════════════════════════════════════

class FactorICAnalyzer:
    """
    批量因子 IC/IR 分析器。

    读入 OHLCV 数据，计算所有注册因子，然后对每个因子运行完整的
    IC 分析（多周期衰减、时间稳定性、质量分级）。

    参数:
        ohlcv_df: DataFrame，列必须包含 [timestamp, open, high, low, close, volume]
        horizons: 前瞻周期列表（以 bar 为单位，1h 数据下 1 bar = 1h）
        ic_window: 滚动 IC 计算窗口
        period_days: IC 历史稳定性评估周期（天）
        external_data: 可选的外部数据（如 funding_rate Series）传给微观结构因子
        direction: 'short' 表示做空（因子高+收益负=好因子），'long' 表示做多
    """

    def __init__(
        self,
        ohlcv_df: pd.DataFrame,
        horizons: Optional[List[int]] = None,
        ic_window: int = 60,
        period_days: int = 30,
        external_data: Optional[Dict[str, pd.Series]] = None,
        direction: str = "short",
    ):
        self.df = ohlcv_df.copy()
        self.horizons = horizons or [1, 4, 8, 24]
        self.ic_window = ic_window
        self.period_days = period_days
        self.external_data = external_data
        self.direction = direction.lower()

    def run(self) -> FactorICReport:
        """执行完整分析，返回报告"""
        from signals.factors import FactorRegistry
        from signals.factor_ic import ICReport, compute_ic, compute_forward_returns

        t0 = time.time()
        report = FactorICReport()
        report.data_bars = len(self.df)
        report.analysis_date = pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        # 1. 计算所有因子
        registry = FactorRegistry(normalize=True, zscore_window=100)
        logger.info(f"计算 {len(registry.factor_names)} 个因子...")
        factor_df = registry.compute_all(self.df, external_data=self.external_data)

        # 2. 计算各周期前瞻收益
        prices = self.df["close"]
        fwd_returns = {}
        for h in self.horizons:
            fwd_returns[h] = compute_forward_returns(prices, horizon=h)
            # 做空策略：收益取反（价格跌=做空赚）
            if self.direction == "short":
                fwd_returns[h] = -fwd_returns[h]

        # 3. 对每个因子跑 IC 分析
        categories = registry.categories
        factor_to_category = {}
        for cat, names in categories.items():
            for name in names:
                factor_to_category[name] = cat

        for factor_name in factor_df.columns:
            try:
                result = self._analyze_single_factor(
                    factor_name=factor_name,
                    factor_values=factor_df[factor_name],
                    prices=prices,
                    fwd_returns=fwd_returns,
                    category=factor_to_category.get(factor_name, "unknown"),
                )
                report.results.append(result)
            except Exception as e:
                logger.warning(f"因子 '{factor_name}' 分析失败: {e}")

        # 4. 汇总统计
        report.total_factors = len(report.results)
        report.useful_factors = sum(1 for r in report.results if r.quality != "noise")
        report.strong_factors = sum(1 for r in report.results if r.quality == "strong")
        report.noise_factors = sum(1 for r in report.results if r.quality == "noise")
        report.elapsed_sec = time.time() - t0

        logger.info(report.summary)
        return report

    def _analyze_single_factor(
        self,
        factor_name: str,
        factor_values: pd.Series,
        prices: pd.Series,
        fwd_returns: Dict[int, pd.Series],
        category: str,
    ) -> FactorICResult:
        """分析单个因子"""
        from signals.factor_ic import (
            compute_ic, compute_ic_decay, compute_ic_history,
            _estimate_decay_half_life, _detect_ic_trend,
        )

        # IC at each horizon
        ic_by_horizon: Dict[int, float] = {}
        for h, ret in fwd_returns.items():
            ic_by_horizon[h] = compute_ic(factor_values, ret)

        # Best horizon
        best_h = max(ic_by_horizon, key=lambda h: abs(ic_by_horizon[h]))

        # Rolling IC at best horizon for mean/std
        best_ret = fwd_returns[best_h]
        aligned = pd.DataFrame({
            "factor": factor_values,
            "returns": best_ret,
        }).dropna()

        # Sliding window IC for IR calculation
        window_size = min(self.ic_window, max(20, len(aligned) // 4))
        step = max(1, window_size // 4)
        rolling_ics: List[float] = []

        for i in range(window_size, len(aligned), step):
            start = i - window_size
            w_factor = aligned["factor"].iloc[start:i]
            w_returns = aligned["returns"].iloc[start:i]
            rolling_ics.append(compute_ic(w_factor, w_returns))

        mean_ic = float(np.mean(rolling_ics)) if rolling_ics else 0.0
        ic_std = float(np.std(rolling_ics)) if rolling_ics else 1.0
        if ic_std < 1e-8:
            ic_std = 1.0
        ic_ir = mean_ic / ic_std

        # IC history (stability over time)
        ic_history = compute_ic_history(
            factor_values, prices,
            period_days=self.period_days,
            horizon=best_h,
        )
        # Adjust for short direction
        if self.direction == "short":
            ic_history = [-x for x in ic_history]

        is_decaying = _detect_ic_trend(ic_history)

        # Decay half-life
        # For short direction, we already negated fwd_returns, so ic_by_horizon is correct
        decay_half = _estimate_decay_half_life(ic_by_horizon)

        # Quality classification
        abs_ic = abs(mean_ic)
        abs_ir = abs(ic_ir)
        if abs_ic > 0.07 and abs_ir > 1.0:
            quality = "strong"
        elif abs_ic > 0.05 and abs_ir > 0.5:
            quality = "moderate"
        elif abs_ic > 0.03 and abs_ir > 0.3:
            quality = "weak"
        else:
            quality = "noise"

        # Direction interpretation
        direction = "positive" if mean_ic >= 0 else "negative"

        # Recommendation
        if quality == "noise":
            recommendation = "remove"
        elif is_decaying and quality == "weak":
            recommendation = "remove"
        elif is_decaying:
            recommendation = "reduce_weight"
        elif quality == "strong":
            recommendation = "keep"
        elif quality == "moderate":
            recommendation = "keep"
        else:
            recommendation = "monitor"

        return FactorICResult(
            factor_name=factor_name,
            category=category,
            mean_ic=mean_ic,
            ic_std=ic_std,
            ic_ir=ic_ir,
            best_horizon=best_h,
            ic_at_1h=ic_by_horizon.get(1, 0.0),
            ic_at_4h=ic_by_horizon.get(4, 0.0),
            ic_at_8h=ic_by_horizon.get(8, 0.0),
            ic_at_24h=ic_by_horizon.get(24, 0.0),
            decay_half_life=decay_half,
            is_decaying=is_decaying,
            quality=quality,
            direction=direction,
            recommendation=recommendation,
        )
