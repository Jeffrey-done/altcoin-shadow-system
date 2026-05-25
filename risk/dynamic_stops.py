"""
动态止损模块 — 基于 ATR 的自适应止盈止损
替代固定百分比止损，根据品种实际波动率动态调整。

核心思想:
  - 高波动率币种 → 更宽的止损（避免被正常波动洗出）
  - 低波动率币种 → 更紧的止损（保护利润）
  - ATR 是衡量"正常波动幅度"的最佳指标
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np


@dataclass
class DynamicStopConfig:
    """动态止损配置"""
    enabled: bool = True

    # ATR 参数
    atr_period: int = 14              # ATR 计算周期
    atr_timeframe: str = '4h'        # ATR 使用的时间框架

    # 止损倍数
    hard_stop_atr_mult: float = 2.5   # 硬止损 = entry ± N × ATR
    tp1_atr_mult: float = 1.5         # TP1 = entry ∓ N × ATR
    tp2_atr_mult: float = 3.0         # TP2 = entry ∓ N × ATR
    trail_activate_atr_mult: float = 1.0  # 移动止损激活门槛

    # 安全边界（防止 ATR 极端值）
    min_stop_pct: float = 2.0         # 最小止损 %（ATR 极低时兜底）
    max_stop_pct: float = 10.0        # 最大止损 %（ATR 极高时封顶）
    min_tp_pct: float = 2.0           # 最小止盈 %
    max_tp_pct: float = 20.0          # 最大止盈 %


@dataclass
class DynamicStopLevels:
    """计算出的动态止损价位"""
    hard_stop_price: float
    tp1_price: float
    tp2_price: float
    trail_activate_pct: float
    atr_value: float
    atr_pct: float                    # ATR / entry_price * 100

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def calculate_atr(highs: List[float], lows: List[float],
                  closes: List[float], period: int = 14) -> float:
    """
    计算 Average True Range。

    True Range = max(H-L, |H-Cprev|, |L-Cprev|)
    ATR = EMA(TR, period)
    """
    if len(highs) < period + 1:
        return 0.0

    h = np.array(highs, dtype=np.float64)
    l = np.array(lows, dtype=np.float64)
    c = np.array(closes, dtype=np.float64)

    # True Range
    tr = np.maximum(
        h[1:] - l[1:],
        np.maximum(
            np.abs(h[1:] - c[:-1]),
            np.abs(l[1:] - c[:-1])
        )
    )

    if len(tr) < period:
        return float(tr.mean()) if len(tr) > 0 else 0.0

    # Wilder 平滑 ATR
    atr = tr[:period].mean()
    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period

    return float(atr)


def calculate_dynamic_stops(
    entry_price: float,
    direction: str,
    ohlcv: List[List[float]],
    config: Optional[DynamicStopConfig] = None,
) -> DynamicStopLevels:
    """
    基于 ATR 计算动态止盈止损价位。

    参数:
      entry_price: 入场价格
      direction: 'SHORT' 或 'LONG'
      ohlcv: K 线数据 [[ts, open, high, low, close, vol], ...]
      config: 动态止损配置

    返回:
      DynamicStopLevels 包含所有计算出的价位
    """
    cfg = config or DynamicStopConfig()

    # 计算 ATR
    if len(ohlcv) < cfg.atr_period + 1:
        # 数据不足，退回固定百分比
        atr_pct = 3.0  # 默认假设 3% 波动
    else:
        highs = [c[2] for c in ohlcv]
        lows = [c[3] for c in ohlcv]
        closes = [c[4] for c in ohlcv]
        atr = calculate_atr(highs, lows, closes, cfg.atr_period)
        atr_pct = (atr / entry_price * 100) if entry_price > 0 else 3.0

    # 裁剪到安全边界
    hard_stop_pct = np.clip(
        atr_pct * cfg.hard_stop_atr_mult,
        cfg.min_stop_pct, cfg.max_stop_pct
    )
    tp1_pct = np.clip(
        atr_pct * cfg.tp1_atr_mult,
        cfg.min_tp_pct, cfg.max_tp_pct
    )
    tp2_pct = np.clip(
        atr_pct * cfg.tp2_atr_mult,
        cfg.min_tp_pct, cfg.max_tp_pct
    )
    trail_activate_pct = atr_pct * cfg.trail_activate_atr_mult

    # 计算价格
    if direction.upper() == 'SHORT':
        hard_stop_price = entry_price * (1 + hard_stop_pct / 100)
        tp1_price = entry_price * (1 - tp1_pct / 100)
        tp2_price = entry_price * (1 - tp2_pct / 100)
    else:
        hard_stop_price = entry_price * (1 - hard_stop_pct / 100)
        tp1_price = entry_price * (1 + tp1_pct / 100)
        tp2_price = entry_price * (1 + tp2_pct / 100)

    return DynamicStopLevels(
        hard_stop_price=round(hard_stop_price, 8),
        tp1_price=round(tp1_price, 8),
        tp2_price=round(tp2_price, 8),
        trail_activate_pct=round(float(trail_activate_pct), 2),
        atr_value=round(atr_pct * entry_price / 100, 8),
        atr_pct=round(float(atr_pct), 2),
    )
