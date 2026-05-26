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
    pending_open: bool = False
    pending_opened_at: Optional[str] = None
    pending_open_retries: int = 0

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
    tp1_stake_released: bool = False # H1: TP1 半仓 stake 是否已从 risk 扣减（防双扣）
    stake_remaining: float = config.DEFAULT_STAKE

    # 硬止损
    hard_stop_price: Optional[float] = None   # 无条件止损价
    hard_stop_pct: float = 0.0        # M4: 实际使用的硬止损百分比（ATR 动态值或 config.HARD_STOP_LOSS_PCT）
    hard_stop_source: str = 'fixed'   # M4: 'atr' | 'fixed' — 用于审计止损来源

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
    close_order_id: Optional[str] = None  # 最终平仓的交易所订单 ID
    tp1_close_order_id: Optional[str] = None  # TP1 半仓平仓订单 ID
    # 多账户隔离（v4.3）
    account_id: str = ''              # 所属账户 ID（空=旧数据/单账户兼容）

    # 幂等键（v5.1，配合 in-flight journal 防幽灵仓位）
    client_order_id: str = ''         # 下单时使用的 clOrdId / newClientOrderId

    # 滑点追踪（H10）
    ref_price_at_order: float = 0.0   # 下单前的 ticker 参考价
    slippage_pct: float = 0.0         # 入场滑点：abs(entry - ref)/ref * 100

    # TP1 实际成交追踪（H7）
    tp1_closed_shares: float = 0.0    # TP1 实际平掉的币数量（用交易所返回的 filled）
    tp1_exit_price: float = 0.0       # TP1 实际平仓均价

    # 平仓滑点追踪（H10 续作）
    # 语义与入场滑点对齐：ref_price = 触发评估时的 ticker 价，实际成交价来自交易所 fill
    # shadow 交易和影子账户不发真单 → 保持 0
    tp1_exit_ref_price: float = 0.0   # TP1 触发评估时的 ticker 参考价
    tp1_slippage_pct: float = 0.0     # TP1 平仓滑点 %：abs(fill - ref) / ref * 100
    exit_ref_price: float = 0.0       # 最终平仓（TP2 / 硬止损 / 移动止损 / 时间止损）触发时 ticker 参考价
    exit_slippage_pct: float = 0.0    # 最终平仓滑点 %：abs(fill - ref) / ref * 100
    protect_stop_algo_id: Optional[str] = None
    protect_tp_algo_id: Optional[str] = None
    protect_stage: str = ''

    @classmethod
    def create_short(cls, symbol: str, price: float, reason: str = '',
                     stake: float = config.DEFAULT_STAKE,
                     leverage: int = config.LEVERAGE,
                     exchange: str = 'shadow',
                     live_order_id: Optional[str] = None,
                     client_order_id: Optional[str] = None,
                     ref_price_at_order: Optional[float] = None,
                     slippage_pct: float = 0.0,
                     account_id: Optional[str] = None,
                     tp1_multiplier: Optional[float] = None,
                     tp2_multiplier: Optional[float] = None,
                     hard_stop_loss_pct: Optional[float] = None,
                     hard_stop_source: str = 'fixed',
                     max_hold_days: Optional[int] = None) -> Trade:
        """工厂方法：创建做空交易（带杠杆 + 硬止损）

        参数:
          client_order_id: 若调用方已生成幂等键（配合 in-flight journal），
            直接传入作为 Trade.id 的一部分，保证 journal / trade / 交易所三端一致。
          ref_price_at_order: 下单前的 ticker 参考价（用于事后分析滑点成本）
          slippage_pct: 实际滑点百分比（abs(price - ref_price)/ref_price * 100）
          hard_stop_loss_pct: 硬止损百分比；None 时取 config.HARD_STOP_LOSS_PCT
          hard_stop_source: 'atr' | 'fixed' — M4 修复：调用方传入 ATR 动态值时
            应同时把 source 设为 'atr',方便审计、weekly_report 拆分统计止损来源。
        """
        import secrets as _secrets
        from common import get_current_account_id
        notional = stake * leverage
        if tp1_multiplier is None:
            tp1_multiplier = config.TP1_MULTIPLIER
        if tp2_multiplier is None:
            tp2_multiplier = config.TP2_MULTIPLIER
        if hard_stop_loss_pct is None:
            hard_stop_loss_pct = config.HARD_STOP_LOSS_PCT
        if max_hold_days is None:
            max_hold_days = config.MAX_HOLD_DAYS

        hard_stop = round(price * (1 + hard_stop_loss_pct / 100), 6)
        # ID 加交易所后缀 + 毫秒时间戳 + 3字节随机 token，
        # 确保多账户并行开仓时同币同秒不会冲突
        ex_tag = exchange[:2].upper() if exchange != 'shadow' else 'SH'
        ts_ms = int(time.time() * 1000)
        rand = _secrets.token_hex(3)
        trade_id = f"SCAN-SHORT-{symbol.replace('/USDT', '').replace('/', '')}-{ex_tag}-{ts_ms}-{rand}"
        return cls(
            id=trade_id,
            symbol=symbol,
            direction='SHORT',
            entry_price=price,
            stake=stake,
            leverage=leverage,
            notional=notional,
            shares=round(notional / price, 4) if price > 0 else 0,
            reason=reason,
            strategy='short_overbought',
            take_profit_1=round(price * tp1_multiplier, 6),
            take_profit_2=round(price * tp2_multiplier, 6),
            stake_remaining=stake,
            hard_stop_price=hard_stop,
            hard_stop_pct=round(hard_stop_loss_pct, 4),
            hard_stop_source=hard_stop_source,
            max_hold_days=max_hold_days,
            exchange=exchange,
            live_order_id=live_order_id,
            account_id=account_id if account_id is not None else get_current_account_id(),
            client_order_id=client_order_id or '',
            ref_price_at_order=ref_price_at_order if ref_price_at_order else price,
            slippage_pct=round(slippage_pct, 4),
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
        # 兼容无 account_id 字段的旧数据（v4.3 之前）
        if 'account_id' not in filtered:
            filtered['account_id'] = ''
        return cls(**filtered)


# ══════════════════════════════════════════════════════════════════
#  历史归档：FundingTrade / LowRiskTrade
# ══════════════════════════════════════════════════════════════════
# 这两个策略在 v4.0 时存在，v4.1 整体移除。当前 v5.x 多策略架构
# 是基于 BaseStrategy + StrategyRegistry 重新打开的，目录在 strategies/。
# 当前内置策略：
#   - short_overbought  (SHORT 方向)
#   - long_oversold     (LONG 方向)
#   - prepump_sniffer   (LONG 方向)
