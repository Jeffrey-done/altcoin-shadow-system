#!/usr/bin/env python3
"""
每日风控模块 v2.0
功能：
  - 单日最大亏损限制（达到后当日禁止开仓）
  - 单日最大开仓次数限制
  - 连续亏损暂停（连亏 N 次暂停 24h）
  - 最大持仓占比限制（简化：max = dynamic_balance * 50%）
  - 所有检查通过才允许开仓
"""

from dataclasses import dataclass, field, asdict
from typing import Optional

import config
from common import (
    RISK_FILE, TRADES_FILE,
    setup_logger, send_tg, atomic_write_json, load_json,
    utcnow_iso, today_str, parse_iso, utcnow,
    get_dynamic_balance,
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
    """加载风控状态，如果日期变了则重置当日计数，并同步实际持仓"""
    data = load_json(RISK_FILE, {})
    if not data:
        state = RiskState()
        # 首次加载：从交易文件同步实际持仓
        state.total_open_stake = _calc_actual_open_stake()
        save_risk_state(state)
        return state

    state = RiskState.from_dict(data)

    # 新的一天：重置当日计数（但连亏次数和暂停时间保留）
    if state.date != today_str():
        state.date = today_str()
        state.daily_loss = 0.0
        state.daily_trades_opened = 0
        # 每日重置时同步实际持仓（防止累积偏差）
        state.total_open_stake = _calc_actual_open_stake()
        save_risk_state(state)

    return state


def _calc_actual_open_stake() -> float:
    """从交易文件计算实际持仓总保证金"""
    total = 0.0

    # 做空交易
    trades = load_json(TRADES_FILE, [])
    total += sum(
        t.get('stake_remaining', t.get('stake', 0))
        for t in trades if t.get('status') == 'open'
    )

    return total


def _calc_today_realized_loss() -> float:
    """
    从交易文件计算"今日"已实现亏损（仅取 pnl<0 的绝对值之和）。
    用于对账校验 risk_state.daily_loss。
    注意：只统计今日 UTC 平仓的交易。
    """
    trades = load_json(TRADES_FILE, [])
    today = today_str()
    total_loss = 0.0
    for t in trades:
        if t.get('status') != 'closed':
            continue
        closed_at = t.get('closed_at', '')
        if not closed_at.startswith(today):
            continue
        realized = t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        if realized < 0:
            total_loss += abs(realized)
    return round(total_loss, 2)


def _calc_today_trades_opened() -> int:
    """从交易文件统计今日新开的仓位数（用于对账）"""
    trades = load_json(TRADES_FILE, [])
    today = today_str()
    return sum(1 for t in trades if t.get('opened_at', '').startswith(today))


def reconcile_risk_state(notify: bool = False) -> dict:
    """
    对账：从 trades 文件反算今日真实的 daily_loss / daily_trades_opened /
    total_open_stake，若与 risk_state.json 不一致则修正。

    这是防止"幽灵亏损"（risk_state 被改了但 trades 没记录）的最后一道防线。
    scheduler 启动时会调用一次，每次 can_open_trade 时也做轻量检查。

    参数:
      notify: True 时如果发现漂移会推送 TG 告警
    返回: 修正前后的 diff（空 dict 表示无漂移）
    """
    state = load_risk_state()
    expected_loss = _calc_today_realized_loss()
    expected_trades = _calc_today_trades_opened()
    expected_stake = _calc_actual_open_stake()

    diff = {}
    if abs(state.daily_loss - expected_loss) > 0.01:
        diff['daily_loss'] = (state.daily_loss, expected_loss)
        state.daily_loss = expected_loss

    if state.daily_trades_opened != expected_trades:
        diff['daily_trades_opened'] = (state.daily_trades_opened, expected_trades)
        state.daily_trades_opened = expected_trades

    if abs(state.total_open_stake - expected_stake) > 0.01:
        diff['total_open_stake'] = (state.total_open_stake, expected_stake)
        state.total_open_stake = expected_stake

    if diff:
        save_risk_state(state)
        msg_lines = ["🔧 风控状态对账修正"]
        for k, (old, new) in diff.items():
            msg_lines.append(f"  {k}: {old} → {new}")
        logger.warning(" | ".join(msg_lines))
        if notify:
            send_tg(
                "🔧 <b>风控状态自动对账</b>\n\n"
                "检测到 risk_state.json 与交易记录不一致，已自动修正：\n"
                + "\n".join(f"• {k}: <code>{old}</code> → <code>{new}</code>"
                            for k, (old, new) in diff.items())
                + "\n\n可能原因：上次崩溃/并发写入导致的状态漂移。"
            )

    return diff


def save_risk_state(state: RiskState) -> None:
    """持久化风控状态"""
    atomic_write_json(RISK_FILE, state.to_dict())


def can_open_trade(stake: float = config.DEFAULT_STAKE, strategy: str = 'short') -> tuple:
    """
    检查是否允许开仓。

    参数:
      stake: 本次开仓保证金
      strategy: 策略类型（保留参数向后兼容）

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

    # 4. 检查最大持仓占比（简化：max = dynamic_balance * RISK_MAX_POSITION_PCT）
    # 先同步实际持仓（防止累积偏差导致误判）
    actual_stake = _calc_actual_open_stake()
    if state.total_open_stake != actual_stake:
        logger.info(f"🔄 持仓自动修正：{state.total_open_stake:.0f}U → {actual_stake:.0f}U")
        state.total_open_stake = actual_stake
        save_risk_state(state)

    dynamic_bal = get_dynamic_balance()
    max_position = dynamic_bal * config.RISK_MAX_POSITION_PCT
    if state.total_open_stake + stake > max_position:
        reason = (
            f"持仓占比超限（当前{state.total_open_stake:.0f}U + 新增{stake:.0f}U "
            f"> 上限{max_position:.0f}U）"
        )
        logger.warning(f"🚫 {reason}")
        return False, reason

    return True, "OK"


def record_trade_opened(stake: float = config.DEFAULT_STAKE, strategy: str = 'short') -> None:
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
    """从交易文件重新计算当前持仓总额（用于启动时同步或手动修正）"""
    state = load_risk_state()
    actual = _calc_actual_open_stake()
    if state.total_open_stake != actual:
        logger.info(f"🔄 持仓修正：{state.total_open_stake:.0f}U → {actual:.0f}U")
        state.total_open_stake = actual
        save_risk_state(state)
    else:
        logger.info(f"🔄 持仓同步：{actual:.0f}U（无偏差）")


def is_in_cooldown(symbol: str) -> tuple:
    """
    检查某币种是否在止损平仓后的冷却期内。

    使用 close_type 枚举判断（而非硬编码中文字符串），
    旧数据如果没有 close_type 字段，则回退到 close_reason 字符串匹配。

    Returns: (bool, str) - (是否冷却中, 原因描述)
    """
    from datetime import timedelta
    from models import CloseType
    trades = load_json(TRADES_FILE, [])
    now = utcnow()
    cooldown_hours = config.COOLDOWN_HOURS

    for t in reversed(trades):
        if t.get('symbol') != symbol:
            continue
        if t.get('status') != 'closed':
            continue

        # 优先用 close_type 枚举判断
        ct = t.get('close_type')
        if ct is not None:
            if not CloseType.is_stop_loss(ct):
                continue
        else:
            # 旧数据回退：字符串包含 '止损' / 'stop'
            close_reason = t.get('close_reason', '').lower()
            if '止损' not in close_reason and 'stop' not in close_reason:
                continue

        # 找到了止损平仓记录，检查时间
        closed_at = t.get('closed_at', '')
        if not closed_at:
            continue
        try:
            closed_dt = parse_iso(closed_at)
            hours_since = (now - closed_dt).total_seconds() / 3600
            if hours_since < cooldown_hours:
                remaining = cooldown_hours - hours_since
                reason = f"止损平仓后冷却中（{hours_since:.1f}h/{cooldown_hours}h，剩余{remaining:.1f}h）"
                return (True, reason)
        except Exception:
            continue

    return (False, "")


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
