#!/usr/bin/env python3
"""
数据模型定义
用 dataclass 明确字段，防止拼写错误导致的静默失败。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Optional, List

from common import utcnow_iso
from config import DEFAULT_STAKE, TP1_MULTIPLIER, TP2_MULTIPLIER


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
        # 过滤多余字段，兼容旧数据
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered)


@dataclass
class Trade:
    """影子交易记录"""
    id: str
    symbol: str
    direction: str                   # SHORT
    entry_price: float
    stake: float = DEFAULT_STAKE
    shares: float = 0.0
    opened_at: str = field(default_factory=utcnow_iso)
    status: str = 'open'             # open | closed
    source: str = '扫描器自动开仓'
    reason: str = ''

    # 止盈档位
    take_profit_1: float = 0.0       # TP1 价格
    take_profit_2: float = 0.0       # TP2 价格
    tp1_triggered: bool = False
    tp1_locked_pnl: float = 0.0      # TP1 锁定的已实现盈利（修复：累计到最终 PnL）
    stake_remaining: float = DEFAULT_STAKE

    # 移动止损
    best_pnl_pct: float = 0.0
    trail_stop_price: Optional[float] = None

    # 止损
    stop_loss: Optional[float] = None
    max_hold_days: int = 7

    # 结算
    pnl: float = 0.0
    current_price: Optional[float] = None
    closed_at: Optional[str] = None
    close_reason: Optional[str] = None

    @classmethod
    def create_short(cls, symbol: str, price: float, reason: str = '',
                     stake: float = DEFAULT_STAKE) -> Trade:
        """工厂方法：创建做空交易"""
        return cls(
            id=f"SCAN-SHORT-{symbol.replace('/USDT', '').replace('/', '')}-{int(time.time())}",
            symbol=symbol,
            direction='SHORT',
            entry_price=price,
            stake=stake,
            shares=round(stake / price, 4) if price > 0 else 0,
            reason=reason,
            take_profit_1=round(price * TP1_MULTIPLIER, 6),
            take_profit_2=round(price * TP2_MULTIPLIER, 6),
            stake_remaining=stake,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Trade:
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        # 兼容旧字段
        if 'take_profit' in d and 'take_profit_2' not in d:
            filtered['take_profit_2'] = d['take_profit']
        return cls(**filtered)

    @property
    def total_realized_pnl(self) -> float:
        """总已实现盈亏 = TP1 锁定利润 + 剩余仓位盈亏"""
        return self.tp1_locked_pnl + self.pnl
