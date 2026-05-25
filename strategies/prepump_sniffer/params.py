"""
Pre-Pump Sniffer 策略参数
"""

from dataclasses import dataclass, asdict
from typing import Dict, Any


@dataclass
class PrePumpParams:
    """妖币起飞前嗅探器参数集"""

    # ── 扫描过滤（候选池基础条件）──
    vol_min: float = 500_000           # 24h 成交量下限 (USDT)
    price_max: float = 1.0             # 价格上限
    price_change_max: float = 5.0      # 24h 涨跌幅 < ±N%（还在横盘的币）

    # ── 异常检测阈值（7维度）──
    vol_spike_ratio: float = 2.0       # 成交量/24H均值 > N 倍
    price_calm_pct: float = 3.0        # 同时价格变化 < N% 才算"平静吸筹"
    oi_spike_pct: float = 2.5          # OI 4H 涨幅 > N%
    funding_shift: float = 0.015       # Funding 从低翻高的阈值
    bb_squeeze_pctile: float = 55.0    # BB width 低于近7天的 N 百分位

    # ── 评分门槛 ──
    signal_threshold: int = 3          # 7个维度至少 N 个亮灯才进入准备池

    # ── 突破确认 ──
    breakout_lookback: int = 12        # 突破近 N 小时高点
    breakout_pct: float = 0.3          # 突破幅度 > N%
    breakout_vol_mult: float = 1.2     # 突破时成交量 > 7日均值 × N
    breakout_wait_bars: int = 18       # 最多等 N 小时确认突破

    # ── 止盈止损 ──
    tp1_pct: float = 6.0              # TP1: +N%
    tp2_pct: float = 18.0             # TP2: +N%（妖币典型涨幅）
    tp1_close_ratio: float = 0.5       # TP1 平仓比例
    hard_stop_pct: float = 5.5         # 硬止损: -N%
    trail_activate_pct: float = 8.0    # 移动止损激活: +N%
    trail_retrace_ratio: float = 0.35  # 移动止损回撤比
    max_hold_hours: int = 36           # 最大持仓（妖币爆发快）

    # ── 仓位 ──
    default_stake: float = 30.0
    leverage: int = 5                  # 做多杠杆

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'PrePumpParams':
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in valid})

    def get_optimization_space(self) -> Dict[str, Dict[str, Any]]:
        return {
            'vol_spike_ratio': {'type': 'float', 'low': 1.5, 'high': 4.0, 'step': 0.5},
            'oi_spike_pct': {'type': 'float', 'low': 1.5, 'high': 5.0, 'step': 0.5},
            'signal_threshold': {'type': 'int', 'low': 2, 'high': 5},
            'tp1_pct': {'type': 'float', 'low': 4.0, 'high': 12.0, 'step': 1.0},
            'tp2_pct': {'type': 'float', 'low': 12.0, 'high': 30.0, 'step': 2.0},
            'hard_stop_pct': {'type': 'float', 'low': 3.0, 'high': 8.0, 'step': 0.5},
            'max_hold_hours': {'type': 'int', 'low': 12, 'high': 48},
            'trail_activate_pct': {'type': 'float', 'low': 5.0, 'high': 12.0, 'step': 1.0},
        }
