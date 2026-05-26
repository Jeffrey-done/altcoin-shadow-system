#!/usr/bin/env python3
"""
Portfolio VaR/CVaR 风险约束
============================

计算当前持仓组合的尾部风险（Value at Risk / Conditional VaR），
超过阈值时拒绝新开仓，防止极端行情下组合爆仓。

核心指标：
  - VaR(95%): 95% 置信度下，组合一天内最大预期亏损
  - CVaR(95%): 超过 VaR 时的平均亏损（尾部风险更保守）
  - Concentration: 单币种占比（超过 50% 发出警告）

约束规则：
  - portfolio_cvar > account_balance × max_cvar_pct → 拒绝开仓
  - 新增仓位后 VaR 增量 > 单笔最大容忍 → 拒绝开仓

用法：
  from risk.portfolio_var import check_portfolio_risk, PortfolioRiskResult

  result = check_portfolio_risk(
      open_trades=open_trades,
      new_stake=33,
      new_symbol='PEPE/USDT',
      account_balance=100,
  )
  if not result.allowed:
      print(f"拒绝开仓: {result.reason}")

依赖：numpy（无需 scipy）
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger("risk.portfolio_var")


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class PortfolioVaRConfig:
    """组合风险配置"""
    confidence_level: float = 0.95     # VaR 置信度
    max_portfolio_cvar_pct: float = 0.30  # 组合 CVaR 不超过账户余额的 30%
    max_single_var_pct: float = 0.15   # 单笔新增 VaR 不超过余额的 15%
    max_concentration_pct: float = 0.50  # 单币种最大集中度 50%
    lookback_hours: int = 168          # 用过去 7 天的历史波动率
    correlation_assumption: float = 0.3  # 币种间相关性假设（简化，不需要协方差矩阵）
    enabled: bool = True               # 总开关


# 全局默认配置
_DEFAULT_CONFIG = PortfolioVaRConfig()


# ══════════════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class PortfolioRiskResult:
    """组合风险检查结果"""
    allowed: bool = True
    reason: str = "OK"

    # 风险指标
    portfolio_var: float = 0.0          # 组合 VaR (USDT)
    portfolio_cvar: float = 0.0         # 组合 CVaR (USDT)
    portfolio_var_pct: float = 0.0      # VaR / account_balance
    portfolio_cvar_pct: float = 0.0     # CVaR / account_balance

    # 新增仓位的边际风险
    marginal_var: float = 0.0           # 新仓位带来的 VaR 增量
    marginal_var_pct: float = 0.0       # 边际 VaR / account_balance

    # 集中度
    max_concentration: float = 0.0      # 最大单币种占比
    concentration_symbol: str = ""      # 最集中的币种

    # 详情
    n_positions: int = 0
    total_exposure: float = 0.0         # 总敞口 (notional)


# ══════════════════════════════════════════════════════════════════
#  VaR 计算（参数法，简化版）
# ══════════════════════════════════════════════════════════════════

def _estimate_daily_vol(symbol: str, leverage: int = 10) -> float:
    """
    估算币种的日波动率（%）。

    简化方法：用经验值（小币种日均波动 5-15%）。
    生产环境应接入历史数据计算。

    Returns:
        日波动率百分比（如 8.0 = 8%/天）
    """
    # 经验分级：
    #   meme 小币（PEPE/DOGE/SHIB 等）：日均 8-15%
    #   中盘币（SUI/ARB/SEI 等）：日均 5-8%
    #   大盘币（BTC/ETH）：日均 2-4%
    symbol_upper = symbol.upper()

    meme_tokens = {'PEPE', 'DOGE', 'SHIB', 'FLOKI', 'BONK', 'WIF', 'PEOPLE', 'ORDI'}
    base = symbol_upper.split('/')[0] if '/' in symbol_upper else symbol_upper.replace('USDT', '')

    if base in meme_tokens:
        daily_vol = 10.0  # Meme 币高波动
    elif base in ('BTC', 'ETH'):
        daily_vol = 3.0
    else:
        daily_vol = 7.0  # 中盘币默认

    return daily_vol


def _parametric_var(
    notional: float,
    daily_vol_pct: float,
    confidence: float = 0.95,
    holding_period_days: float = 1.0,
) -> float:
    """
    参数法 VaR 计算（假设正态分布）。

    VaR = notional × z_score × daily_vol × sqrt(holding_period)

    Args:
        notional: 名义敞口 (USDT)
        daily_vol_pct: 日波动率 %
        confidence: 置信度
        holding_period_days: 持仓天数

    Returns:
        VaR 金额 (USDT)
    """
    # Z-score 查表（避免依赖 scipy）
    z_table = {
        0.90: 1.282,
        0.95: 1.645,
        0.99: 2.326,
    }
    z = z_table.get(confidence, 1.645)

    vol_decimal = daily_vol_pct / 100.0
    var = notional * z * vol_decimal * np.sqrt(holding_period_days)
    return float(var)


def _parametric_cvar(
    notional: float,
    daily_vol_pct: float,
    confidence: float = 0.95,
    holding_period_days: float = 1.0,
) -> float:
    """
    参数法 CVaR（Expected Shortfall）。

    CVaR ≈ VaR × (pdf(z) / (1-confidence)) 对正态分布的近似
    简化：CVaR ≈ VaR × 1.4（经验系数）
    """
    var = _parametric_var(notional, daily_vol_pct, confidence, holding_period_days)
    return var * 1.4  # 正态分布下 CVaR/VaR ≈ 1.4 at 95%


# ══════════════════════════════════════════════════════════════════
#  组合风险检查
# ══════════════════════════════════════════════════════════════════

def check_portfolio_risk(
    open_trades: List[Dict],
    new_stake: float = 0,
    new_symbol: str = "",
    new_leverage: int = 10,
    account_balance: float = 100,
    config: Optional[PortfolioVaRConfig] = None,
) -> PortfolioRiskResult:
    """
    检查当前组合风险 + 新仓位的边际风险。

    Args:
        open_trades: 当前未平仓交易列表（dict 格式，含 symbol/stake/leverage/notional）
        new_stake: 拟新增的保证金
        new_symbol: 拟新增的币种
        new_leverage: 拟新增的杠杆
        account_balance: 账户余额
        config: 风险配置

    Returns:
        PortfolioRiskResult
    """
    cfg = config or _DEFAULT_CONFIG
    result = PortfolioRiskResult()

    if not cfg.enabled:
        result.allowed = True
        result.reason = "Portfolio VaR 检查已禁用"
        return result

    # 1. 收集现有持仓
    positions: List[Dict] = []
    for t in open_trades:
        if t.get('status') != 'open':
            continue
        positions.append({
            'symbol': t.get('symbol', ''),
            'notional': float(t.get('notional', 0) or (t.get('stake', 0) * t.get('leverage', 10))),
            'stake': float(t.get('stake_remaining', t.get('stake', 0))),
        })

    result.n_positions = len(positions)
    result.total_exposure = sum(p['notional'] for p in positions)

    # 2. 计算组合 VaR（假设各币种间有 correlation_assumption 的相关性）
    individual_vars = []
    symbol_exposure: Dict[str, float] = {}

    for p in positions:
        vol = _estimate_daily_vol(p['symbol'])
        var_i = _parametric_var(p['notional'], vol, cfg.confidence_level)
        individual_vars.append(var_i)
        symbol_exposure[p['symbol']] = symbol_exposure.get(p['symbol'], 0) + p['stake']

    # 组合 VaR（考虑相关性的简化公式）
    # portfolio_var = sqrt(sum(var_i^2) + 2*rho*sum(var_i*var_j for i<j))
    if individual_vars:
        sum_var_sq = sum(v ** 2 for v in individual_vars)
        cross_terms = 0.0
        for i in range(len(individual_vars)):
            for j in range(i + 1, len(individual_vars)):
                cross_terms += individual_vars[i] * individual_vars[j]
        portfolio_var = np.sqrt(sum_var_sq + 2 * cfg.correlation_assumption * cross_terms)
    else:
        portfolio_var = 0.0

    portfolio_cvar = portfolio_var * 1.4  # 近似

    result.portfolio_var = round(portfolio_var, 2)
    result.portfolio_cvar = round(portfolio_cvar, 2)
    result.portfolio_var_pct = round(portfolio_var / max(account_balance, 1) * 100, 1)
    result.portfolio_cvar_pct = round(portfolio_cvar / max(account_balance, 1) * 100, 1)

    # 3. 集中度检查
    total_stake = sum(symbol_exposure.values()) + new_stake
    if total_stake > 0:
        # 加入新仓位后的集中度
        if new_symbol:
            symbol_exposure[new_symbol] = symbol_exposure.get(new_symbol, 0) + new_stake
        max_sym = max(symbol_exposure, key=symbol_exposure.get) if symbol_exposure else ""
        max_conc = symbol_exposure.get(max_sym, 0) / total_stake if total_stake > 0 else 0
        result.max_concentration = round(max_conc, 2)
        result.concentration_symbol = max_sym

    # 4. 计算新仓位的边际 VaR
    if new_stake > 0 and new_symbol:
        new_notional = new_stake * new_leverage
        new_vol = _estimate_daily_vol(new_symbol)
        marginal_var = _parametric_var(new_notional, new_vol, cfg.confidence_level)
        result.marginal_var = round(marginal_var, 2)
        result.marginal_var_pct = round(marginal_var / max(account_balance, 1) * 100, 1)

    # 5. 风险约束检查
    # 检查1: 组合 CVaR 不超过余额的 max_portfolio_cvar_pct
    new_total_cvar = portfolio_cvar
    if new_stake > 0 and new_symbol:
        new_notional = new_stake * new_leverage
        new_vol = _estimate_daily_vol(new_symbol)
        new_var = _parametric_var(new_notional, new_vol, cfg.confidence_level)
        # 简化：新增后的组合 VaR ≈ sqrt(old^2 + new^2 + 2*rho*old*new)
        new_portfolio_var = np.sqrt(
            portfolio_var ** 2 + new_var ** 2 + 2 * cfg.correlation_assumption * portfolio_var * new_var
        )
        new_total_cvar = new_portfolio_var * 1.4

    max_cvar_limit = account_balance * cfg.max_portfolio_cvar_pct
    if new_total_cvar > max_cvar_limit:
        result.allowed = False
        result.reason = (
            f"组合 CVaR 超限: 新增后 CVaR={new_total_cvar:.1f}U > "
            f"上限{max_cvar_limit:.1f}U ({cfg.max_portfolio_cvar_pct*100:.0f}%×{account_balance:.0f}U)"
        )
        logger.warning(f"🚫 Portfolio VaR: {result.reason}")
        return result

    # 检查2: 单笔边际 VaR 不超过 max_single_var_pct
    if result.marginal_var > 0:
        max_single_limit = account_balance * cfg.max_single_var_pct
        if result.marginal_var > max_single_limit:
            result.allowed = False
            result.reason = (
                f"单笔边际 VaR 超限: {new_symbol} VaR={result.marginal_var:.1f}U > "
                f"上限{max_single_limit:.1f}U ({cfg.max_single_var_pct*100:.0f}%×{account_balance:.0f}U)"
            )
            logger.warning(f"🚫 Portfolio VaR: {result.reason}")
            return result

    # 检查3: 集中度（警告但不阻止）
    if result.max_concentration > cfg.max_concentration_pct:
        logger.warning(
            f"⚠️ 集中度警告: {result.concentration_symbol} 占 {result.max_concentration*100:.0f}% "
            f"(阈值 {cfg.max_concentration_pct*100:.0f}%)"
        )

    result.allowed = True
    result.reason = "OK"
    return result


def get_portfolio_risk_summary(
    open_trades: List[Dict],
    account_balance: float = 100,
    config: Optional[PortfolioVaRConfig] = None,
) -> str:
    """获取组合风险的人类可读摘要（用于 dashboard/TG）"""
    result = check_portfolio_risk(
        open_trades=open_trades,
        account_balance=account_balance,
        config=config,
    )

    if result.n_positions == 0:
        return "📊 组合风险: 无持仓"

    return (
        f"📊 组合风险 ({result.n_positions}仓):\n"
        f"  VaR(95%): {result.portfolio_var:.1f}U ({result.portfolio_var_pct:.0f}%)\n"
        f"  CVaR(95%): {result.portfolio_cvar:.1f}U ({result.portfolio_cvar_pct:.0f}%)\n"
        f"  总敞口: {result.total_exposure:.0f}U\n"
        f"  最大集中: {result.concentration_symbol} {result.max_concentration*100:.0f}%"
    )
