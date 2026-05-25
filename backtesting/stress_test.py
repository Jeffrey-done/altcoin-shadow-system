#!/usr/bin/env python3
"""
极端行情压力测试框架 v1.0

模拟历史 Black Swan 事件和合成极端场景，评估策略在尾部风险下的表现。

场景类型：
  A. 历史回放（Historical Replay）
     - LUNA 崩盘 2022-05-09 (全市场 -30% 1h)
     - FTX 暴雷 2022-11-08 (流动性蒸发)
     - SVB 危机 2023-03-11 (USDC 脱锚)

  B. 合成极端（Synthetic Stress）
     - 全仓位同时打止损 + 交易所 5s 无响应
     - Funding rate 突变 ±0.5%/8h
     - 持仓币种同日退市（价格归零）
     - 流动性枯竭（Order Book 深度降 90%）

输出指标：
  - 最坏情况 Max Drawdown
  - 破产概率 (净值 < 50% 初始本金)
  - VaR / CVaR (99%)
  - 恢复时间（从最大回撤恢复到前高的 bar 数）
  - 各场景下的盈亏分布

用法：
  from backtesting.stress_test import StressTestRunner
  runner = StressTestRunner(account_balance=1000)
  report = runner.run_all()
  print(report.summary())
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

import numpy as np

logger = logging.getLogger("backtesting.stress_test")



# ══════════════════════════════════════════════════════════════════
#  场景定义
# ══════════════════════════════════════════════════════════════════

@dataclass
class StressScenario:
    """压力测试场景"""
    name: str
    description: str
    category: str                      # 'historical' / 'synthetic'

    # 价格冲击参数
    price_shock_pct: float = 0.0       # 瞬时价格变动 %（负=下跌）
    shock_duration_bars: int = 1       # 冲击持续时间（bars）
    recovery_bars: int = 24            # 恢复时间（bars）
    recovery_ratio: float = 0.5        # 恢复比例（0=不恢复, 1=完全恢复）

    # 流动性冲击
    liquidity_shock_pct: float = 0.0   # 深度减少比例（0.9=减90%）
    slippage_multiplier: float = 1.0   # 滑点乘数

    # 交易所异常
    api_blackout_sec: float = 0.0      # API 不可用时间
    funding_shock: float = 0.0         # 费率突变 (%/8h)

    # 特殊事件
    delist_probability: float = 0.0    # 持仓币退市概率
    cascade_liquidation: bool = False  # 是否触发清算瀑布


# 预定义历史场景
LUNA_CRASH = StressScenario(
    name='LUNA_CRASH_2022',
    description='LUNA/UST 崩盘: 全市场 1h 内跌 30%, 山寨币跌 40-80%',
    category='historical',
    price_shock_pct=-40.0,
    shock_duration_bars=4,
    recovery_bars=48,
    recovery_ratio=0.2,
    liquidity_shock_pct=0.7,
    slippage_multiplier=5.0,
    api_blackout_sec=30.0,
    cascade_liquidation=True,
)

FTX_COLLAPSE = StressScenario(
    name='FTX_COLLAPSE_2022',
    description='FTX 暴雷: 流动性蒸发, 连续 3 天阴跌 50%',
    category='historical',
    price_shock_pct=-15.0,
    shock_duration_bars=72,
    recovery_bars=168,
    recovery_ratio=0.3,
    liquidity_shock_pct=0.8,
    slippage_multiplier=8.0,
    api_blackout_sec=120.0,
    funding_shock=-0.3,
)

SVB_USDC_DEPEG = StressScenario(
    name='SVB_USDC_DEPEG_2023',
    description='SVB 危机导致 USDC 脱锚: 市场 flash crash 后快速反弹',
    category='historical',
    price_shock_pct=-20.0,
    shock_duration_bars=2,
    recovery_bars=12,
    recovery_ratio=0.9,
    liquidity_shock_pct=0.5,
    slippage_multiplier=3.0,
)



# 合成极端场景
SIMULTANEOUS_STOP_LOSS = StressScenario(
    name='SIMULTANEOUS_STOP_LOSS',
    description='所有持仓同时触发硬止损 + 交易所 5s 无响应',
    category='synthetic',
    price_shock_pct=6.0,  # 做空仓位反弹 6%（超过 5% 硬止损）
    shock_duration_bars=1,
    recovery_bars=0,
    recovery_ratio=0.0,
    api_blackout_sec=5.0,
    slippage_multiplier=3.0,
)

FUNDING_SPIKE = StressScenario(
    name='FUNDING_RATE_SPIKE',
    description='资金费率突变到 ±0.5%/8h（做空者每 8h 付 0.5% 利息）',
    category='synthetic',
    funding_shock=-0.5,
    shock_duration_bars=24,
)

COIN_DELIST = StressScenario(
    name='COIN_DELIST',
    description='持仓币种同日宣布退市，价格 1h 归零',
    category='synthetic',
    price_shock_pct=-95.0,
    shock_duration_bars=4,
    recovery_ratio=0.0,
    delist_probability=1.0,
    liquidity_shock_pct=0.95,
    slippage_multiplier=20.0,
)

LIQUIDITY_DROUGHT = StressScenario(
    name='LIQUIDITY_DROUGHT',
    description='市场流动性枯竭: Order Book 深度降 90%, 滑点 10x',
    category='synthetic',
    liquidity_shock_pct=0.9,
    slippage_multiplier=10.0,
    shock_duration_bars=12,
)

ALL_SCENARIOS = [
    LUNA_CRASH, FTX_COLLAPSE, SVB_USDC_DEPEG,
    SIMULTANEOUS_STOP_LOSS, FUNDING_SPIKE, COIN_DELIST, LIQUIDITY_DROUGHT,
]



# ══════════════════════════════════════════════════════════════════
#  压力测试结果
# ══════════════════════════════════════════════════════════════════

@dataclass
class ScenarioResult:
    """单场景测试结果"""
    scenario_name: str
    category: str = ''

    # 盈亏
    pnl_usdt: float = 0.0
    pnl_pct: float = 0.0              # 相对初始本金
    max_drawdown_pct: float = 0.0
    max_drawdown_usdt: float = 0.0

    # 持仓影响
    positions_stopped: int = 0         # 被止损的仓位数
    positions_liquidated: int = 0      # 被强平的仓位数
    positions_survived: int = 0        # 存活仓位数

    # 执行
    slippage_total_usdt: float = 0.0   # 额外滑点成本
    api_blackout_missed: int = 0       # API 不可用期间错过的止损
    funding_cost_usdt: float = 0.0     # 额外费率成本

    # 恢复
    recovery_bars: int = 0             # 恢复到前高需要多少 bar
    is_bankrupt: bool = False          # 净值 < 50% 初始本金

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class StressTestReport:
    """完整压力测试报告"""
    results: List[ScenarioResult] = field(default_factory=list)
    account_balance: float = 1000.0
    timestamp: float = 0.0
    elapsed_sec: float = 0.0

    # 汇总指标
    worst_case_drawdown_pct: float = 0.0
    worst_case_scenario: str = ''
    bankruptcy_count: int = 0
    total_scenarios: int = 0
    var_99_pct: float = 0.0            # 99% VaR
    cvar_99_pct: float = 0.0           # 99% CVaR (Expected Shortfall)

    def summary(self) -> str:
        """生成人类可读的摘要报告"""
        lines = [
            "═" * 60,
            "  极端行情压力测试报告",
            "═" * 60,
            f"  初始本金: {self.account_balance:.0f} USDT",
            f"  测试场景: {self.total_scenarios} 个",
            f"  耗时: {self.elapsed_sec:.2f}s",
            "",
            "── 汇总指标 ──",
            f"  最坏回撤: {self.worst_case_drawdown_pct:.1f}% ({self.worst_case_scenario})",
            f"  破产场景: {self.bankruptcy_count}/{self.total_scenarios}",
            f"  99% VaR:  {self.var_99_pct:.1f}%",
            f"  99% CVaR: {self.cvar_99_pct:.1f}%",
            "",
            "── 各场景详情 ──",
        ]
        for r in self.results:
            status = "💀" if r.is_bankrupt else ("🔴" if r.pnl_pct < -10 else "🟡" if r.pnl_pct < 0 else "🟢")
            lines.append(
                f"  {status} {r.scenario_name:<28} | "
                f"PnL={r.pnl_pct:+.1f}% | DD={r.max_drawdown_pct:.1f}% | "
                f"止损={r.positions_stopped} 强平={r.positions_liquidated}"
            )
        lines.append("═" * 60)
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'account_balance': self.account_balance,
            'total_scenarios': self.total_scenarios,
            'worst_case_drawdown_pct': self.worst_case_drawdown_pct,
            'worst_case_scenario': self.worst_case_scenario,
            'bankruptcy_count': self.bankruptcy_count,
            'var_99_pct': self.var_99_pct,
            'cvar_99_pct': self.cvar_99_pct,
            'results': [r.to_dict() for r in self.results],
        }



# ══════════════════════════════════════════════════════════════════
#  压力测试运行器
# ══════════════════════════════════════════════════════════════════

class StressTestRunner:
    """
    压力测试运行器。

    用法:
      runner = StressTestRunner(account_balance=1000)
      report = runner.run_all()
      print(report.summary())

      # 或测试单个场景
      result = runner.run_scenario(LUNA_CRASH, open_positions=[...])
    """

    def __init__(
        self,
        account_balance: float = 1000.0,
        leverage: int = 10,
        hard_stop_pct: float = 5.0,
        max_positions: int = 3,
        default_stake: float = 30.0,
    ):
        self.account_balance = account_balance
        self.leverage = leverage
        self.hard_stop_pct = hard_stop_pct
        self.max_positions = max_positions
        self.default_stake = default_stake

    def run_all(
        self,
        scenarios: Optional[List[StressScenario]] = None,
        open_positions: Optional[List[Dict]] = None,
    ) -> StressTestReport:
        """
        运行所有压力测试场景。

        参数:
          scenarios: 场景列表（None=使用全部预定义场景）
          open_positions: 模拟持仓（None=自动生成最大持仓）
        """
        t0 = time.monotonic()
        scenarios = scenarios or ALL_SCENARIOS

        if open_positions is None:
            open_positions = self._generate_max_positions()

        report = StressTestReport(
            account_balance=self.account_balance,
            timestamp=time.time(),
            total_scenarios=len(scenarios),
        )

        pnl_list = []

        for scenario in scenarios:
            result = self.run_scenario(scenario, open_positions)
            report.results.append(result)
            pnl_list.append(result.pnl_pct)

            if result.is_bankrupt:
                report.bankruptcy_count += 1

            if result.max_drawdown_pct > report.worst_case_drawdown_pct:
                report.worst_case_drawdown_pct = result.max_drawdown_pct
                report.worst_case_scenario = scenario.name

        # 计算 VaR / CVaR
        if pnl_list:
            sorted_pnl = sorted(pnl_list)
            idx_99 = max(0, int(len(sorted_pnl) * 0.01))
            report.var_99_pct = abs(sorted_pnl[idx_99]) if sorted_pnl else 0
            # CVaR = 平均超过 VaR 的损失
            tail = [p for p in sorted_pnl if p <= sorted_pnl[idx_99]]
            report.cvar_99_pct = abs(np.mean(tail)) if tail else report.var_99_pct

        report.elapsed_sec = round(time.monotonic() - t0, 3)
        return report

    def run_scenario(
        self,
        scenario: StressScenario,
        open_positions: Optional[List[Dict]] = None,
    ) -> ScenarioResult:
        """
        运行单个压力测试场景。

        模拟逻辑：
          1. 对所有持仓施加价格冲击
          2. 检查哪些仓位触发止损/强平
          3. 计算滑点放大、费率成本、API 不可用影响
          4. 汇总损益
        """
        if open_positions is None:
            open_positions = self._generate_max_positions()

        result = ScenarioResult(
            scenario_name=scenario.name,
            category=scenario.category,
        )

        total_pnl = 0.0
        equity_curve = [self.account_balance]

        for pos in open_positions:
            pos_pnl = self._simulate_position_under_stress(pos, scenario)
            total_pnl += pos_pnl['net_pnl']
            result.slippage_total_usdt += pos_pnl['extra_slippage']
            result.funding_cost_usdt += pos_pnl['funding_cost']

            if pos_pnl['stopped']:
                result.positions_stopped += 1
            elif pos_pnl['liquidated']:
                result.positions_liquidated += 1
            else:
                result.positions_survived += 1

            if pos_pnl['api_missed']:
                result.api_blackout_missed += 1

        # 计算结果
        result.pnl_usdt = round(total_pnl, 2)
        result.pnl_pct = round(total_pnl / self.account_balance * 100, 2)
        result.max_drawdown_usdt = abs(min(0, total_pnl))
        result.max_drawdown_pct = round(result.max_drawdown_usdt / self.account_balance * 100, 2)
        result.is_bankrupt = (self.account_balance + total_pnl) < (self.account_balance * 0.5)

        # 恢复时间估算
        if scenario.recovery_bars > 0 and scenario.recovery_ratio > 0:
            result.recovery_bars = int(scenario.recovery_bars / max(scenario.recovery_ratio, 0.1))
        else:
            result.recovery_bars = -1  # 不可恢复

        return result

    def _simulate_position_under_stress(
        self,
        position: Dict,
        scenario: StressScenario,
    ) -> Dict[str, Any]:
        """模拟单个持仓在压力场景下的表现"""
        entry_price = position.get('entry_price', 1.0)
        stake = position.get('stake', self.default_stake)
        leverage = position.get('leverage', self.leverage)
        direction = position.get('direction', 'SHORT')
        notional = stake * leverage

        # 价格变动
        shock = scenario.price_shock_pct / 100.0

        # 做空：价格上涨 = 亏损; 做多：价格下跌 = 亏损
        if direction == 'SHORT':
            pnl_pct = -shock  # 价格涨 → 做空亏
        else:
            pnl_pct = shock   # 价格跌 → 做多亏

        raw_pnl = notional * pnl_pct

        # 滑点放大
        base_slippage = 0.001  # 正常 0.1%
        stress_slippage = base_slippage * scenario.slippage_multiplier
        extra_slippage = notional * (stress_slippage - base_slippage)

        # 费率成本
        funding_cost = 0.0
        if scenario.funding_shock != 0:
            # 做空时负费率 = 亏损
            periods = scenario.shock_duration_bars / 8  # 每 8 bar 结算一次
            if direction == 'SHORT' and scenario.funding_shock < 0:
                funding_cost = abs(scenario.funding_shock / 100) * notional * periods
            elif direction == 'LONG' and scenario.funding_shock > 0:
                funding_cost = abs(scenario.funding_shock / 100) * notional * periods

        # 检查止损触发
        stopped = False
        liquidated = False
        api_missed = False

        actual_loss_pct = abs(pnl_pct) * 100 if pnl_pct < 0 else 0

        if actual_loss_pct >= self.hard_stop_pct:
            stopped = True
            # API 不可用期间无法执行止损 → 额外损失
            if scenario.api_blackout_sec > 0:
                api_missed = True
                # 在 blackout 期间价格继续恶化（假设每秒恶化 0.1%）
                additional_loss_pct = scenario.api_blackout_sec * 0.001
                raw_pnl -= notional * additional_loss_pct

        # 强平检查（损失 > 保证金）
        if abs(raw_pnl) > stake:
            liquidated = True
            raw_pnl = -stake  # 最多亏完保证金

        # 退市 → 归零
        if scenario.delist_probability > 0:
            if np.random.random() < scenario.delist_probability:
                if direction == 'SHORT':
                    raw_pnl = notional * 0.95  # 做空时币归零 = 大赚
                else:
                    raw_pnl = -stake  # 做多时归零 = 全亏

        net_pnl = raw_pnl - extra_slippage - funding_cost

        return {
            'net_pnl': net_pnl,
            'extra_slippage': extra_slippage,
            'funding_cost': funding_cost,
            'stopped': stopped,
            'liquidated': liquidated,
            'api_missed': api_missed,
        }

    def _generate_max_positions(self) -> List[Dict]:
        """生成最大持仓场景（最差情况）"""
        positions = []
        for i in range(self.max_positions):
            positions.append({
                'symbol': f'MEME{i}/USDT',
                'direction': 'SHORT',
                'entry_price': 0.001 * (i + 1),
                'stake': self.default_stake,
                'leverage': self.leverage,
            })
        return positions
