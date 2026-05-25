"""
滑点模型
提供多层次的滑点估算：从简单固定比例到基于成交量的真实模型。

设计原则:
  - 小币种（本策略目标）滑点显著高于主流币
  - 名义仓位 / 24h 成交量 的比值是最重要的影响因子
  - 模型参数可在 Optuna 优化中作为"环境噪声"敏感性测试
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np


class SlippageModelType(str, Enum):
    FIXED = 'fixed'           # 固定百分比
    VOLUME_BASED = 'volume'   # 基于成交量
    TIERED = 'tiered'         # 分层（根据 notional 大小）


@dataclass
class SlippageConfig:
    """滑点模型配置"""
    model_type: SlippageModelType = SlippageModelType.VOLUME_BASED

    # 固定模型参数
    fixed_pct: float = 0.1             # 固定滑点 %

    # 基于成交量模型参数
    base_bps: float = 3.0              # 基础滑点 bps (0.03%)
    volume_impact_coeff: float = 50.0  # 冲击系数
    # 公式: slippage_bps = base_bps + coeff * (notional / volume_24h)

    # 分层模型参数（按 notional 档位）
    tier_thresholds: tuple = (1000, 5000, 20000)  # USDT
    tier_bps: tuple = (3, 8, 15, 30)              # 对应滑点 bps

    # 随机扰动（回测真实性）
    random_noise_pct: float = 0.02     # 在模型基础上 ±N% 随机扰动
    seed: Optional[int] = None


class SlippageModel:
    """
    滑点估算模型。

    用法:
      model = SlippageModel(SlippageConfig())
      slippage_pct = model.estimate(notional=300, volume_24h=500000)
      adjusted_price = model.apply(price=0.00123, notional=300,
                                    volume_24h=500000, direction='SHORT')
    """

    def __init__(self, config: Optional[SlippageConfig] = None):
        self.config = config or SlippageConfig()
        self._rng = np.random.default_rng(self.config.seed)

    def estimate(self, notional: float, volume_24h: float = 0.0) -> float:
        """
        估算滑点百分比。

        参数:
          notional: 名义仓位 (USDT)
          volume_24h: 该币种 24h 成交量 (USDT)

        返回:
          滑点百分比 (如 0.15 表示 0.15%)
        """
        cfg = self.config

        if cfg.model_type == SlippageModelType.FIXED:
            base = cfg.fixed_pct

        elif cfg.model_type == SlippageModelType.VOLUME_BASED:
            if volume_24h <= 0:
                # 无成交量数据时用保守估计
                base = cfg.base_bps / 100 + 0.2
            else:
                ratio = notional / volume_24h
                bps = cfg.base_bps + cfg.volume_impact_coeff * ratio
                base = bps / 100  # bps → %

        elif cfg.model_type == SlippageModelType.TIERED:
            thresholds = cfg.tier_thresholds
            bps_list = cfg.tier_bps
            tier_idx = 0
            for i, t in enumerate(thresholds):
                if notional > t:
                    tier_idx = i + 1
            tier_idx = min(tier_idx, len(bps_list) - 1)
            base = bps_list[tier_idx] / 100

        else:
            base = cfg.fixed_pct

        # 添加随机扰动
        if cfg.random_noise_pct > 0:
            noise = self._rng.uniform(-cfg.random_noise_pct, cfg.random_noise_pct)
            base = max(0, base + noise)

        return round(base, 4)

    def apply(self, price: float, notional: float,
              volume_24h: float = 0.0, direction: str = 'SHORT') -> float:
        """
        应用滑点到价格。

        做空开仓: 实际成交价更低（不利）→ price * (1 - slippage)
        做空平仓: 实际成交价更高（不利）→ price * (1 + slippage)
        做多开仓: price * (1 + slippage)
        做多平仓: price * (1 - slippage)

        参数:
          price: 参考价格
          notional: 名义仓位
          volume_24h: 24h 成交量
          direction: 'SHORT' 或 'LONG'

        返回:
          滑点调整后的成交价
        """
        slip_pct = self.estimate(notional, volume_24h) / 100

        if direction.upper() == 'SHORT':
            # 做空开仓：卖出，价格滑向更低 → 对空头不利
            return price * (1 - slip_pct)
        else:
            # 做多开仓：买入，价格滑向更高
            return price * (1 + slip_pct)

    def apply_close(self, price: float, notional: float,
                    volume_24h: float = 0.0, direction: str = 'SHORT') -> float:
        """
        应用平仓滑点（方向相反）。
        """
        slip_pct = self.estimate(notional, volume_24h) / 100

        if direction.upper() == 'SHORT':
            # 平空 = 买入，价格滑向更高 → 对空头不利
            return price * (1 + slip_pct)
        else:
            # 平多 = 卖出，价格滑向更低
            return price * (1 - slip_pct)
