#!/usr/bin/env python3
"""
增强蒙特卡洛模拟 v2.0

在原有"交易顺序打乱"基础上，新增两种关键模拟模式：

1. 参数扰动 MC (Parameter Perturbation)
   - 给最优参数加随机噪声 → 评估参数对微小变化的敏感度
   - 如果扰动 ±10% 就导致 Sharpe 崩溃 → 说明参数过拟合

2. Bootstrap 重采样 (Bootstrap Resampling)
   - 有放回抽样交易序列 → 评估指标的统计置信区间
   - 输出 Sharpe/PnL/胜率 的 95% 置信区间

3. 综合稳健性评分
   - 结合三种 MC 结果，给出 0~100 策略稳健性评分

用法：
  from backtesting.monte_carlo_enhanced import (
      run_parameter_perturbation,
      run_bootstrap_resampling,
      run_robustness_assessment,
  )

  # 参数扰动测试
  perturb_result = run_parameter_perturbation(
      ohlcv_data=df,
      base_params={'tp1_pct': 5, 'hard_stop_pct': 5, ...},
      perturbation_pct=0.1,
      n_simulations=200,
  )
  print(f"参数稳定性评分: {perturb_result.stability_score:.0f}/100")

  # Bootstrap 置信区间
  boot_result = run_bootstrap_resampling(
      trade_pnls=[1.2, -0.5, 2.1, ...],
      n_bootstrap=5000,
  )
  print(f"Sharpe 95% CI: [{boot_result.sharpe_ci_lower:.2f}, {boot_result.sharpe_ci_upper:.2f}]")

  # 综合稳健性评估
  robustness = run_robustness_assessment(
      ohlcv_data=df,
      base_params=params,
      trade_pnls=pnls,
  )
  print(f"综合稳健性: {robustness.overall_score:.0f}/100 (Grade {robustness.grade})")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("backtesting.monte_carlo_enhanced")


# ══════════════════════════════════════════════════════════════════
#  参数扰动 MC
# ══════════════════════════════════════════════════════════════════

@dataclass
class PerturbationResult:
    """参数扰动模拟结果"""
    n_simulations: int = 0
    base_sharpe: float = 0.0
    base_pnl: float = 0.0
    perturbation_pct: float = 0.1

    # Sharpe 分布
    sharpe_mean: float = 0.0
    sharpe_std: float = 0.0
    sharpe_p5: float = 0.0
    sharpe_p95: float = 0.0
    sharpe_min: float = 0.0

    # PnL 分布
    pnl_mean: float = 0.0
    pnl_std: float = 0.0
    pnl_p5: float = 0.0
    pnl_positive_pct: float = 0.0      # 扰动后仍盈利的比例

    # 稳定性评分 (0~100)
    stability_score: float = 0.0

    # 哪些参数最敏感
    sensitivity_ranking: Dict[str, float] = field(default_factory=dict)

    # 原始数据
    all_sharpes: Optional[np.ndarray] = None
    all_pnls: Optional[np.ndarray] = None

    @property
    def summary(self) -> str:
        return (
            f"参数扰动 MC ({self.n_simulations} sims, ±{self.perturbation_pct*100:.0f}%)\n"
            f"{'─'*50}\n"
            f"基线 Sharpe: {self.base_sharpe:.2f} → 扰动后: "
            f"{self.sharpe_mean:.2f} ± {self.sharpe_std:.2f}\n"
            f"Sharpe P5~P95: [{self.sharpe_p5:.2f}, {self.sharpe_p95:.2f}]\n"
            f"PnL 仍为正: {self.pnl_positive_pct:.0f}%\n"
            f"稳定性评分: {self.stability_score:.0f}/100\n"
            f"敏感度排名: {self._fmt_sensitivity()}"
        )

    def _fmt_sensitivity(self) -> str:
        if not self.sensitivity_ranking:
            return "N/A"
        sorted_items = sorted(self.sensitivity_ranking.items(), key=lambda x: -x[1])
        return ", ".join(f"{k}={v:.2f}" for k, v in sorted_items[:5])


def run_parameter_perturbation(
    ohlcv_data: pd.DataFrame,
    base_params: Dict[str, Any],
    perturbation_pct: float = 0.1,
    n_simulations: int = 200,
    symbol: str = '',
    seed: Optional[int] = None,
    bt_config: Optional[Dict[str, Any]] = None,
) -> PerturbationResult:
    """
    参数扰动蒙特卡洛。

    给每个参数加 ±perturbation_pct 的随机噪声，观察回测结果的变化。

    参数:
      ohlcv_data: K 线数据
      base_params: 基线参数
      perturbation_pct: 扰动幅度 (0.1 = ±10%)
      n_simulations: 模拟次数
      symbol: 币种
      seed: 随机种子

    返回:
      PerturbationResult
    """
    result = PerturbationResult(
        n_simulations=n_simulations,
        perturbation_pct=perturbation_pct,
    )

    rng = np.random.default_rng(seed)

    # 运行基线
    base_metrics = _run_backtest_with_params(ohlcv_data, base_params, symbol, bt_config)
    result.base_sharpe = base_metrics.get('sharpe_ratio', 0)
    result.base_pnl = base_metrics.get('total_pnl', 0)

    # 识别可扰动的数值参数
    numeric_params = {k: v for k, v in base_params.items() if isinstance(v, (int, float))}
    if not numeric_params:
        return result

    # 批量扰动
    sharpes = []
    pnls = []
    # 用于敏感度分析：每个参数单独扰动的影响
    param_impacts = {k: [] for k in numeric_params}

    for i in range(n_simulations):
        # 全参数同时扰动
        perturbed = {}
        for k, v in base_params.items():
            if k in numeric_params and v != 0:
                noise = rng.uniform(-perturbation_pct, perturbation_pct)
                new_val = v * (1 + noise)
                # 保持整数类型
                if isinstance(v, int):
                    new_val = int(round(new_val))
                else:
                    new_val = round(new_val, 2)
                perturbed[k] = new_val
            else:
                perturbed[k] = v

        metrics = _run_backtest_with_params(ohlcv_data, perturbed, symbol, bt_config)
        sharpes.append(metrics.get('sharpe_ratio', 0))
        pnls.append(metrics.get('total_pnl', 0))

    # 单参数敏感度分析（每个参数独立扰动 20 次）
    for param_key, param_val in numeric_params.items():
        if param_val == 0:
            continue
        for _ in range(20):
            single_perturbed = dict(base_params)
            noise = rng.uniform(-perturbation_pct, perturbation_pct)
            new_val = param_val * (1 + noise)
            if isinstance(param_val, int):
                new_val = int(round(new_val))
            single_perturbed[param_key] = new_val

            m = _run_backtest_with_params(ohlcv_data, single_perturbed, symbol, bt_config)
            impact = abs(m.get('sharpe_ratio', 0) - result.base_sharpe)
            param_impacts[param_key].append(impact)

    # 汇总
    sharpes_arr = np.array(sharpes)
    pnls_arr = np.array(pnls)

    result.sharpe_mean = round(float(sharpes_arr.mean()), 3)
    result.sharpe_std = round(float(sharpes_arr.std()), 3)
    result.sharpe_p5 = round(float(np.percentile(sharpes_arr, 5)), 3)
    result.sharpe_p95 = round(float(np.percentile(sharpes_arr, 95)), 3)
    result.sharpe_min = round(float(sharpes_arr.min()), 3)

    result.pnl_mean = round(float(pnls_arr.mean()), 2)
    result.pnl_std = round(float(pnls_arr.std()), 2)
    result.pnl_p5 = round(float(np.percentile(pnls_arr, 5)), 2)
    result.pnl_positive_pct = round(float((pnls_arr > 0).mean() * 100), 1)

    # 敏感度排名
    for k, impacts in param_impacts.items():
        if impacts:
            result.sensitivity_ranking[k] = round(float(np.mean(impacts)), 3)

    # 稳定性评分
    result.stability_score = _compute_stability_score(result)
    result.all_sharpes = sharpes_arr
    result.all_pnls = pnls_arr

    return result


def _compute_stability_score(result: PerturbationResult) -> float:
    """计算参数稳定性评分 (0~100)"""
    score = 0.0

    # 1. Sharpe 保持率 (0~40): 扰动后均值/基线
    if result.base_sharpe > 0:
        retention = result.sharpe_mean / result.base_sharpe
        score += min(40, max(0, retention * 40))

    # 2. PnL 正比例 (0~30)
    score += min(30, result.pnl_positive_pct * 0.3)

    # 3. Sharpe 窄幅波动 (0~30): std 越小越好
    if result.base_sharpe > 0:
        cv = result.sharpe_std / abs(result.base_sharpe)
        stability = max(0, 1 - cv * 2)  # CV>0.5 → 0分
        score += stability * 30

    return round(min(100, score), 1)


# ══════════════════════════════════════════════════════════════════
#  Bootstrap 重采样
# ══════════════════════════════════════════════════════════════════

@dataclass
class BootstrapResult:
    """Bootstrap 重采样结果"""
    n_bootstrap: int = 0
    n_trades: int = 0

    # Sharpe 置信区间
    sharpe_mean: float = 0.0
    sharpe_ci_lower: float = 0.0       # 95% CI 下界
    sharpe_ci_upper: float = 0.0       # 95% CI 上界
    sharpe_significant: bool = False   # CI 下界 > 0 → 统计显著

    # PnL 置信区间
    pnl_mean: float = 0.0
    pnl_ci_lower: float = 0.0
    pnl_ci_upper: float = 0.0
    pnl_significant: bool = False

    # 胜率置信区间
    win_rate_mean: float = 0.0
    win_rate_ci_lower: float = 0.0
    win_rate_ci_upper: float = 0.0

    # 最大回撤置信区间
    max_dd_mean: float = 0.0
    max_dd_ci_upper: float = 0.0       # 95% 分位（最坏估计）

    # Profit Factor 置信区间
    pf_mean: float = 0.0
    pf_ci_lower: float = 0.0

    @property
    def summary(self) -> str:
        sig_mark = "✅" if self.sharpe_significant else "❌"
        return (
            f"Bootstrap 重采样 ({self.n_bootstrap} sims, {self.n_trades} trades)\n"
            f"{'─'*50}\n"
            f"Sharpe 95% CI: [{self.sharpe_ci_lower:.2f}, {self.sharpe_ci_upper:.2f}] "
            f"{sig_mark} {'显著>0' if self.sharpe_significant else '不显著'}\n"
            f"PnL 95% CI:    [{self.pnl_ci_lower:.1f}, {self.pnl_ci_upper:.1f}] U\n"
            f"胜率 95% CI:   [{self.win_rate_ci_lower:.1f}%, {self.win_rate_ci_upper:.1f}%]\n"
            f"Max DD P95:    {self.max_dd_ci_upper:.1f}%\n"
            f"PF 95% CI下界: {self.pf_ci_lower:.2f}"
        )


def run_bootstrap_resampling(
    trade_pnls: List[float],
    n_bootstrap: int = 5000,
    initial_capital: float = 1000.0,
    confidence_level: float = 0.95,
    seed: Optional[int] = None,
) -> BootstrapResult:
    """
    Bootstrap 有放回重采样。

    与原 Monte Carlo 的区别：
      - 原 MC：打乱顺序（同一组交易的不同排列）
      - Bootstrap：有放回抽样（模拟"如果交易样本不同"的情况）

    用途：评估指标的统计显著性和置信区间。

    参数:
      trade_pnls: 交易 PnL 序列
      n_bootstrap: 重采样次数
      initial_capital: 初始本金
      confidence_level: 置信水平 (默认 95%)
      seed: 随机种子

    返回:
      BootstrapResult
    """
    result = BootstrapResult(
        n_bootstrap=n_bootstrap,
        n_trades=len(trade_pnls),
    )

    if not trade_pnls or len(trade_pnls) < 5:
        return result

    rng = np.random.default_rng(seed)
    pnls = np.array(trade_pnls, dtype=np.float64)
    n = len(pnls)
    alpha = 1 - confidence_level

    # 批量 Bootstrap 采样
    # shape: (n_bootstrap, n)
    indices = rng.integers(0, n, size=(n_bootstrap, n))
    samples = pnls[indices]

    # ── Sharpe 分布 ──
    means = samples.mean(axis=1)
    stds = samples.std(axis=1, ddof=1)
    valid = stds > 1e-10
    sharpes = np.zeros(n_bootstrap)
    sharpes[valid] = means[valid] / stds[valid] * np.sqrt(n)

    result.sharpe_mean = round(float(sharpes.mean()), 3)
    result.sharpe_ci_lower = round(float(np.percentile(sharpes, alpha / 2 * 100)), 3)
    result.sharpe_ci_upper = round(float(np.percentile(sharpes, (1 - alpha / 2) * 100)), 3)
    result.sharpe_significant = result.sharpe_ci_lower > 0

    # ── PnL 分布 ──
    total_pnls = samples.sum(axis=1)
    result.pnl_mean = round(float(total_pnls.mean()), 2)
    result.pnl_ci_lower = round(float(np.percentile(total_pnls, alpha / 2 * 100)), 2)
    result.pnl_ci_upper = round(float(np.percentile(total_pnls, (1 - alpha / 2) * 100)), 2)
    result.pnl_significant = result.pnl_ci_lower > 0

    # ── 胜率分布 ──
    win_rates = (samples > 0).mean(axis=1) * 100
    result.win_rate_mean = round(float(win_rates.mean()), 1)
    result.win_rate_ci_lower = round(float(np.percentile(win_rates, alpha / 2 * 100)), 1)
    result.win_rate_ci_upper = round(float(np.percentile(win_rates, (1 - alpha / 2) * 100)), 1)

    # ── 最大回撤分布 ──
    equity = np.cumsum(samples, axis=1) + initial_capital
    equity_full = np.column_stack([np.full(n_bootstrap, initial_capital), equity])
    peaks = np.maximum.accumulate(equity_full, axis=1)
    drawdowns = (peaks - equity_full) / peaks * 100
    max_dds = drawdowns.max(axis=1)
    result.max_dd_mean = round(float(max_dds.mean()), 1)
    result.max_dd_ci_upper = round(float(np.percentile(max_dds, (1 - alpha / 2) * 100)), 1)

    # ── Profit Factor 分布 ──
    gross_profits = np.where(samples > 0, samples, 0).sum(axis=1)
    gross_losses = np.abs(np.where(samples < 0, samples, 0).sum(axis=1))
    valid_pf = gross_losses > 0
    pfs = np.zeros(n_bootstrap)
    pfs[valid_pf] = gross_profits[valid_pf] / gross_losses[valid_pf]
    result.pf_mean = round(float(pfs[valid_pf].mean()), 2) if valid_pf.any() else 0
    result.pf_ci_lower = round(float(np.percentile(pfs[valid_pf], alpha / 2 * 100)), 2) if valid_pf.any() else 0

    return result


# ══════════════════════════════════════════════════════════════════
#  综合稳健性评估
# ══════════════════════════════════════════════════════════════════

@dataclass
class RobustnessAssessment:
    """综合稳健性评估结果"""
    # 三种 MC 的分项得分
    permutation_score: float = 0.0      # 原版 MC（交易顺序打乱）
    perturbation_score: float = 0.0     # 参数扰动
    bootstrap_score: float = 0.0        # Bootstrap

    # 综合评分
    overall_score: float = 0.0          # 0~100
    grade: str = 'D'                    # A/B/C/D

    # 关键发现
    findings: List[str] = field(default_factory=list)

    # 详细结果
    bootstrap_result: Optional[BootstrapResult] = None
    perturbation_result: Optional[PerturbationResult] = None

    @property
    def summary(self) -> str:
        return (
            f"综合稳健性评估\n"
            f"{'═'*50}\n"
            f"总分: {self.overall_score:.0f}/100 (Grade {self.grade})\n"
            f"{'─'*50}\n"
            f"  顺序打乱 MC:  {self.permutation_score:.0f}/100\n"
            f"  参数扰动 MC:  {self.perturbation_score:.0f}/100\n"
            f"  Bootstrap CI: {self.bootstrap_score:.0f}/100\n"
            f"{'─'*50}\n"
            f"关键发现:\n" +
            "\n".join(f"  • {f}" for f in self.findings)
        )


def run_robustness_assessment(
    ohlcv_data: pd.DataFrame,
    base_params: Dict[str, Any],
    trade_pnls: List[float],
    symbol: str = '',
    initial_capital: float = 1000.0,
    seed: Optional[int] = None,
    bt_config: Optional[Dict[str, Any]] = None,
) -> RobustnessAssessment:
    """
    综合稳健性评估。

    运行三种 MC 模拟 + 综合评分。

    参数:
      ohlcv_data: K 线数据
      base_params: 基线参数
      trade_pnls: 回测产生的交易 PnL 序列
      symbol: 币种
      initial_capital: 初始本金

    返回:
      RobustnessAssessment
    """
    assessment = RobustnessAssessment()

    # 1. 原版 MC（交易顺序打乱）
    from backtesting.monte_carlo import run_monte_carlo
    mc_result = run_monte_carlo(
        trade_pnls=trade_pnls,
        n_simulations=5000,
        initial_capital=initial_capital,
        seed=seed,
    )

    # 评分：破产概率低 + Sharpe 稳定
    perm_score = 100
    perm_score -= mc_result.ruin_probability * 200  # 破产率每1%扣2分
    perm_score -= max(0, mc_result.max_drawdown_p95 - 30)  # P95回撤>30%扣分
    if mc_result.sharpe_p5 < 0:
        perm_score -= 30  # P5 Sharpe 为负，严重扣分
    assessment.permutation_score = round(max(0, min(100, perm_score)), 1)

    # 2. 参数扰动 MC
    if ohlcv_data is not None and base_params:
        perturb = run_parameter_perturbation(
            ohlcv_data=ohlcv_data,
            base_params=base_params,
            perturbation_pct=0.1,
            n_simulations=100,
            symbol=symbol,
            seed=seed,
            bt_config=bt_config,
        )
        assessment.perturbation_score = perturb.stability_score
        assessment.perturbation_result = perturb

        if perturb.pnl_positive_pct < 60:
            assessment.findings.append(
                f"⚠️ 参数扰动 ±10% 后仅 {perturb.pnl_positive_pct:.0f}% 仍盈利，参数过度拟合风险高"
            )
        if perturb.stability_score >= 70:
            assessment.findings.append(
                f"✅ 参数稳定性良好 ({perturb.stability_score:.0f}/100)"
            )
    else:
        assessment.perturbation_score = 50  # 无数据时中性

    # 3. Bootstrap
    if trade_pnls and len(trade_pnls) >= 5:
        boot = run_bootstrap_resampling(
            trade_pnls=trade_pnls,
            n_bootstrap=5000,
            initial_capital=initial_capital,
            seed=seed,
        )
        assessment.bootstrap_result = boot

        boot_score = 0
        if boot.sharpe_significant:
            boot_score += 40
            assessment.findings.append(
                f"✅ Sharpe 统计显著 (95% CI: [{boot.sharpe_ci_lower:.2f}, {boot.sharpe_ci_upper:.2f}])"
            )
        else:
            assessment.findings.append(
                f"❌ Sharpe 不显著 (95% CI 包含 0: [{boot.sharpe_ci_lower:.2f}, {boot.sharpe_ci_upper:.2f}])"
            )

        if boot.pnl_significant:
            boot_score += 30
        if boot.pf_ci_lower > 1.0:
            boot_score += 30
            assessment.findings.append(
                f"✅ Profit Factor 95% CI 下界 > 1.0 ({boot.pf_ci_lower:.2f})"
            )
        else:
            boot_score += max(0, boot.pf_ci_lower * 15)

        assessment.bootstrap_score = round(min(100, boot_score), 1)
    else:
        assessment.bootstrap_score = 0
        assessment.findings.append("⚠️ 交易样本不足，Bootstrap 不可靠")

    # 综合评分（加权平均）
    assessment.overall_score = round(
        assessment.permutation_score * 0.3 +
        assessment.perturbation_score * 0.4 +
        assessment.bootstrap_score * 0.3,
        1
    )

    # 等级
    if assessment.overall_score >= 75:
        assessment.grade = 'A'
    elif assessment.overall_score >= 55:
        assessment.grade = 'B'
    elif assessment.overall_score >= 35:
        assessment.grade = 'C'
    else:
        assessment.grade = 'D'

    # 额外发现
    if mc_result.ruin_probability > 0.05:
        assessment.findings.append(
            f"🔴 破产概率 {mc_result.ruin_probability*100:.1f}% (>5%警戒线)"
        )

    return assessment


# ══════════════════════════════════════════════════════════════════
#  辅助函数
# ══════════════════════════════════════════════════════════════════

def _run_backtest_with_params(
    ohlcv_data: pd.DataFrame,
    params: Dict[str, Any],
    symbol: str = '',
    bt_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """运行单次回测，返回指标字典"""
    try:
        from backtesting.engine import VectorizedBacktester, BacktestConfig
        from strategies.short_overbought import ShortOverboughtStrategy

        cfg_dict = bt_config or {}
        config = BacktestConfig(
            initial_capital=cfg_dict.get('initial_capital', 1000.0),
            fee_pct=cfg_dict.get('fee_pct', 0.04),
            max_open_trades=cfg_dict.get('max_open_trades', 3),
        )

        strategy = ShortOverboughtStrategy()
        bt = VectorizedBacktester(
            strategy=strategy,
            ohlcv_data=ohlcv_data,
            config=config,
            symbol=symbol,
        )
        result = bt.run(params=params)

        return {
            'total_trades': result.metrics.total_trades,
            'win_rate': result.metrics.win_rate,
            'total_pnl': result.metrics.total_pnl,
            'sharpe_ratio': result.metrics.sharpe_ratio,
            'max_drawdown_pct': result.metrics.max_drawdown_pct,
            'profit_factor': result.metrics.profit_factor,
        }
    except Exception as e:
        logger.debug(f"回测失败: {e}")
        return {'sharpe_ratio': 0, 'total_pnl': 0, 'total_trades': 0}
