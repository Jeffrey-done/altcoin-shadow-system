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
    account_param,
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


def _calc_consecutive_losses(account_id: Optional[str] = None) -> int:
    """从最近已平仓交易反推当前连续亏损次数（按账户过滤后）。"""
    trades = load_json(TRADES_FILE, [])
    acc_id = _resolve_account_id(account_id)
    if acc_id != '_default':
        trades = filter_trades_by_account(trades, acc_id)

    closed = [t for t in trades if t.get('status') == 'closed']
    closed.sort(key=lambda x: x.get('closed_at', ''))

    cnt = 0
    for t in reversed(closed):
        realized = float(t.get('tp1_locked_pnl', 0) or 0) + float(t.get('pnl', 0) or 0)
        if realized < 0:
            cnt += 1
        elif realized > 0:
            break
    return cnt


def reconcile_risk_state(account_id: Optional[str] = None, notify: bool = False) -> dict:
    """
    对账：从 trades 文件反算指定账户真实的 daily_loss / daily_trades_opened /
    total_open_stake，若与 risk_state 不一致则修正。
    """
    expected_loss = _calc_today_realized_loss(account_id)
    expected_trades = _calc_today_trades_opened(account_id)
    expected_stake = _calc_actual_open_stake(account_id)
    expected_consecutive = _calc_consecutive_losses(account_id)

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

        if state.consecutive_losses != expected_consecutive:
            diff['consecutive_losses'] = (state.consecutive_losses, expected_consecutive)
            state.consecutive_losses = expected_consecutive
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

def can_open_trade(stake: float = None, strategy: str = 'short',
                   account_id: Optional[str] = None) -> tuple:
    """
    检查指定账户是否允许开仓。

    参数:
      stake: 本次开仓保证金；None → 取该账号的 DEFAULT_STAKE（含 proportional 缩放）
      strategy: 策略类型（保留参数向后兼容）
      account_id: 账户 ID（None 使用活跃账户）

    返回: (allowed: bool, reason: str)
    """
    # 阶段 2（2026-05）：default=None 而不是 config.DEFAULT_STAKE，避免 import
    # 时把 PRISTINE 30 锁进 default。改为函数体内按账号取（含 proportional 缩放）。
    if stake is None:
        stake = float(account_param(account_id, 'DEFAULT_STAKE',
                                    config.DEFAULT_STAKE))
    # 所有读-判-写操作统一在锁内执行，避免并发 can_open_trade 重复清理 paused_until
    # 或 total_open_stake 双写漂移
    # 多账户合规（2026-05）：所有账户级阈值（RISK_MAX_DAILY_LOSS / DAILY_TRADES /
    # POSITION_PCT）都通过 account_param() 取该账号的覆盖值，避免 apply_overrides
    # 把活跃账户的阈值写到 config 模块后，非活跃账户的检查用错阈值。
    _max_daily_loss = float(account_param(account_id, 'RISK_MAX_DAILY_LOSS',
                                          config.RISK_MAX_DAILY_LOSS))
    _max_daily_trades = int(account_param(account_id, 'RISK_MAX_DAILY_TRADES',
                                          config.RISK_MAX_DAILY_TRADES))
    _max_position_pct = float(account_param(account_id, 'RISK_MAX_POSITION_PCT',
                                            config.RISK_MAX_POSITION_PCT))

    # 先在 RISK 锁外读取 TRADES 快照，避免 RISK->TRADES 交叉锁顺序
    actual_stake_snapshot = _calc_actual_open_stake(account_id)

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
        if state.daily_loss >= _max_daily_loss:
            reason = f"单日亏损已达上限（{state.daily_loss:.1f}U >= {_max_daily_loss:.0f}U）"
            if dirty:
                data = _save_state_in_lock(data, state, account_id)
                save(data)
            logger.warning(f"🚫 {reason}")
            return False, reason

        # 3. 检查单日最大开仓次数
        if state.daily_trades_opened >= _max_daily_trades:
            reason = f"单日开仓次数已达上限（{state.daily_trades_opened} >= {_max_daily_trades}）"
            if dirty:
                data = _save_state_in_lock(data, state, account_id)
                save(data)
            logger.warning(f"🚫 {reason}")
            return False, reason

        # 4. 检查最大持仓占比（用锁外快照同步持仓总额，避免交叉锁）
        if abs(state.total_open_stake - actual_stake_snapshot) > 0.01:
            logger.info(f"🔄 持仓自动修正：{state.total_open_stake:.0f}U → {actual_stake_snapshot:.0f}U")
            state.total_open_stake = actual_stake_snapshot
            dirty = True

        # M2: 持仓占比风控基准改为"已实现余额"，不含浮动 TP1 锁定利润，
        # 防止 TP1 后上限放大形成加仓正反馈。（旧版本用 get_dynamic_balance，
        # 现已不再作为基准；移除了无用调用避免多一次余额查询。）
        realized_bal = get_realized_balance(account_id=_resolve_account_id(account_id))
        max_position = realized_bal * _max_position_pct
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


def record_trade_opened(stake: float = None, strategy: str = 'short',
                        account_id: Optional[str] = None) -> None:
    """记录指定账户的开仓事件（全程加锁）"""
    if stake is None:
        stake = float(account_param(account_id, 'DEFAULT_STAKE',
                                    config.DEFAULT_STAKE))
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


def try_open_trade(stake: float = None, strategy: str = 'short',
                   account_id: Optional[str] = None) -> tuple:
    """
    原子检查并记录开仓。合并 can_open_trade + record_trade_opened 为一个锁事务。
    返回 (allowed, reason)。如果 allowed=True，说明风控检查通过且状态已更新。
    """
    if stake is None:
        stake = float(account_param(account_id, 'DEFAULT_STAKE',
                                    config.DEFAULT_STAKE))
    _max_daily_loss = float(account_param(account_id, 'RISK_MAX_DAILY_LOSS',
                                          config.RISK_MAX_DAILY_LOSS))
    _max_daily_trades = int(account_param(account_id, 'RISK_MAX_DAILY_TRADES',
                                          config.RISK_MAX_DAILY_TRADES))
    _max_position_pct = float(account_param(account_id, 'RISK_MAX_POSITION_PCT',
                                            config.RISK_MAX_POSITION_PCT))
    actual_stake_snapshot = _calc_actual_open_stake(account_id)
    with LockedJsonFile(RISK_FILE, default={}) as (data, save):
        state = _state_from_data(data, account_id)
        dirty = False
        if state.paused_until:
            pause_end = parse_iso(state.paused_until)
            if utcnow() < pause_end:
                remaining = (pause_end - utcnow()).total_seconds()
                reason = f"账户暂停中（剩余{remaining:.0f}s）"
                return False, reason
            else:
                state.paused_until = None
                state.consecutive_losses = 0
                dirty = True
        if state.daily_loss >= _max_daily_loss:
            reason = f"单日亏损已达上限（{state.daily_loss:.2f} >= {_max_daily_loss:.2f}）"
            if dirty:
                data = _save_state_in_lock(data, state, account_id)
                save(data)
            return False, reason
        if state.daily_trades_opened >= _max_daily_trades:
            reason = f"单日开仓次数已达上限（{state.daily_trades_opened} >= {_max_daily_trades}）"
            if dirty:
                data = _save_state_in_lock(data, state, account_id)
                save(data)
            return False, reason
        if abs(state.total_open_stake - actual_stake_snapshot) > 0.01:
            state.total_open_stake = actual_stake_snapshot
            dirty = True
        realized_bal = get_realized_balance(account_id=_resolve_account_id(account_id))
        max_position = realized_bal * _max_position_pct
        if state.total_open_stake + stake > max_position:
            reason = (
                f"持仓占比超限（当前{state.total_open_stake:.0f}U + 新增{stake:.0f}U "
                f"> 上限{max_position:.0f}U，基于已实现余额{realized_bal:.0f}U）"
            )
            if dirty:
                data = _save_state_in_lock(data, state, account_id)
                save(data)
            return False, reason
        state.daily_trades_opened += 1
        state.total_open_stake += stake
        data = _save_state_in_lock(data, state, account_id)
        save(data)
    logger.info(
        f"\U0001f4dd 记录开仓 [{_resolve_account_id(account_id)}]：今日第{state.daily_trades_opened}单，"
        f"持仓{state.total_open_stake:.0f}U"
    )
    return True, "OK"


def record_trade_closed(pnl: float, stake: float = None,
                        account_id: Optional[str] = None,
                        trade_account_id: Optional[str] = None) -> None:
    """
    记录指定账户的平仓事件（全程加锁）。
    pnl < 0 表示亏损。

    NF-4: 与 ``release_partial_stake`` 同样的语义 —— 当 ``account_id`` 未指定
    但调用方知道这笔交易自带的 account_id（含空字符串），优先使用
    ``trade_account_id``，避免回退到当前活跃账户造成跨账户错账。
    """
    # NF-4: 优先用 trade 自带的 account_id
    if account_id is None and trade_account_id is not None:
        account_id = trade_account_id
    # 阶段 2（2026-05）：stake 默认值改为 None，避免 import 时锁定 PRISTINE。
    # 必须在 account_id 解析之后再取（否则可能用错账号的 stake）。
    if stake is None:
        stake = float(account_param(account_id, 'DEFAULT_STAKE',
                                    config.DEFAULT_STAKE))
    # 多账户合规（2026-05）：连亏暂停 / 暂停时长 / 单日亏损告警阈值都按
    # 账户级覆盖取，避免活跃账户的阈值被 apply_overrides 写到 config 后污染
    # 非活跃账户的判定。
    _consec_pause = int(account_param(account_id, 'RISK_CONSECUTIVE_LOSS_PAUSE',
                                      config.RISK_CONSECUTIVE_LOSS_PAUSE))
    _pause_hours = int(account_param(account_id, 'RISK_PAUSE_HOURS',
                                     config.RISK_PAUSE_HOURS))
    _max_daily_loss = float(account_param(account_id, 'RISK_MAX_DAILY_LOSS',
                                          config.RISK_MAX_DAILY_LOSS))
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
            if state.consecutive_losses >= _consec_pause:
                from datetime import timedelta
                pause_end = utcnow() + timedelta(hours=_pause_hours)
                state.paused_until = pause_end.isoformat()
                logger.warning(
                    f"🚨 连亏{state.consecutive_losses}次，暂停开仓{_pause_hours}小时"
                )
                send_tg(
                    f"🚨 <b>风控警告：连亏暂停</b>\n\n"
                    f"连续亏损 {state.consecutive_losses} 次\n"
                    f"今日累计亏损：{state.daily_loss:.1f}U\n"
                    f"暂停开仓至：{state.paused_until[:16]} UTC\n\n"
                    f"冷静等待，不要追单 ⏸️"
                )

            # 单日亏损告警
            if state.daily_loss >= _max_daily_loss:
                send_tg(
                    f"🛑 <b>风控警告：今日停止交易</b>\n\n"
                    f"今日累计亏损：{state.daily_loss:.1f}U\n"
                    f"已达上限 {_max_daily_loss:.0f}U\n"
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


def release_partial_stake(stake: float, account_id: Optional[str] = None,
                          trade_account_id: Optional[str] = None) -> None:
    """
    M-1 修复：TP1 半仓平仓后释放保证金到 total_open_stake，
    但不影响 daily_loss / consecutive_losses（这些只在最终平仓时记账）。

    背景：record_trade_closed 会重置/累加连亏计数，把"TP1 锁定的浮动利润"
    当成实现盈利记账会让 TP1 后再硬止损的整笔亏损被错误地清空连亏计数。
    专用函数只动 total_open_stake，避免误触发风控状态。

    NF-4 修复：当 ``account_id`` 未指定但调用方知道这笔交易自带的 account_id
    （包括空字符串 ``''`` 表示 v4.3 之前的老数据 → 全局默认账户），优先使用
    ``trade_account_id``。否则在多账户环境里调 ``release_partial_stake(stake)``
    会回退到 *当前活跃账户*，把 TP1 释放的 stake 错记到错误账户上。
    """
    # NF-4: 优先用 trade 自带的 account_id（可能是空字符串，代表全局默认）
    if account_id is None and trade_account_id is not None:
        account_id = trade_account_id
    if stake <= 0:
        return
    with LockedJsonFile(RISK_FILE, default={}) as (data, save):
        state = _state_from_data(data, account_id)
        new_stake = state.total_open_stake - stake
        if new_stake < -0.01:
            logger.warning(
                f"⚠️ release_partial_stake: total_open_stake 漂移 "
                f"[{_resolve_account_id(account_id)}]: "
                f"{state.total_open_stake:.2f} - {stake:.2f} = {new_stake:.2f}，从交易文件反算修正"
            )
            state.total_open_stake = _calc_actual_open_stake(account_id)
        else:
            state.total_open_stake = max(0.0, new_stake)
        data = _save_state_in_lock(data, state, account_id)
        save(data)
    logger.info(
        f"📝 TP1 半仓释放保证金 [{_resolve_account_id(account_id)}]：-{stake:.0f}U"
    )


def is_in_cooldown(symbol: str, account_id: Optional[str] = None) -> tuple:
    """
    检查某币种在指定账户下是否在冷却期内。

    account_id 语义（2026-05 修复后统一）:
      - None 或 ''        → 跨所有账户扫描（保守默认；任一账户的止损都会触发冷却）
      - 非空字符串 'acc_X' → 仅看 acc_X 名下的交易（账户级精准查询）

    背景:
      旧实现 '' 走 _resolve_account_id 后会变成 '_default'，跳过 filter
      也是"全扫"的效果，与 None 行为相同但语义混乱（注释写"仅扫描 account_id 为空
      的老数据"，实际是全扫）。新语义：把 None / '' / 任何 falsy 都归为"默认全扫"，
      明确字符串才走账户过滤。

    使用建议:
      - scanner 默认调用 is_in_cooldown(symbol) 不传 account_id → 全局保守
      - 需要按账户隔离时显式传 account_id='acc_X'（仅 COOLDOWN_SCOPE='per_account'
        时 scanner 会这样传）
    """
    from models import CloseType
    trades = load_json(TRADES_FILE, [])

    # 统一语义：仅当 account_id 是非空字符串时才按账户过滤
    if account_id:
        trades = filter_trades_by_account(trades, account_id)

    now = utcnow()
    today_dt = now.date()
    # 多账户合规（2026-05）：COOLDOWN_HOURS 是账户级字段，按账户取覆盖值。
    # account_id 为 None/'' 时退化为全局 config 值（单账户兼容）。
    cooldown_hours = int(account_param(account_id, 'COOLDOWN_HOURS', config.COOLDOWN_HOURS))

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
            #
            # Fix: 只有亏损平仓才触发同日冷却。盈利平仓（TP1/TP2 止盈）不应
            # 阻止当天再次捕捉同一币种的新信号（例如早盘 TP2 后下午出现第二波）。
            # 原先所有平仓都冷却的设计过于保守，会浪费日内多次入场机会。
            realized_pnl = t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
            if realized_pnl >= 0:
                # 盈利或持平平仓 → 不冷却，允许同日再开
                continue

            try:
                # NF2-4: 必须先 astimezone(UTC) 再 .date()
                # parse_iso 不做时区转换，只给 naive 时间戳贴 UTC 标签；
                # 老数据若写入了带本地时区的 ISO 串（如 +08:00），
                # 直接 .date() 拿到的是本地日期，与 today_dt(UTC) 比较会漏冷却。
                from datetime import timezone as _tz
                closed_date = parse_iso(closed_at).astimezone(_tz.utc).date()
                if closed_date == today_dt:
                    reason = "今日已亏损平仓过（防止同日二次开仓扩大亏损）"
                    return (True, reason)
            except Exception:
                # 解析失败 → 退回到保守的 startswith 作为 fallback
                if closed_at.startswith(now.strftime('%Y-%m-%d')):
                    reason = "今日已亏损平仓过（防止同日二次开仓扩大亏损）"
                    return (True, reason)

    return (False, "")


def get_risk_summary(account_id: Optional[str] = None) -> str:
    """获取指定账户的风控状态摘要"""
    state = load_risk_state(account_id)
    acc_id = _resolve_account_id(account_id)
    # 多账户合规：摘要里展示的阈值也按该账户取
    _max_daily_loss = float(account_param(account_id, 'RISK_MAX_DAILY_LOSS',
                                          config.RISK_MAX_DAILY_LOSS))
    _max_daily_trades = int(account_param(account_id, 'RISK_MAX_DAILY_TRADES',
                                          config.RISK_MAX_DAILY_TRADES))
    _consec_pause = int(account_param(account_id, 'RISK_CONSECUTIVE_LOSS_PAUSE',
                                      config.RISK_CONSECUTIVE_LOSS_PAUSE))
    lines = [
        f"📋 <b>风控状态</b> [{acc_id}]",
        f"  今日亏损：{state.daily_loss:.1f} / {_max_daily_loss:.0f}U",
        f"  今日开仓：{state.daily_trades_opened} / {_max_daily_trades}次",
        f"  连亏次数：{state.consecutive_losses} / {_consec_pause}次",
        f"  持仓占用：{state.total_open_stake:.0f}U",
    ]
    if state.paused_until:
        lines.append(f"  ⚠️ 暂停至：{state.paused_until[:16]} UTC")
    return "\n".join(lines)
