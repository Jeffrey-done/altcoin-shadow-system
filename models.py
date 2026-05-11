#!/usr/bin/env python3
"""
数据模型定义 v4.0
用 dataclass 明确字段，防止拼写错误导致的静默失败。
新增：杠杆字段、硬止损、FundingTrade 模型
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from common import utcnow_iso
import config


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

    @classmethod
    def create_short(cls, symbol: str, price: float, reason: str = '',
                     stake: float = config.DEFAULT_STAKE,
                     leverage: int = config.LEVERAGE) -> Trade:
        """工厂方法：创建做空交易（带杠杆 + 硬止损）"""
        notional = stake * leverage
        hard_stop = round(price * (1 + config.HARD_STOP_LOSS_PCT / 100), 6)
        return cls(
            id=f"SCAN-SHORT-{symbol.replace('/USDT', '').replace('/', '')}-{int(time.time())}",
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
        return cls(**filtered)


@dataclass
class FundingTrade:
    """资金费率套利交易"""
    id: str
    symbol: str
    direction: str = 'LONG'          # 通常做多吃负费率
    entry_price: float = 0.0
    stake: float = config.FUNDING_ARB_STAKE
    leverage: int = config.FUNDING_ARB_LEVERAGE
    notional: float = 0.0
    funding_rate: float = 0.0        # 开仓时的费率（%/8h）
    expected_income: float = 0.0     # 预期费率收入
    opened_at: str = field(default_factory=utcnow_iso)
    status: str = 'open'             # open | closed
    strategy: str = 'funding_arb'

    # 止损
    hard_stop_price: Optional[float] = None
    max_hold_hours: float = config.FUNDING_ARB_MAX_HOLD_HOURS

    # 结算
    pnl: float = 0.0                 # 方向性盈亏
    funding_income: float = 0.0      # 费率收入
    total_pnl: float = 0.0           # 总盈亏 = pnl + funding_income
    current_price: Optional[float] = None
    closed_at: Optional[str] = None
    close_reason: Optional[str] = None

    @classmethod
    def create_long(cls, symbol: str, price: float, funding_rate: float,
                    stake: float = config.FUNDING_ARB_STAKE,
                    leverage: int = config.FUNDING_ARB_LEVERAGE) -> FundingTrade:
        """工厂方法：做多吃负费率"""
        notional = stake * leverage
        # 止损价：价格下跌 FUNDING_ARB_STOP_LOSS_PCT% 触发
        hard_stop = round(price * (1 - config.FUNDING_ARB_STOP_LOSS_PCT / 100), 6)
        # 预期费率收入 = 名义仓位 × |费率|（空头付给多头）
        expected_income = round(notional * abs(funding_rate) / 100, 4)
        return cls(
            id=f"FUND-LONG-{symbol.replace('/USDT', '').replace('/', '')}-{int(time.time())}",
            symbol=symbol,
            direction='LONG',
            entry_price=price,
            stake=stake,
            leverage=leverage,
            notional=notional,
            funding_rate=funding_rate,
            expected_income=expected_income,
            hard_stop_price=hard_stop,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> FundingTrade:
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered)
