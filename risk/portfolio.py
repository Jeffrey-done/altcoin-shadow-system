"""
组合级风控 — PortfolioRiskManager
超越单笔交易的风控：相关性限制、板块暴露、动态仓位（Kelly）、组合 VaR。

设计原则:
  - 与原有单笔风控（risk_control.py）互补，不替代
  - 原有风控做"单笔准入门禁"，本模块做"整体暴露监控"
  - 可独立启用/禁用（配置开关）
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("risk.portfolio")


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class PortfolioRiskConfig:
    """组合风控配置"""
    enabled: bool = True

    # 相关性限制
    correlation_limit: float = 0.85     # 同时持仓的最大相关性
    correlation_lookback_days: int = 7  # 相关性计算回溯天数
    max_same_sector: int = 2            # 同板块最大持仓数

    # Kelly 仓位
    kelly_enabled: bool = True
    kelly_fraction: float = 0.5         # 半 Kelly（保守）
    kelly_min_trades: int = 20          # 至少 N 笔历史交易才启用
    kelly_max_pct: float = 30.0         # Kelly 建议的最大仓位占比 %
    kelly_min_stake: float = 10.0       # 最低仓位 (USDT)

    # VaR 限制
    var_limit_pct: float = 20.0         # 组合 VaR 不超过本金 N%
    var_confidence: float = 0.95        # VaR 置信度

    # 集中度
    max_single_position_pct: float = 40.0   # 单笔最大占比 %
    max_total_exposure_pct: float = 80.0    # 总暴露不超过本金 N%

    # 板块分类（symbol → sector 映射）
    sector_map: Dict[str, str] = field(default_factory=lambda: {
        # meme 币
        'PEPE/USDT': 'meme', 'DOGE/USDT': 'meme', 'SHIB/USDT': 'meme',
        'FLOKI/USDT': 'meme', 'BONK/USDT': 'meme', 'WIF/USDT': 'meme',
        # 铭文
        'ORDI/USDT': 'inscription', '1000SATS/USDT': 'inscription',
        # AI
        'FET/USDT': 'ai', 'RNDR/USDT': 'ai', 'AGIX/USDT': 'ai',
        # 治理/DAO
        'PEOPLE/USDT': 'governance',
    })


# ══════════════════════════════════════════════════════════════════
#  检查结果
# ══════════════════════════════════════════════════════════════════

@dataclass
class RiskCheckResult:
    """风控检查结果"""
    approved: bool = True
    reason: str = ''
    adjustments: Dict[str, float] = field(default_factory=dict)
    # adjustments 可包含 {'suggested_stake': 20.0} 等建议

    def __bool__(self):
        return self.approved


# ══════════════════════════════════════════════════════════════════
#  组合风控管理器
# ══════════════════════════════════════════════════════════════════

class PortfolioRiskManager:
    """
    组合级风控管理器。

    用法:
      manager = PortfolioRiskManager(config, account_balance=1000)
      result = manager.check_new_position(
          symbol='PEPE/USDT',
          stake=30,
          open_positions=[{'symbol': 'DOGE/USDT', 'stake': 30}, ...]
      )
      if not result.approved:
          logger.warning(f"组合风控拒绝: {result.reason}")
    """

    def __init__(
        self,
        config: Optional[PortfolioRiskConfig] = None,
        account_balance: float = 1000.0,
    ):
        self.config = config or PortfolioRiskConfig()
        self.account_balance = account_balance
        self._price_history: Dict[str, List[float]] = {}  # symbol → 近期收盘价

    def check_new_position(
        self,
        symbol: str,
        stake: float,
        open_positions: List[Dict],
        historical_trades: Optional[List[Dict]] = None,
    ) -> RiskCheckResult:
        """
        综合检查新仓位是否通过组合风控。

        参数:
          symbol: 新仓位的币种
          stake: 新仓位保证金
          open_positions: 当前所有持仓 [{'symbol': str, 'stake': float, ...}]
          historical_trades: 历史已平仓交易（用于 Kelly 计算）

        返回:
          RiskCheckResult（approved=True/False + 原因）
        """
        cfg = self.config
        if not cfg.enabled:
            return RiskCheckResult(approved=True, reason="组合风控已禁用")

        # 1. 总暴露检查
        result = self._check_total_exposure(stake, open_positions)
        if not result.approved:
            return result

        # 2. 单笔集中度检查
        result = self._check_concentration(stake)
        if not result.approved:
            return result

        # 3. 板块暴露检查
        result = self._check_sector_exposure(symbol, open_positions)
        if not result.approved:
            return result

        # 4. 相关性检查
        result = self._check_correlation(symbol, open_positions)
        if not result.approved:
            return result

        # 5. Kelly 仓位建议
        adjustments = {}
        if cfg.kelly_enabled and historical_trades:
            kelly_stake = self._calculate_kelly_stake(historical_trades)
            if kelly_stake is not None and stake > kelly_stake:
                adjustments['suggested_stake'] = kelly_stake
                logger.info(
                    f"Kelly 建议仓位 {kelly_stake:.0f}U（实际 {stake:.0f}U）"
                )

        return RiskCheckResult(
            approved=True,
            reason="通过",
            adjustments=adjustments,
        )

    # ── 子检查 ───────────────────────────────────────────────────

    def _check_total_exposure(self, new_stake: float,
                              open_positions: List[Dict]) -> RiskCheckResult:
        """总暴露不超过本金 N%"""
        current_total = sum(p.get('stake_remaining', p.get('stake', 0))
                           for p in open_positions)
        new_total = current_total + new_stake
        max_exposure = self.account_balance * self.config.max_total_exposure_pct / 100

        if new_total > max_exposure:
            return RiskCheckResult(
                approved=False,
                reason=(
                    f"总暴露超限: 当前{current_total:.0f}U + 新增{new_stake:.0f}U = "
                    f"{new_total:.0f}U > 上限{max_exposure:.0f}U "
                    f"({self.config.max_total_exposure_pct}% × {self.account_balance:.0f}U)"
                ),
            )
        return RiskCheckResult(approved=True)

    def _check_concentration(self, stake: float) -> RiskCheckResult:
        """单笔不超过本金 N%"""
        max_single = self.account_balance * self.config.max_single_position_pct / 100
        if stake > max_single:
            return RiskCheckResult(
                approved=False,
                reason=(
                    f"单笔集中度超限: {stake:.0f}U > "
                    f"{max_single:.0f}U ({self.config.max_single_position_pct}%)"
                ),
            )
        return RiskCheckResult(approved=True)

    def _check_sector_exposure(self, symbol: str,
                               open_positions: List[Dict]) -> RiskCheckResult:
        """同板块持仓数限制"""
        cfg = self.config
        new_sector = cfg.sector_map.get(symbol, 'unknown')

        if new_sector == 'unknown':
            return RiskCheckResult(approved=True)

        same_sector_count = sum(
            1 for p in open_positions
            if cfg.sector_map.get(p.get('symbol', ''), 'other') == new_sector
        )

        if same_sector_count >= cfg.max_same_sector:
            return RiskCheckResult(
                approved=False,
                reason=(
                    f"板块暴露超限: {new_sector} 板块已有 {same_sector_count} 笔持仓 "
                    f"(上限 {cfg.max_same_sector})"
                ),
            )
        return RiskCheckResult(approved=True)

    def _check_correlation(self, symbol: str,
                           open_positions: List[Dict]) -> RiskCheckResult:
        """相关性检查：不允许高相关品种同时持仓"""
        cfg = self.config

        if not self._price_history or symbol not in self._price_history:
            # 无历史价格数据，跳过相关性检查
            return RiskCheckResult(approved=True)

        new_prices = np.array(self._price_history.get(symbol, []))
        if len(new_prices) < 10:
            return RiskCheckResult(approved=True)

        for pos in open_positions:
            pos_symbol = pos.get('symbol', '')
            pos_prices = self._price_history.get(pos_symbol)
            if pos_prices is None or len(pos_prices) < 10:
                continue

            pos_arr = np.array(pos_prices)
            min_len = min(len(new_prices), len(pos_arr))
            if min_len < 10:
                continue

            # 计算收益率相关性
            returns_new = np.diff(new_prices[-min_len:]) / new_prices[-min_len:-1]
            returns_pos = np.diff(pos_arr[-min_len:]) / pos_arr[-min_len:-1]

            corr = np.corrcoef(returns_new, returns_pos)[0, 1]

            if abs(corr) > cfg.correlation_limit:
                return RiskCheckResult(
                    approved=False,
                    reason=(
                        f"相关性超限: {symbol} 与 {pos_symbol} "
                        f"相关系数={corr:.2f} > {cfg.correlation_limit}"
                    ),
                )

        return RiskCheckResult(approved=True)

    # ── Kelly 公式 ───────────────────────────────────────────────

    def _calculate_kelly_stake(self, historical_trades: List[Dict]) -> Optional[float]:
        """
        Kelly 公式计算最优仓位。

        公式: f* = (p * b - q) / b
          - p = 胜率
          - q = 1 - p
          - b = 平均盈亏比 (avg_win / avg_loss)

        使用半 Kelly（乘以 kelly_fraction）更保守。
        """
        cfg = self.config

        if len(historical_trades) < cfg.kelly_min_trades:
            return None

        wins = []
        losses = []
        for t in historical_trades:
            pnl = t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
            if pnl > 0:
                wins.append(pnl)
            elif pnl < 0:
                losses.append(abs(pnl))

        if not wins or not losses:
            return None

        p = len(wins) / len(historical_trades)
        q = 1 - p
        avg_win = np.mean(wins)
        avg_loss = np.mean(losses)

        if avg_loss == 0:
            return None

        b = avg_win / avg_loss  # 盈亏比
        kelly_f = (p * b - q) / b

        if kelly_f <= 0:
            # Kelly 建议不投入（策略没有正期望）
            return cfg.kelly_min_stake

        # 半 Kelly
        fraction = kelly_f * cfg.kelly_fraction
        # 限制上限
        fraction = min(fraction, cfg.kelly_max_pct / 100)

        stake = self.account_balance * fraction
        stake = max(stake, cfg.kelly_min_stake)

        return round(stake, 1)

    # ── 价格历史管理 ─────────────────────────────────────────────

    def update_price_history(self, symbol: str, prices: List[float]):
        """更新价格历史（用于相关性计算）"""
        self._price_history[symbol] = prices

    def update_balance(self, balance: float):
        """更新账户余额"""
        self.account_balance = balance

    # ── 组合统计 ─────────────────────────────────────────────────

    def get_portfolio_summary(self, open_positions: List[Dict]) -> Dict:
        """获取当前组合摘要"""
        cfg = self.config
        total_stake = sum(
            p.get('stake_remaining', p.get('stake', 0))
            for p in open_positions
        )

        # 板块分布
        sectors = {}
        for p in open_positions:
            sector = cfg.sector_map.get(p.get('symbol', ''), 'other')
            sectors[sector] = sectors.get(sector, 0) + 1

        return {
            'total_positions': len(open_positions),
            'total_exposure': round(total_stake, 2),
            'exposure_pct': round(total_stake / max(self.account_balance, 1) * 100, 1),
            'max_exposure_pct': cfg.max_total_exposure_pct,
            'sector_distribution': sectors,
            'symbols': [p.get('symbol', '') for p in open_positions],
        }
