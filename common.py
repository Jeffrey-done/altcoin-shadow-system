#!/usr/bin/env python3
"""
公共工具模块
提供：日志配置、TG推送、原子写JSON、环境变量加载、符号转换、时间工具
"""

import fcntl
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv

# ── 路径常量 ─────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, '.env')
CANDIDATES_FILE = os.path.join(SCRIPT_DIR, 'altcoin_candidates.json')
TRADES_FILE = os.path.join(SCRIPT_DIR, 'altcoin_shadow_trades.json')
RISK_FILE = os.path.join(SCRIPT_DIR, 'risk_state.json')
WEEKLY_REPORT_FILE = os.path.join(SCRIPT_DIR, 'weekly_report.json')
TRADES_ARCHIVE_FILE = os.path.join(SCRIPT_DIR, 'altcoin_trades_archive.json')
# In-flight journal：记录"已经调用交易所下单但还没确认写入 trades.json"的订单
# 任何进程崩溃后重启都会扫这个文件，防止产生交易所已成交但系统不知情的幽灵仓位
TRADES_INFLIGHT_FILE = os.path.join(SCRIPT_DIR, 'altcoin_trades_inflight.json')


# ── 环境变量 ─────────────────────────────────────────────────────
def load_env():
    """加载 .env 文件，缺失关键变量时发出警告"""
    load_dotenv(ENV_PATH, override=True)
    token = os.environ.get('TG_BOT_TOKEN', '')
    chat_id = os.environ.get('TG_CHAT_ID', '')
    if not token:
        logging.warning("TG_BOT_TOKEN 未设置，TG 推送将不可用")
    if not chat_id:
        logging.warning("TG_CHAT_ID 未设置，TG 推送将不可用")
    return token, chat_id


# 模块加载时自动读取
TG_BOT_TOKEN, TG_CHAT_ID = load_env()


# ── 日志配置 ─────────────────────────────────────────────────────
def setup_logger(name: str) -> logging.Logger:
    """统一日志格式，支持 LOG_LEVEL 环境变量"""
    level_str = os.environ.get('LOG_LEVEL', 'INFO').upper()
    level = getattr(logging, level_str, logging.INFO)
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    logger = logging.getLogger(name)
    logger.setLevel(level)
    return logger


# ── TG 推送 ──────────────────────────────────────────────────────
def send_tg(msg: str) -> bool:
    """发送 Telegram 消息，返回是否成功"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        logging.warning("TG 配置缺失，跳过推送")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code != 200:
            logging.warning(f"TG 推送返回非 200: {resp.status_code} {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logging.error(f"TG 推送失败: {e}")
        return False


# ── 原子写 JSON ──────────────────────────────────────────────────
def atomic_write_json(filepath: str, data: Any) -> None:
    """
    原子写入 JSON 文件：先写临时文件再 rename，防止崩溃时数据损坏。
    使用 fcntl.flock 排他锁保证并发安全。
    """
    dir_name = os.path.dirname(filepath)
    lockfile = filepath + '.lock'
    fd, tmp_path = tempfile.mkstemp(suffix='.tmp', dir=dir_name)
    lock_fd = None
    try:
        # 获取排他锁
        lock_fd = open(lockfile, 'a')
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, filepath)
    except Exception:
        # 清理临时文件
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


def load_json(filepath: str, default: Any = None) -> Any:
    """安全加载 JSON 文件，不存在或损坏返回 default。使用 fcntl.flock 共享锁。"""
    if not os.path.exists(filepath):
        return default if default is not None else []
    lockfile = filepath + '.lock'
    lock_fd = None
    try:
        lock_fd = open(lockfile, 'a')
        fcntl.flock(lock_fd, fcntl.LOCK_SH)

        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logging.warning(f"JSON 加载失败 ({filepath}): {e}，返回默认值")
        return default if default is not None else []
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


class LockedJsonFile:
    """
    上下文管理器：对 JSON 文件加排他锁，确保 read-modify-write 原子性。
    用于 realtime_monitor 等需要在持锁期间修改数据的场景。

    用法：
        with LockedJsonFile(filepath, default=[]) as (data, save):
            # data 是读取到的 JSON 数据
            # 修改 data ...
            save(data)  # 调用 save 写回文件（仍在锁保护下）
    """

    def __init__(self, filepath: str, default: Any = None):
        self.filepath = filepath
        self.default = default if default is not None else []
        self.lockfile = filepath + '.lock'
        self.lock_fd = None

    def __enter__(self):
        self.lock_fd = open(self.lockfile, 'a')
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX)

        # 持锁读取
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                data = self.default
        else:
            data = self.default

        def save(new_data):
            """在锁保护下原子写入"""
            dir_name = os.path.dirname(self.filepath) or '.'
            fd, tmp_path = tempfile.mkstemp(suffix='.tmp', dir=dir_name)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(new_data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, self.filepath)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

        return data, save

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            self.lock_fd.close()
        return False


# ── 符号转换 ─────────────────────────────────────────────────────
def to_binance_symbol(symbol: str) -> str:
    """
    ccxt 格式 (BTC/USDT) → Binance API 格式 (BTCUSDT)
    """
    return symbol.replace('/USDT', 'USDT').replace('/', '')


def to_okx_symbol(symbol: str) -> str:
    """
    ccxt 格式 (BTC/USDT) → OKX 永续合约 instId 格式 (BTC-USDT-SWAP)
    """
    base = symbol.replace('/USDT', '').replace('/', '')
    return f"{base}-USDT-SWAP"


# ── 时间工具 ─────────────────────────────────────────────────────
def utcnow() -> datetime:
    """返回带时区信息的 UTC 当前时间"""
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """返回 ISO 格式的 UTC 时间字符串（带时区）"""
    return utcnow().isoformat()


def parse_iso(dt_str: str) -> datetime:
    """
    解析 ISO 时间字符串，兼容 naive（当作 UTC）和 aware 两种格式
    """
    dt_str = dt_str.strip()
    try:
        dt = datetime.fromisoformat(dt_str)
    except ValueError:
        # 兼容旧格式：截取前 19 位
        dt = datetime.fromisoformat(dt_str[:19])

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def hold_days(opened_at: str) -> int:
    """计算持仓天数"""
    opened = parse_iso(opened_at)
    return (utcnow() - opened).days


def hold_hours(opened_at: str) -> float:
    """计算持仓小时数"""
    opened = parse_iso(opened_at)
    delta = utcnow() - opened
    return delta.total_seconds() / 3600


def today_str() -> str:
    """返回今日日期字符串 YYYY-MM-DD（UTC）"""
    return utcnow().strftime('%Y-%m-%d')


def get_current_account_id() -> str:
    """
    获取当前活跃账户 ID。
    用于给新交易打标和按账户过滤数据。
    如果 admin_secrets 未初始化（单账户模式），返回空字符串。
    """
    try:
        from admin_secrets import get_active_account_id
        return get_active_account_id() or ''
    except Exception:
        return ''


def get_all_trading_account_ids() -> list:
    """
    返回所有配置了凭证的交易账户 ID 列表（用于多账户同步开仓）。
    不含系统影子账户。如果 admin_secrets 不可用，返回空列表。
    """
    try:
        from admin_secrets import get_all_trading_accounts
        return [acc['id'] for acc in get_all_trading_accounts()]
    except Exception:
        return []


def filter_trades_by_account(trades: list, account_id: str = None) -> list:
    """
    按账户 ID 过滤交易列表。
    - 如果 account_id 为空或 None，返回所有交易（单账户兼容模式）
    - 否则只返回匹配该 account_id 的交易
    - 无 account_id 的历史交易归属影子账户（acc_shadow_system）
    """
    if not account_id:
        return trades

    SHADOW_ID = 'acc_shadow_system'
    filtered = []
    for t in trades:
        t_account = t.get('account_id', '')
        if t_account == account_id:
            # 精确匹配
            filtered.append(t)
        elif t_account == '' and account_id == SHADOW_ID:
            # 无标记的旧交易归属影子账户
            filtered.append(t)
    return filtered



def _account_balance_for(account_id: str = None) -> float:
    """
    返回指定账户的本金 (ACCOUNT_BALANCE)。

    B7 修复：之前所有账户都返回全局 config.ACCOUNT_BALANCE，但 admin panel
    设计上每个账户可以独立设 ACCOUNT_BALANCE（runtime_config.json 的
    {account_id: {"ACCOUNT_BALANCE": N}}）。
    runtime_config.apply_overrides() 只把"当前活跃账户"的覆盖写到全局 config
    模块，所以前端切到非活跃账户视图（/api/data?account_id=acc_B）时，原来
    会用错的本金计算余额。

    现在改成读 runtime_config.load_account_overrides(account_id) 拿那个账户
    自己的 ACCOUNT_BALANCE；没设的话再 fallback 到全局值。
    """
    import config
    fallback = float(getattr(config, 'ACCOUNT_BALANCE', 0))
    if not account_id:
        return fallback
    try:
        import runtime_config
        overrides = runtime_config.load_account_overrides(account_id) or {}
        val = overrides.get('ACCOUNT_BALANCE')
        if val is not None:
            return float(val)
    except Exception:
        # runtime_config.json 损坏 / 不存在 / admin 模块未加载都走 fallback
        pass
    return fallback


def get_compound_stake(account_id: str = None) -> float:
    """
    自动复利：根据累计已实现盈亏动态调整单笔保证金。

    M6 更新：改为平滑线性衰减（避免离散跳变造成风控账面错位）。
      - 亏损时（total_pnl <= 0）仍然固定 DEFAULT_STAKE，不加仓
      - 盈利时：stake = DEFAULT_STAKE + (total_pnl / COMPOUND_STEP) * COMPOUND_INCREASE
        而不是 (total_pnl // COMPOUND_STEP) * COMPOUND_INCREASE
      - 上限仍为 COMPOUND_MAX_STAKE

    参数:
      account_id: 指定账户 ID；None 使用当前活跃账户
    """
    import config
    if not config.AUTO_COMPOUND_ENABLED:
        return config.DEFAULT_STAKE

    trades = load_json(TRADES_FILE, [])
    if account_id is None:
        account_id = get_current_account_id()
    trades = filter_trades_by_account(trades, account_id)

    total_pnl = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        for t in trades if t.get('status') == 'closed'
    )

    if total_pnl <= 0:
        return config.DEFAULT_STAKE

    # 平滑复利：用比例代替整数步数，stake 随 total_pnl 连续增长
    step = max(config.COMPOUND_STEP, 1)
    ratio = total_pnl / step
    stake = config.DEFAULT_STAKE + ratio * config.COMPOUND_INCREASE
    stake = min(stake, config.COMPOUND_MAX_STAKE)

    # 四舍五入到整数 U（交易所最小精度，也避免浮点尾数扰动风控比对）
    return round(stake)


def get_dynamic_balance(account_id: str = None) -> float:
    """
    计算动态账户余额 = 初始本金 + 已实现盈亏 + TP1已锁定利润。

    参数:
      account_id: 指定账户 ID；None 使用当前活跃账户

    TP1锁定利润说明：
      当 TP1 触发时，50%仓位已平仓并锁定利润（tp1_locked_pnl），
      但交易 status 仍为 'open'（剩余50%等TP2）。
      这部分利润已经是"已实现"的，应计入余额。

    ⚠️ 注意：不要用此函数做"持仓占比"风控基准，应使用 get_realized_balance()。
    浮动 TP1 利润算进余额会让风控上限随浮动盈利扩大，形成"开仓→TP1→再开仓"
    的正反馈放大敞口（见 M2 修复）。
    """
    # 本金通过 _account_balance_for(account_id) 取，不再依赖全局 config.ACCOUNT_BALANCE
    trades = load_json(TRADES_FILE, [])
    if account_id is None:
        account_id = get_current_account_id()
    trades = filter_trades_by_account(trades, account_id)

    total_pnl = 0.0
    for t in trades:
        if t.get('status') == 'closed':
            total_pnl += t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        elif t.get('status') == 'open' and t.get('tp1_locked_pnl', 0) > 0:
            # TP1已触发但交易未完全平仓：锁定利润计入余额
            total_pnl += t.get('tp1_locked_pnl', 0)

    return _account_balance_for(account_id) + total_pnl


def get_realized_balance(account_id: str = None) -> float:
    """
    M2: 严格已实现余额 = 初始本金 + 已平仓交易的 tp1_locked_pnl + pnl。

    与 get_dynamic_balance 的区别：
      - 不把 open 状态交易的 tp1_locked_pnl 算进来
      - 用于 RISK_MAX_POSITION_PCT 的持仓占比风控基准
      - 防止 TP1 触发的"锁定浮动利润"让最大仓位上限立即扩大，
        形成 TP1 → 余额 +X → 持仓上限 +X/2 → 多开一笔 → 敞口翻倍的正反馈
    """
    # 本金通过 _account_balance_for(account_id) 取
    trades = load_json(TRADES_FILE, [])
    if account_id is None:
        account_id = get_current_account_id()
    trades = filter_trades_by_account(trades, account_id)

    realized_pnl = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        for t in trades if t.get('status') == 'closed'
    )
    return _account_balance_for(account_id) + realized_pnl



def cleanup_old_trades():
    """
    归档超过 TRADES_ARCHIVE_DAYS 的已平仓交易。
    将旧记录移到 archive 文件，主交易文件只保留近期数据。
    使用 LockedJsonFile 确保与其他写入者的原子性。
    """
    import config
    now = utcnow()
    archived_count = 0

    with LockedJsonFile(TRADES_FILE, default=[]) as (trades, save_trades):
        if not trades:
            return 0

        keep = []
        archive_new = []

        for t in trades:
            if t.get('status') != 'closed':
                keep.append(t)
                continue

            # 优先用 closed_at 判断归档；若没有就 fallback 到 opened_at
            # （兼容历史数据漂移，避免无限累积）
            ts_str = t.get('closed_at') or t.get('opened_at', '')
            if not ts_str:
                keep.append(t)
                continue

            try:
                closed_dt = parse_iso(ts_str)
                age_days = (now - closed_dt).days
                if age_days > config.TRADES_ARCHIVE_DAYS:
                    archive_new.append(t)
                else:
                    keep.append(t)
            except Exception:
                keep.append(t)

        if not archive_new:
            return 0

        # 追加到归档文件（归档文件也加锁）
        with LockedJsonFile(TRADES_ARCHIVE_FILE, default=[]) as (existing_archive, save_archive):
            existing_archive.extend(archive_new)
            save_archive(existing_archive)

        # 更新主交易文件
        save_trades(keep)
        archived_count = len(archive_new)

    logging.info(f"归档了 {archived_count} 笔过期交易（>{config.TRADES_ARCHIVE_DAYS}天）")
    return archived_count



# ══════════════════════════════════════════════════════════════════
#  In-flight Journal（幽灵仓位防护）
# ══════════════════════════════════════════════════════════════════
#
# 背景：实盘下单是一次网络调用；成功后需要把 trade 写入 trades.json。
# 如果下单成功但写盘前进程崩溃（OOM / kill / 磁盘只读 / 网络重试路径），
# 系统重启后会以为"该币未开仓"而重复下单，造成交易所双倍仓位。
#
# 解决方案：
#   1. 下单前：往 journal 文件写 pending 条目（symbol, account, coid, ts）
#   2. 下单成功且 trades.json 落盘后：从 journal 移除 pending
#   3. 进程启动时：扫 journal 里的 pending，反查交易所是否有对应 clOrdId 的成交
#      - 有成交但 trades.json 没有 → 告警 + 补录
#      - 没成交 → 清理 pending
#
# Journal 结构:
# [
#   {
#     "client_order_id": "sho-PEPE-1715...",
#     "exchange": "binance",
#     "account_id": "acc_xxx",
#     "symbol": "PEPE/USDT",
#     "direction": "SHORT",
#     "stake": 50,
#     "leverage": 10,
#     "status": "pending" | "confirmed" | "failed",
#     "created_at": "ISO-8601",
#     "updated_at": "ISO-8601",
#     "order_id": "",          # 交易所订单 ID（成功后回填）
#     "last_error": ""         # 最近一次失败信息
#   },
# ]

def journal_add_pending(client_order_id: str, exchange: str, account_id: str,
                         symbol: str, direction: str, stake: float, leverage: int) -> None:
    """下单前调用：往 journal 写 pending 条目（持锁）"""
    with LockedJsonFile(TRADES_INFLIGHT_FILE, default=[]) as (journal, save):
        # 幂等：同 client_order_id 已经在 journal 里就跳过
        for entry in journal:
            if entry.get('client_order_id') == client_order_id:
                entry['status'] = 'pending'
                entry['updated_at'] = utcnow_iso()
                save(journal)
                return
        journal.append({
            'client_order_id': client_order_id,
            'exchange': exchange,
            'account_id': account_id or '',
            'symbol': symbol,
            'direction': direction,
            'stake': stake,
            'leverage': leverage,
            'status': 'pending',
            'created_at': utcnow_iso(),
            'updated_at': utcnow_iso(),
            'order_id': '',
            'last_error': '',
        })
        save(journal)


def journal_mark_confirmed(client_order_id: str, order_id: str = '') -> None:
    """trades.json 写盘成功后调用：从 journal 移除 pending"""
    with LockedJsonFile(TRADES_INFLIGHT_FILE, default=[]) as (journal, save):
        new_journal = [
            e for e in journal if e.get('client_order_id') != client_order_id
        ]
        if len(new_journal) != len(journal):
            save(new_journal)


def journal_mark_failed(client_order_id: str, error: str) -> None:
    """下单失败时调用：标记为 failed（保留一段时间便于审计），不阻塞后续重试"""
    with LockedJsonFile(TRADES_INFLIGHT_FILE, default=[]) as (journal, save):
        for entry in journal:
            if entry.get('client_order_id') == client_order_id:
                entry['status'] = 'failed'
                entry['last_error'] = str(error)[:500]
                entry['updated_at'] = utcnow_iso()
                save(journal)
                return


def journal_list_pending() -> list:
    """读取 journal 中所有 pending 条目（不加锁，只读）"""
    data = load_json(TRADES_INFLIGHT_FILE, [])
    return [e for e in data if e.get('status') == 'pending']


def journal_cleanup_failed(retain_hours: int = 72) -> int:
    """
    清理 journal 中超过 retain_hours 小时的 failed 条目，避免无限增长。
    pending 条目永远保留直到被 confirmed / 人工处理。
    返回清理数量。
    """
    from datetime import timedelta
    cutoff = utcnow() - timedelta(hours=retain_hours)
    removed = 0
    with LockedJsonFile(TRADES_INFLIGHT_FILE, default=[]) as (journal, save):
        new_journal = []
        for e in journal:
            if e.get('status') == 'failed':
                try:
                    if parse_iso(e.get('updated_at', '')) < cutoff:
                        removed += 1
                        continue
                except Exception:
                    pass
            new_journal.append(e)
        if removed:
            save(new_journal)
    return removed
