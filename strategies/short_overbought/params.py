"""
做空超买策略参数定义
集中管理所有策略参数，支持序列化、验证和优化空间定义。
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, Any


@dataclass
class ShortOverboughtParams:
    """做空超买策略参数集"""

    # ── 扫描过滤 ──
    vol_min: float = 500_000           # 24h 成交量下限 (USDT)
    price_max: float = 1.0             # 价格上限（只做小币 <1U）
    pct_24h_min: float = 10.0          # 24h 涨幅最低要求 (%)

    # ── RSI 参数 ──
    rsi_period: int = 14               # RSI 计算周期
    daily_rsi_min: float = 75.0        # 日线 RSI 超买阈值
    h4_rsi_enter: float = 70.0         # 4h RSI 回落进入阈值
    h4_rsi_drop: float = 10.0          # 4h RSI 需从峰值回落的点数
    h4_rsi_peak_lookback: int = 10     # 4h RSI 峰值回溯 K 线数

    # ── 妖币识别 ──
    oi_change_min: float = 0.30        # OI 24h 涨幅下限 (30%)
    funding_max: float = 0.05          # 资金费率上限
    funding_min: float = -0.03         # 资金费率下限
    funding_hot: float = 0.03          # 多头过热阈值 (%/8h)

    # ── 弃盘点 ──
    abandon_body_drop_pct: float = 3.0 # 单根 1H K 线实体下跌阈值 (%)
    abandon_consecutive: int = 2       # 连续满足的 K 线数
    abandon_oi_drop_pct: float = 0.02  # OI 下降比例阈值

    # ── 止盈止损 ──
    tp1_pct: float = 5.0              # TP1 目标 (价格跌 5%)
    tp2_pct: float = 8.0              # TP2 目标 (价格跌 8%)
    tp1_close_ratio: float = 0.5      # TP1 平仓比例
    hard_stop_pct: float = 5.0        # 硬止损 (价格反弹 5%)
    trail_activate_pct: float = 3.0   # 移动止损激活阈值
    trail_retrace_ratio: float = 0.4  # 移动止损回撤比
    max_hold_hours: int = 24          # 最大持仓时间

    # ── 信号评分 ──
    score_full_threshold: int = 70     # ≥70分：全仓
    score_half_threshold: int = 40     # 40~69分：半仓
    score_skip_threshold: int = 40     # <40分：跳过

    # ── BTC 趋势过滤 ──
    btc_filter_enabled: bool = True
    btc_crash_threshold: float = -5.0  # BTC 24h 跌幅超此值暂停做空
    btc_pump_threshold: float = 8.0    # BTC 24h 涨幅超此值信号加分

    # ── OKX 交叉验证 ──
    okx_cross_validate_enabled: bool = False
    okx_cross_validate_bonus: int = 8

    # ── 仓位 ──
    default_stake: float = 30.0       # 默认保证金 (USDT)
    leverage: int = 10                # 杠杆倍数

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'ShortOverboughtParams':
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered)

    def get_optimization_space(self) -> Dict[str, Dict[str, Any]]:
        """
        返回 Optuna 参数优化空间定义。
        只包含对策略性能有显著影响的关键参数。
        """
        return {
            'daily_rsi_min': {'type': 'int', 'low': 70, 'high': 85},
            'h4_rsi_drop': {'type': 'int', 'low': 5, 'high': 20},
            'tp1_pct': {'type': 'float', 'low': 3.0, 'high': 8.0, 'step': 0.5},
            'tp2_pct': {'type': 'float', 'low': 5.0, 'high': 15.0, 'step': 0.5},
            'hard_stop_pct': {'type': 'float', 'low': 3.0, 'high': 8.0, 'step': 0.5},
            'trail_retrace_ratio': {'type': 'float', 'low': 0.2, 'high': 0.6, 'step': 0.05},
            'max_hold_hours': {'type': 'int', 'low': 12, 'high': 72},
            'score_full_threshold': {'type': 'int', 'low': 60, 'high': 85},
            'score_half_threshold': {'type': 'int', 'low': 30, 'high': 60},
            'trail_activate_pct': {'type': 'float', 'low': 1.5, 'high': 5.0, 'step': 0.5},
            'pct_24h_min': {'type': 'float', 'low': 5.0, 'high': 20.0, 'step': 1.0},
        }
