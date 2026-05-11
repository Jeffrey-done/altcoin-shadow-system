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
FUNDING_TRADES_FILE = os.path.join(SCRIPT_DIR, 'funding_arb_trades.json')
WEEKLY_REPORT_FILE = os.path.join(SCRIPT_DIR, 'weekly_report.json')
LOW_RISK_TRADES_FILE = os.path.join(SCRIPT_DIR, 'low_risk_trades.json')


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


# ── 符号转换 ─────────────────────────────────────────────────────
def to_binance_symbol(symbol: str) -> str:
    """
    ccxt 格式 (BTC/USDT) → Binance API 格式 (BTCUSDT)
    """
    return symbol.replace('/USDT', 'USDT').replace('/', '')


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



def get_compound_stake() -> float:
    """
    自动复利：根据累计已实现盈亏动态调整单笔保证金。
    公式：stake = DEFAULT_STAKE + (total_pnl // COMPOUND_STEP) * COMPOUND_INCREASE
    上限：COMPOUND_MAX_STAKE
    """
    import config
    if not config.AUTO_COMPOUND_ENABLED:
        return config.DEFAULT_STAKE

    trades = load_json(TRADES_FILE, [])
    total_pnl = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        for t in trades if t.get('status') == 'closed'
    )

    if total_pnl <= 0:
        return config.DEFAULT_STAKE

    steps = int(total_pnl // config.COMPOUND_STEP)
    stake = config.DEFAULT_STAKE + steps * config.COMPOUND_INCREASE
    stake = min(stake, config.COMPOUND_MAX_STAKE)

    return stake


def get_dynamic_balance() -> float:
    """
    计算动态账户余额 = 初始本金 + 所有策略已实现盈亏 + TP1已锁定利润。
    
    TP1锁定利润说明：
      当 TP1 触发时，50%仓位已平仓并锁定利润（tp1_locked_pnl），
      但交易 status 仍为 'open'（剩余50%等TP2）。
      这部分利润已经是"已实现"的，应计入余额。
    """
    import config
    trades = load_json(TRADES_FILE, [])
    funding_trades = load_json(FUNDING_TRADES_FILE, [])
    low_risk_trades = load_json(LOW_RISK_TRADES_FILE, [])

    total_pnl = 0.0
    # 做空/做多交易
    for t in trades:
        if t.get('status') == 'closed':
            total_pnl += t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        elif t.get('status') == 'open' and t.get('tp1_locked_pnl', 0) > 0:
            # TP1已触发但交易未完全平仓：锁定利润计入余额
            total_pnl += t.get('tp1_locked_pnl', 0)
    # 资金费率套利交易
    for t in funding_trades:
        if t.get('status') == 'closed':
            total_pnl += t.get('total_pnl', 0)
    # 低风险策略交易
    for t in low_risk_trades:
        if t.get('status') == 'closed':
            total_pnl += t.get('pnl', 0)

    return config.ACCOUNT_BALANCE + total_pnl
