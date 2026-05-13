#!/usr/bin/env python3
"""
数据模型定义 v4.1
用 dataclass 明确字段，防止拼写错误导致的静默失败。
新增：杠杆字段、硬止损、CloseType 枚举

v4.1 语义约定：
  - Trade.pnl = "剩余仓位"的盈亏（不含 tp1_locked_pnl）
  - 总盈亏 = tp1_locked_pnl + pnl（见 total_realized_pnl 属性）
  - Trade.close_type = 机器可读的关闭原因枚举（close_reason 为展示字符串）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional

from common import utcnow_iso
import config


class CloseType(str, Enum):
    """
    交易关闭原因枚举（机器可读）。
    继承 str 以便 JSON 序列化时直接写成字符串。
    """
    HARD_STOP = 'hard_stop'
    TP1 = 'tp1'              # 仅用于标注，TP1 本身不关闭交易
    TP2 = 'tp2'
    TRAIL_STOP = 'trail_stop'
    BREAKEVEN_STOP = 'breakeven_stop'
    TIME_STOP = 'time_stop'
    MANUAL = 'manual'

    @classmethod
    def is_stop_loss(cls, value) -> bool:
        """判断是否为止损类平仓（用于冷却期逻辑）"""
        if value is None:
            return False
        v = value.value if isinstance(value, cls) else str(value)
        return v in (cls.HARD_STOP.value, cls.TRAIL_STOP.value, cls.TIME_STOP.value)


@dataclass
class Candidate:
    """扫描候选币"""
    symbol: str
    price: float
    vol24h: float
    pct24h: float
    rsi_1d: float
    rsi_4h: Optional[float] = None
    rsi_4h_peak: Optional[float] = None
    oi_change: float = 0.0          # OI 24h 变化 %
    funding_rate: float = 0.0       # 资金费率 %/8h
    yao_score: int = 0              # 妖币评分 0~3
    added_at: str = field(default_factory=utcnow_iso)
    triggered: bool = False
    trigger_type: Optional[str] = None   # 'abandon' | '4h_rsi'
    trigger_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Candidate:
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered)


@dataclass
class Trade:
    """影子交易记录（支持杠杆）"""
    id: str
    symbol: str
    direction: str                   # SHORT | LONG
    entry_price: float
    stake: float = config.DEFAULT_STAKE      # 保证金
    leverage: int = config.LEVERAGE          # 杠杆倍数
    notional: float = 0.0                    # 名义仓位 = stake × leverage
    shares: float = 0.0
    opened_at: str = field(default_factory=utcnow_iso)
    status: str = 'open'             # open | closed
    source: str = '扫描器自动开仓'
    reason: str = ''
    strategy: str = 'short_overbought'  # 策略标记

    # 止盈档位
    take_profit_1: float = 0.0       # TP1 价格
    take_profit_2: float = 0.0       # TP2 价格
    tp1_triggered: bool = False
    tp1_locked_pnl: float = 0.0      # TP1 锁定的已实现盈利
    stake_remaining: float = config.DEFAULT_STAKE

    # 硬止损
    hard_stop_price: Optional[float] = None   # 无条件止损价

    # 移动止损
    best_pnl_pct: float = 0.0
    trail_stop_price: Optional[float] = None

    # 其他止损
    stop_loss: Optional[float] = None
    max_hold_days: int = config.MAX_HOLD_DAYS

    # 结算
    pnl: float = 0.0
    current_price: Optional[float] = None
    closed_at: Optional[str] = None
    close_reason: Optional[str] = None
    close_type: Optional[str] = None   # CloseType 枚举值（机器可读），用于冷却期/统计

    # 实盘路由（v4.2）
    exchange: str = 'shadow'          # 'binance' | 'okx' | 'shadow'（纸上交易）
    live_order_id: Optional[str] = None   # 开仓的交易所订单 ID（纸上交易为 None）
    close_order_id: Optional[str] = None  # 平仓的交易所订单 ID

    @classmethod
    def create_short(cls, symbol: str, price: float, reason: str = '',
                     stake: float = config.DEFAULT_STAKE,
                     leverage: int = config.LEVERAGE,
                     exchange: str = 'shadow',
                     live_order_id: Optional[str] = None) -> Trade:
        """工厂方法：创建做空交易（带杠杆 + 硬止损）"""
        notional = stake * leverage
        hard_stop = round(price * (1 + config.HARD_STOP_LOSS_PCT / 100), 6)
        # ID 加交易所后缀，避免 both 模式下两所同币同秒 ID 冲突
        ex_tag = exchange[:2].upper() if exchange != 'shadow' else 'SH'
        return cls(
            id=f"SCAN-SHORT-{symbol.replace('/USDT', '').replace('/', '')}-{ex_tag}-{int(time.time())}",
            symbol=symbol,
            direction='SHORT',
            entry_price=price,
            stake=stake,
            leverage=leverage,
            notional=notional,
            shares=round(notional / price, 4) if price > 0 else 0,
            reason=reason,
            strategy='short_overbought',
            take_profit_1=round(price * config.TP1_MULTIPLIER, 6),
            take_profit_2=round(price * config.TP2_MULTIPLIER, 6),
            stake_remaining=stake,
            hard_stop_price=hard_stop,
            max_hold_days=config.MAX_HOLD_DAYS,
            exchange=exchange,
            live_order_id=live_order_id,
        )

    @property
    def notional_remaining(self) -> float:
        """剩余名义仓位"""
        return self.stake_remaining * self.leverage

    @property
    def total_realized_pnl(self) -> float:
        """总已实现盈亏 = TP1 锁定利润 + 剩余仓位盈亏"""
        return self.tp1_locked_pnl + self.pnl

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Trade:
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        # 兼容旧字段
        if 'take_profit' in d and 'take_profit_2' not in filtered:
            filtered['take_profit_2'] = d['take_profit']
        # 兼容无杠杆字段的旧数据
        if 'leverage' not in filtered:
            filtered['leverage'] = config.LEVERAGE
        if 'notional' not in filtered:
            filtered['notional'] = filtered.get('stake', config.DEFAULT_STAKE) * filtered['leverage']
        # 兼容无 exchange 字段的旧数据（v4.2 之前）
        if 'exchange' not in filtered:
            filtered['exchange'] = 'shadow'
        return cls(**filtered)


# ══════════════════════════════════════════════════════════════════
#  已废弃：FundingTrade / LowRiskTrade
# ══════════════════════════════════════════════════════════════════
# 这两个策略在 v4.1 之前被整体移除（见 README：当前仅做空策略）。
# 保留的 Python 源码和 config 已废弃字段都已清理。
# 如果需要旧数据的归档读取，字段在历史 commit 中仍可查阅。
