#!/usr/bin/env python3
"""
每日风控模块 v1.0
功能：
  - 单日最大亏损限制（达到后当日禁止开仓）
  - 单日最大开仓次数限制
  - 连续亏损暂停（连亏 N 次暂停 24h）
  - 最大持仓占比限制
  - 所有检查通过才允许开仓
"""

from dataclasses import dataclass, field, asdict
from typing import Optional

import config
from common import (
    RISK_FILE, TRADES_FILE,
    setup_logger, send_tg, atomic_write_json, load_json,
    utcnow_iso, today_str, parse_iso, utcnow,
)

logger = setup_logger("risk_control")


@dataclass
class RiskState:
    """每日风控状态"""
    date: str = field(default_factory=today_str)
    daily_loss: float = 0.0          # 当日已实现亏损累计
    daily_trades_opened: int = 0     # 当日已开仓次数
    consecutive_losses: int = 0      # 连续亏损次数（跨日）
    paused_until: Optional[str] = None  # 暂停截止时间（ISO）
    total_open_stake: float = 0.0    # 当前持仓总保证金

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'RiskState':
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered)


def load_risk_state() -> RiskState:
    """加载风控状态，如果日期变了则重置当日计数"""
    data = load_json(RISK_FILE, {})
    if not data:
        return RiskState()

    state = RiskState.from_dict(data)

    # 新的一天：重置当日计数（但连亏次数和暂停时间保留）
    if state.date != today_str():
        state.date = today_str()
        state.daily_loss = 0.0
        state.daily_trades_opened = 0
        save_risk_state(state)

    return state


def save_risk_state(state: RiskState) -> None:
    """持久化风控状态"""
    atomic_write_json(RISK_FILE, state.to_dict())


def can_open_trade(stake: float = config.DEFAULT_STAKE) -> tuple:
    """
    检查是否允许开仓。

    返回: (allowed: bool, reason: str)
    """
    state = load_risk_state()

    # 1. 检查暂停状态
    if state.paused_until:
        pause_end = parse_iso(state.paused_until)
        if utcnow() < pause_end:
            remaining = (pause_end - utcnow()).total_seconds() / 3600
            reason = f"风控暂停中（连亏{state.consecutive_losses}次），剩余{remaining:.1f}小时"
            logger.warning(f"🚫 {reason}")
            return False, reason

        # 暂停已过期，重置
        state.paused_until = None
        state.consecutive_losses = 0
        save_risk_state(state)

    # 2. 检查单日最大亏损
    if state.daily_loss >= config.RISK_MAX_DAILY_LOSS:
        reason = f"单日亏损已达上限（{state.daily_loss:.1f}U >= {config.RISK_MAX_DAILY_LOSS}U）"
        logger.warning(f"🚫 {reason}")
        return False, reason

    # 3. 检查单日最大开仓次数
    if state.daily_trades_opened >= config.RISK_MAX_DAILY_TRADES:
        reason = f"单日开仓次数已达上限（{state.daily_trades_opened} >= {config.RISK_MAX_DAILY_TRADES}）"
        logger.warning(f"🚫 {reason}")
        return False, reason

    # 4. 检查最大持仓占比
    max_position = config.ACCOUNT_BALANCE * config.RISK_MAX_POSITION_PCT
    if state.total_open_stake + stake > max_position:
        reason = (
            f"持仓占比超限（当前{state.total_open_stake:.0f}U + 新增{stake:.0f}U "
            f"> 上限{max_position:.0f}U）"
        )
        logger.warning(f"🚫 {reason}")
        return False, reason

    return True, "OK"


def record_trade_opened(stake: float = config.DEFAULT_STAKE) -> None:
    """记录开仓事件"""
    state = load_risk_state()
    state.daily_trades_opened += 1
    state.total_open_stake += stake
    save_risk_state(state)
    logger.info(f"📝 记录开仓：今日第{state.daily_trades_opened}单，持仓{state.total_open_stake:.0f}U")


def record_trade_closed(pnl: float, stake: float = config.DEFAULT_STAKE) -> None:
    """
    记录平仓事件，更新亏损累计和连亏计数。
    pnl < 0 表示亏损。
    """
    state = load_risk_state()

    # 更新持仓总额
    state.total_open_stake = max(0, state.total_open_stake - stake)

    if pnl < 0:
        # 记录亏损
        state.daily_loss += abs(pnl)
        state.consecutive_losses += 1
        logger.info(
            f"📉 记录亏损：{pnl:.2f}U | 今日累计亏损{state.daily_loss:.1f}U | "
            f"连亏{state.consecutive_losses}次"
        )

        # 连亏暂停
        if state.consecutive_losses >= config.RISK_CONSECUTIVE_LOSS_PAUSE:
            from datetime import timedelta
            pause_end = utcnow() + timedelta(hours=config.RISK_PAUSE_HOURS)
            state.paused_until = pause_end.isoformat()
            logger.warning(
                f"🚨 连亏{state.consecutive_losses}次，暂停开仓{config.RISK_PAUSE_HOURS}小时"
            )
            send_tg(
                f"🚨 <b>风控警告：连亏暂停</b>\n\n"
                f"连续亏损 {state.consecutive_losses} 次\n"
                f"今日累计亏损：{state.daily_loss:.1f}U\n"
                f"暂停开仓至：{state.paused_until[:16]} UTC\n\n"
                f"冷静等待，不要追单 ⏸️"
            )

        # 单日亏损告警
        if state.daily_loss >= config.RISK_MAX_DAILY_LOSS:
            send_tg(
                f"🛑 <b>风控警告：今日停止交易</b>\n\n"
                f"今日累计亏损：{state.daily_loss:.1f}U\n"
                f"已达上限 {config.RISK_MAX_DAILY_LOSS}U\n"
                f"今日不再开新仓，明天重新来过 💤"
            )
    else:
        # 盈利：重置连亏计数
        state.consecutive_losses = 0
        logger.info(f"📈 记录盈利：+{pnl:.2f}U | 连亏重置为0")

    save_risk_state(state)


def refresh_open_stake() -> None:
    """从交易文件重新计算当前持仓总额（用于启动时同步）"""
    trades = load_json(TRADES_FILE, [])
    total = sum(t.get('stake_remaining', t.get('stake', 0))
                for t in trades if t.get('status') == 'open')

    state = load_risk_state()
    state.total_open_stake = total
    save_risk_state(state)
    logger.info(f"🔄 同步持仓总额：{total:.0f}U")


def get_risk_summary() -> str:
    """获取风控状态摘要（用于日报）"""
    state = load_risk_state()
    lines = [
        f"📋 <b>风控状态</b>",
        f"  今日亏损：{state.daily_loss:.1f} / {config.RISK_MAX_DAILY_LOSS}U",
        f"  今日开仓：{state.daily_trades_opened} / {config.RISK_MAX_DAILY_TRADES}次",
        f"  连亏次数：{state.consecutive_losses} / {config.RISK_CONSECUTIVE_LOSS_PAUSE}次",
        f"  持仓占用：{state.total_open_stake:.0f}U",
    ]
    if state.paused_until:
        lines.append(f"  ⚠️ 暂停至：{state.paused_until[:16]} UTC")
    return "\n".join(lines)
