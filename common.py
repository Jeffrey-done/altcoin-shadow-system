#!/usr/bin/env python3
"""
公共工具模块
提供：日志配置、TG推送、原子写JSON、环境变量加载、符号转换、时间工具
"""

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
    """
    dir_name = os.path.dirname(filepath)
    fd, tmp_path = tempfile.mkstemp(suffix='.tmp', dir=dir_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, filepath)
    except Exception:
        # 清理临时文件
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def load_json(filepath: str, default: Any = None) -> Any:
    """安全加载 JSON 文件，不存在或损坏返回 default"""
    if not os.path.exists(filepath):
        return default if default is not None else []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logging.warning(f"JSON 加载失败 ({filepath}): {e}，返回默认值")
        return default if default is not None else []


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
