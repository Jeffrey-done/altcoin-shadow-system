#!/usr/bin/env python3
"""
每日风控模块 v3.0 — 按账户隔离
功能：
  - 单日最大亏损限制（达到后当日禁止开仓）
  - 单日最大开仓次数限制
  - 连续亏损暂停（连亏 N 次暂停 24h）
  - 最大持仓占比限制（简化：max = dynamic_balance * 50%）
  - 所有检查通过才允许开仓

v3.0 变更：
  - 每个账户拥有独立的风控额度（交易次数、日亏损、连损次数）
  - 账户之间互不干扰
  - 向后兼容：不传 account_id 时使用活跃账户（单账户模式无感知升级）
  - risk_state.json 结构升级为 v2（自动从 v1 迁移）
"""

from dataclasses import dataclass, field, asdict
from typing import Optional

import config
from common import (
    RISK_FILE, TRADES_FILE,
    setup_logger, send_tg, atomic_write_json, load_json,
    today_str, parse_iso, utcnow,
    get_realized_balance, LockedJsonFile,
    get_current_account_id, filter_trades_by_account,
)

logger = setup_logger("risk_control")


# ══════════════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class RiskState:
    """单个账户的风控状态"""
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


# ══════════════════════════════════════════════════════════════════
#  数据持久化（v2 多账户格式）
# ══════════════════════════════════════════════════════════════════

def _resolve_account_id(account_id: Optional[str] = None) -> str:
    """解析 account_id，None 时使用当前活跃账户，空字符串作为全局默认"""
    if account_id is None:
        account_id = get_current_account_id()
    return account_id or '_default'


def _load_raw_risk() -> dict:
    """加载原始 risk_state.json，自动处理 v1→v2 迁移"""
    data = load_json(RISK_FILE, {})
    if not data:
        return {'_version': 2, 'accounts': {}}

    # 检测 v1 格式（没有 _version 字段，直接是 RiskState 的字段）
    if '_version' not in data and 'date' in data:
        # v1 → v2 迁移：把整个 data 当作默认账户的状态
        v2 = {'_version': 2, 'accounts': {'_default': data}}
        return v2

    return data


def _save_raw_risk(data: dict) -> None:
    """保存原始 risk_state.json"""
    data.setdefault('_version', 2)
    atomic_write_json(RISK_FILE, data)


def load_risk_state(account_id: Optional[str] = None) -> RiskState:
    """加载指定账户的风控状态，如果日期变了则重置当日计数"""
    acc_id = _resolve_account_id(account_id)
    raw = _load_raw_risk()
    acc_data = raw.get('accounts', {}).get(acc_id, {})

    if not acc_data:
        state = RiskState()
        state.total_open_stake = _calc_actual_open_stake(account_id)
        save_risk_state(state, account_id)
        return state

    state = RiskState.from_dict(acc_data)

    # 新的一天：重置当日计数（但连亏次数和暂停时间保留）
    if state.date != today_str():
        state.date = today_str()
        state.daily_loss = 0.0
        state.daily_trades_opened = 0
        state.total_open_stake = _calc_actual_open_stake(account_id)
        save_risk_state(state, account_id)

    return state


def save_risk_state(state: RiskState, account_id: Optional[str] = None) -> None:
    """持久化指定账户的风控状态"""
    acc_id = _resolve_account_id(account_id)
    with LockedJsonFile(RISK_FILE, default={}) as (raw, save):
        if '_version' not in raw and 'date' in raw:
            # v1 → v2 迁移
            raw = {'_version': 2, 'accounts': {'_default': raw}}
        raw.setdefault('_version', 2)
        raw.setdefault('accounts', {})
        raw['accounts'][acc_id] = state.to_dict()
        save(raw)


# ══════════════════════════════════════════════════════════════════
#  辅助函数
# ══════════════════════════════════════════════════════════════════

def _calc_actual_open_stake(account_id: Optional[str] = None) -> float:
    """从交易文件计算指定账户的实际持仓总保证金"""
    trades = load_json(TRADES_FILE, [])
    acc_id = _resolve_account_id(account_id)

    # 如果是 _default（旧单账户模式），不过滤
    if acc_id != '_default':
        trades = filter_trades_by_account(trades, acc_id)

    return sum(
        t.get('stake_remaining', t.get('stake', 0))
        for t in trades if t.get('status') == 'open'
    )


def _state_from_data(data: dict, account_id: Optional[str] = None) -> RiskState:
    """
    从原始 dict 构建 RiskState，处理日期翻转逻辑。
    供 LockedJsonFile 上下文中使用（已在锁内）。
    """
    acc_id = _resolve_account_id(account_id)

    # 处理 v1 格式
    if '_version' not in data and 'date' in data:
        # v1 格式：直接当作当前账户的状态
        if acc_id == '_default':
            acc_data = data
        else:
            acc_data = {}
    else:
        acc_data = data.get('accounts', {}).get(acc_id, {})

    if not acc_data:
        state = RiskState()
        state.total_open_stake = _calc_actual_open_stake(account_id)
        return state

    state = RiskState.from_dict(acc_data)

    # 新的一天：重置当日计数
    if state.date != today_str():
        state.date = today_str()
        state.daily_loss = 0.0
        state.daily_trades_opened = 0
        state.total_open_stake = _calc_actual_open_stake(account_id)

    return state


def _save_state_in_lock(raw: dict, state: RiskState, account_id: Optional[str] = None) -> dict:
    """在锁内更新 raw dict 中指定账户的状态，返回更新后的 raw"""
    acc_id = _resolve_account_id(account_id)

    # 确保是 v2 格式
    if '_version' not in raw and 'date' in raw:
        raw = {'_version': 2, 'accounts': {'_default': raw}}
    raw.setdefault('_version', 2)
    raw.setdefault('accounts', {})
    raw['accounts'][acc_id] = state.to_dict()
    return raw


# ══════════════════════════════════════════════════════════════════
#  对账
# ══════════════════════════════════════════════════════════════════

def _calc_today_realized_loss(account_id: Optional[str] = None) -> float:
    """计算指定账户今日已实现亏损"""
    trades = load_json(TRADES_FILE, [])
    acc_id = _resolve_account_id(account_id)
    if acc_id != '_default':
        trades = filter_trades_by_account(trades, acc_id)

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


def _calc_today_trades_opened(account_id: Optional[str] = None) -> int:
    """统计指定账户今日新开的仓位数"""
    trades = load_json(TRADES_FILE, [])
    acc_id = _resolve_account_id(account_id)
    if acc_id != '_default':
        trades = filter_trades_by_account(trades, acc_id)

    today = today_str()
    return sum(1 for t in trades if t.get('opened_at', '').startswith(today))


def reconcile_risk_state(account_id: Optional[str] = None, notify: bool = False) -> dict:
    """
    对账：从 trades 文件反算指定账户真实的 daily_loss / daily_trades_opened /
    total_open_stake，若与 risk_state 不一致则修正。
    """
    expected_loss = _calc_today_realized_loss(account_id)
    expected_trades = _calc_today_trades_opened(account_id)
    expected_stake = _calc_actual_open_stake(account_id)

    diff = {}
    with LockedJsonFile(RISK_FILE, default={}) as (data, save):
        state = _state_from_data(data, account_id)

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
            data = _save_state_in_lock(data, state, account_id)
            save(data)

    if diff:
        acc_id = _resolve_account_id(account_id)
        msg_lines = [f"🔧 风控状态对账修正 [{acc_id}]"]
        for k, (old, new) in diff.items():
            msg_lines.append(f"  {k}: {old} → {new}")
        logger.warning(" | ".join(msg_lines))
        if notify:
            send_tg(
                f"🔧 <b>风控状态自动对账</b> [{acc_id}]\n\n"
                "检测到 risk_state 与交易记录不一致，已自动修正：\n"
                + "\n".join(f"• {k}: <code>{old}</code> → <code>{new}</code>"
                            for k, (old, new) in diff.items())
            )

    return diff


# ══════════════════════════════════════════════════════════════════
#  核心风控检查
# ══════════════════════════════════════════════════════════════════

def can_open_trade(stake: float = config.DEFAULT_STAKE, strategy: str = 'short',
                   account_id: Optional[str] = None) -> tuple:
    """
    检查指定账户是否允许开仓。

    参数:
      stake: 本次开仓保证金
      strategy: 策略类型（保留参数向后兼容）
      account_id: 账户 ID（None 使用活跃账户）

    返回: (allowed: bool, reason: str)
    """
    # 所有读-判-写操作统一在锁内执行，避免并发 can_open_trade 重复清理 paused_until
    # 或 total_open_stake 双写漂移
    with LockedJsonFile(RISK_FILE, default={}) as (data, save):
        state = _state_from_data(data, account_id)
        dirty = False

        # 1. 检查暂停状态
        if state.paused_until:
            pause_end = parse_iso(state.paused_until)
            if utcnow() < pause_end:
                remaining = (pause_end - utcnow()).total_seconds() / 3600
                reason = f"风控暂停中（连亏{state.consecutive_losses}次），剩余{remaining:.1f}小时"
                logger.warning(f"🚫 {reason}")
                return False, reason
            # 暂停已过期，在锁内重置
            state.paused_until = None
            state.consecutive_losses = 0
            dirty = True

        # 2. 检查单日最大亏损
        if state.daily_loss >= config.RISK_MAX_DAILY_LOSS:
            reason = f"单日亏损已达上限（{state.daily_loss:.1f}U >= {config.RISK_MAX_DAILY_LOSS}U）"
            if dirty:
                data = _save_state_in_lock(data, state, account_id)
                save(data)
            logger.warning(f"🚫 {reason}")
            return False, reason

        # 3. 检查单日最大开仓次数
        if state.daily_trades_opened >= config.RISK_MAX_DAILY_TRADES:
            reason = f"单日开仓次数已达上限（{state.daily_trades_opened} >= {config.RISK_MAX_DAILY_TRADES}）"
            if dirty:
                data = _save_state_in_lock(data, state, account_id)
                save(data)
            logger.warning(f"🚫 {reason}")
            return False, reason

        # 4. 检查最大持仓占比（锁内同步持仓总额）
        actual_stake = _calc_actual_open_stake(account_id)
        if abs(state.total_open_stake - actual_stake) > 0.01:
            logger.info(f"🔄 持仓自动修正：{state.total_open_stake:.0f}U → {actual_stake:.0f}U")
            state.total_open_stake = actual_stake
            dirty = True

        # M2: 持仓占比风控基准改为"已实现余额"，不含浮动 TP1 锁定利润，
        # 防止 TP1 后上限放大形成加仓正反馈。（旧版本用 get_dynamic_balance，
        # 现已不再作为基准；移除了无用调用避免多一次余额查询。）
        realized_bal = get_realized_balance(account_id=_resolve_account_id(account_id))
        max_position = realized_bal * config.RISK_MAX_POSITION_PCT
        if state.total_open_stake + stake > max_position:
            reason = (
                f"持仓占比超限（当前{state.total_open_stake:.0f}U + 新增{stake:.0f}U "
                f"> 上限{max_position:.0f}U，基于已实现余额{realized_bal:.0f}U）"
            )
            if dirty:
                data = _save_state_in_lock(data, state, account_id)
                save(data)
            logger.warning(f"🚫 {reason}")
            return False, reason

        if dirty:
            data = _save_state_in_lock(data, state, account_id)
            save(data)

    return True, "OK"


def record_trade_opened(stake: float = config.DEFAULT_STAKE, strategy: str = 'short',
                        account_id: Optional[str] = None) -> None:
    """记录指定账户的开仓事件（全程加锁）"""
    with LockedJsonFile(RISK_FILE, default={}) as (data, save):
        state = _state_from_data(data, account_id)
        state.daily_trades_opened += 1
        state.total_open_stake += stake
        data = _save_state_in_lock(data, state, account_id)
        save(data)
    logger.info(
        f"📝 记录开仓 [{_resolve_account_id(account_id)}]：今日第{state.daily_trades_opened}单，"
        f"持仓{state.total_open_stake:.0f}U"
    )


def record_trade_closed(pnl: float, stake: float = config.DEFAULT_STAKE,
                        account_id: Optional[str] = None) -> None:
    """
    记录指定账户的平仓事件（全程加锁）。
    pnl < 0 表示亏损。
    """
    with LockedJsonFile(RISK_FILE, default={}) as (data, save):
        state = _state_from_data(data, account_id)

        # 更新持仓总额 — 若出现负值说明风控状态已漂移，告警并自动从交易文件反算修正
        new_stake = state.total_open_stake - stake
        if new_stake < -0.01:
            logger.warning(
                f"⚠️ total_open_stake 出现负值漂移 [{_resolve_account_id(account_id)}]: "
                f"{state.total_open_stake:.2f} - {stake:.2f} = {new_stake:.2f}，"
                f"从交易文件反算修正"
            )
            state.total_open_stake = _calc_actual_open_stake(account_id)
        else:
            state.total_open_stake = max(0.0, new_stake)

        if pnl < 0:
            # 记录亏损
            state.daily_loss += abs(pnl)
            state.consecutive_losses += 1
            logger.info(
                f"📉 记录亏损 [{_resolve_account_id(account_id)}]：{pnl:.2f}U | "
                f"今日累计亏损{state.daily_loss:.1f}U | 连亏{state.consecutive_losses}次"
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
            logger.info(f"📈 记录盈利 [{_resolve_account_id(account_id)}]：+{pnl:.2f}U | 连亏重置为0")

        data = _save_state_in_lock(data, state, account_id)
        save(data)


def refresh_open_stake(account_id: Optional[str] = None) -> None:
    """从交易文件重新计算指定账户的持仓总额"""
    actual = _calc_actual_open_stake(account_id)
    with LockedJsonFile(RISK_FILE, default={}) as (data, save):
        state = _state_from_data(data, account_id)
        if state.total_open_stake != actual:
            logger.info(f"🔄 持仓修正 [{_resolve_account_id(account_id)}]：{state.total_open_stake:.0f}U → {actual:.0f}U")
            state.total_open_stake = actual
            data = _save_state_in_lock(data, state, account_id)
            save(data)
        else:
            logger.info(f"🔄 持仓同步 [{_resolve_account_id(account_id)}]：{actual:.0f}U（无偏差）")


def is_in_cooldown(symbol: str, account_id: Optional[str] = None) -> tuple:
    """
    检查某币种在指定账户下是否在冷却期内。
    """
    from models import CloseType
    trades = load_json(TRADES_FILE, [])

    acc_id = _resolve_account_id(account_id)
    if acc_id != '_default':
        trades = filter_trades_by_account(trades, acc_id)

    now = utcnow()
    today_dt = now.date()
    cooldown_hours = config.COOLDOWN_HOURS

    for t in reversed(trades):
        if t.get('symbol') != symbol:
            continue
        if t.get('status') != 'closed':
            continue

        closed_at = t.get('closed_at', '')
        if not closed_at:
            continue

        ct = t.get('close_type')
        is_stop = False
        if ct is not None:
            is_stop = CloseType.is_stop_loss(ct)
        else:
            close_reason = t.get('close_reason', '').lower()
            is_stop = '止损' in close_reason or 'stop' in close_reason

        if is_stop:
            try:
                closed_dt = parse_iso(closed_at)
                hours_since = (now - closed_dt).total_seconds() / 3600
                if hours_since < cooldown_hours:
                    remaining = cooldown_hours - hours_since
                    reason = f"止损平仓后冷却中（{hours_since:.1f}h/{cooldown_hours}h，剩余{remaining:.1f}h）"
                    return (True, reason)
            except Exception:
                continue
        else:
            # M4: 用 parse_iso().date() 比较而不是 startswith
            # 防止时区漂移写入的 closed_at 字段（如本地时间串）绕过"同日已平仓"保护
            try:
                closed_date = parse_iso(closed_at).date()
                if closed_date == today_dt:
                    reason = "今日已平仓过（防止同日二次开仓亏损）"
                    return (True, reason)
            except Exception:
                # 解析失败 → 退回到保守的 startswith 作为 fallback
                if closed_at.startswith(now.strftime('%Y-%m-%d')):
                    reason = "今日已平仓过（防止同日二次开仓亏损）"
                    return (True, reason)

    return (False, "")


def get_risk_summary(account_id: Optional[str] = None) -> str:
    """获取指定账户的风控状态摘要"""
    state = load_risk_state(account_id)
    acc_id = _resolve_account_id(account_id)
    lines = [
        f"📋 <b>风控状态</b> [{acc_id}]",
        f"  今日亏损：{state.daily_loss:.1f} / {config.RISK_MAX_DAILY_LOSS}U",
        f"  今日开仓：{state.daily_trades_opened} / {config.RISK_MAX_DAILY_TRADES}次",
        f"  连亏次数：{state.consecutive_losses} / {config.RISK_CONSECUTIVE_LOSS_PAUSE}次",
        f"  持仓占用：{state.total_open_stake:.0f}U",
    ]
    if state.paused_until:
        lines.append(f"  ⚠️ 暂停至：{state.paused_until[:16]} UTC")
    return "\n".join(lines)
