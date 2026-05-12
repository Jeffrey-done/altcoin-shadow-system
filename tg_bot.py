#!/usr/bin/env python3
"""
Telegram Bot 交互指令模块
通过 getUpdates 轮询接收指令，返回系统状态信息。

支持指令：
  /status     - 当前持仓概览 + 风控状态
  /balance    - 账户余额、今日/累计盈亏
  /positions  - 所有持仓详情（入场价、浮盈、止损位）
  /candidates - 候选池（等待触发的币）
  /risk       - 风控状态详情
  /help       - 显示所有可用指令
"""

import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests as _requests

import config
from common import (
    TRADES_FILE, CANDIDATES_FILE, RISK_FILE,
    setup_logger, load_json, today_str, utcnow_iso,
    get_dynamic_balance, get_compound_stake,
    TG_BOT_TOKEN, TG_CHAT_ID,
)

logger = setup_logger("tg_bot")

_last_update_id = 0


# ══════════════════════════════════════════════════════════════════
#  指令处理
# ══════════════════════════════════════════════════════════════════

def cmd_help() -> str:
    """显示帮助"""
    return (
        "🤖 <b>影子做空系统 - 可用指令</b>\n\n"
        "/status - 当前持仓概览 + 风控状态\n"
        "/balance - 账户余额、今日/累计盈亏\n"
        "/positions - 所有持仓详情\n"
        "/candidates - 候选池（等待触发）\n"
        "/risk - 风控状态详情\n"
        "/help - 显示本帮助\n"
    )


def cmd_balance() -> str:
    """账户余额"""
    trades = load_json(TRADES_FILE, [])
    closed = [t for t in trades if t.get('status') == 'closed']

    today = today_str()
    today_closed = [t for t in closed if t.get('closed_at', '').startswith(today)]
    today_pnl = sum(t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in today_closed)
    total_pnl = sum(t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in closed)

    # TP1 已锁定但未平仓
    open_trades = [t for t in trades if t.get('status') == 'open']
    tp1_locked = sum(t.get('tp1_locked_pnl', 0) for t in open_trades if t.get('tp1_triggered'))
    total_pnl += tp1_locked

    balance = get_dynamic_balance()
    compound_stake = get_compound_stake()

    wins = sum(1 for t in closed if (t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)) > 0)
    win_rate = round(wins / len(closed) * 100, 1) if closed else 0

    return (
        f"💰 <b>账户概览</b>\n\n"
        f"动态余额：<b>{balance:.2f}U</b>\n"
        f"初始本金：{config.ACCOUNT_BALANCE}U\n"
        f"累计盈亏：<b>{total_pnl:+.2f}U</b>\n"
        f"今日盈亏：<b>{today_pnl:+.2f}U</b>\n"
        f"胜率：{win_rate}%（{wins}/{len(closed)}）\n"
        f"复利仓位：{compound_stake:.0f}U\n"
        f"杠杆：{config.LEVERAGE}x\n"
    )


def cmd_positions() -> str:
    """持仓详情"""
    trades = load_json(TRADES_FILE, [])
    open_trades = [t for t in trades if t.get('status') == 'open']

    if not open_trades:
        return "📭 <b>当前无持仓</b>"

    lines = [f"📊 <b>当前持仓（{len(open_trades)}笔）</b>\n"]

    for t in open_trades:
        symbol = t.get('symbol', '?')
        entry = t.get('entry_price', 0)
        current = t.get('current_price', entry)
        stake = t.get('stake_remaining', t.get('stake', 0))
        leverage = t.get('leverage', config.LEVERAGE)
        direction = t.get('direction', 'SHORT')

        # 计算浮盈
        if direction == 'LONG':
            pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0
        else:
            pnl_pct = (entry - current) / entry * 100 if entry > 0 else 0

        pnl_usd = stake * leverage * pnl_pct / 100
        tp1_locked = t.get('tp1_locked_pnl', 0)

        # 止损位
        hard_stop = t.get('hard_stop_price', 0)
        trail_stop = t.get('trail_stop_price', 0)
        tp1 = t.get('take_profit_1', 0)
        tp2 = t.get('take_profit_2', 0)

        emoji = "🟢" if pnl_pct > 0 else "🔴"
        tp1_tag = " ✅TP1" if t.get('tp1_triggered') else ""

        lines.append(
            f"{emoji} <b>{symbol}</b> {direction}{tp1_tag}\n"
            f"   入场: {entry:.6f} → 现价: {current:.6f}\n"
            f"   浮盈: <b>{pnl_pct:+.1f}% = {pnl_usd:+.2f}U</b>"
            f"{f' (TP1锁{tp1_locked:+.2f}U)' if tp1_locked else ''}\n"
            f"   止损: 硬={hard_stop:.6f}"
            f"{f' | 移动={trail_stop:.6f}' if trail_stop else ''}\n"
            f"   止盈: TP1={tp1:.6f} | TP2={tp2:.6f}\n"
        )

    return "\n".join(lines)


def cmd_candidates() -> str:
    """候选池"""
    candidates = load_json(CANDIDATES_FILE, [])

    if not candidates:
        return "📭 <b>候选池为空</b>"

    # 分离：等待中 vs 已触发
    waiting = [c for c in candidates if not c.get('triggered')]
    triggered = [c for c in candidates if c.get('triggered')]

    lines = [f"📋 <b>候选池（{len(waiting)}个等待中）</b>\n"]

    for c in waiting[:10]:  # 最多显示10个
        symbol = c.get('symbol', '?')
        rsi = c.get('rsi_1d', 0)
        pct = c.get('pct24h', 0)
        yao = c.get('yao_score', 0)
        yao_emoji = "🔥" if yao >= 2 else ("⚡" if yao == 1 else "📌")

        lines.append(f"  {yao_emoji} {symbol} | RSI={rsi} | 24h={pct:+.1f}% | 妖={yao}/3")

    if len(waiting) > 10:
        lines.append(f"  ...还有 {len(waiting) - 10} 个")

    return "\n".join(lines)


def cmd_risk() -> str:
    """风控状态"""
    risk = load_json(RISK_FILE, {})

    daily_loss = risk.get('daily_loss', 0)
    trades_opened = risk.get('daily_trades_opened', 0)
    consec = risk.get('consecutive_losses', 0)
    paused = risk.get('paused_until')
    stake = risk.get('total_open_stake', 0)

    status = "🟢 正常"
    if paused:
        status = f"🔴 暂停至 {paused[:16]} UTC"
    elif daily_loss >= config.RISK_MAX_DAILY_LOSS:
        status = "🔴 今日停止（亏损达上限）"
    elif daily_loss >= config.RISK_MAX_DAILY_LOSS * 0.7:
        status = "🟡 接近限额"

    return (
        f"🛡️ <b>风控状态</b>\n\n"
        f"状态：{status}\n"
        f"今日亏损：{daily_loss:.1f} / {config.RISK_MAX_DAILY_LOSS}U\n"
        f"今日开仓：{trades_opened} / {config.RISK_MAX_DAILY_TRADES} 次\n"
        f"连续亏损：{consec} / {config.RISK_CONSECUTIVE_LOSS_PAUSE} 次\n"
        f"持仓占用：{stake:.0f}U\n"
        f"冷却期：{config.COOLDOWN_HOURS}h\n"
    )


def cmd_status() -> str:
    """综合状态（持仓概览 + 风控）"""
    trades = load_json(TRADES_FILE, [])
    open_trades = [t for t in trades if t.get('status') == 'open']

    # 持仓概览
    if open_trades:
        total_pnl = 0
        position_lines = []
        for t in open_trades:
            entry = t.get('entry_price', 0)
            current = t.get('current_price', entry)
            stake = t.get('stake_remaining', t.get('stake', 0))
            leverage = t.get('leverage', config.LEVERAGE)
            direction = t.get('direction', 'SHORT')

            if direction == 'LONG':
                pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0
            else:
                pnl_pct = (entry - current) / entry * 100 if entry > 0 else 0

            pnl_usd = stake * leverage * pnl_pct / 100
            total_pnl += pnl_usd + t.get('tp1_locked_pnl', 0)

            emoji = "🟢" if pnl_pct > 0 else "🔴"
            position_lines.append(f"  {emoji} {t.get('symbol')} {pnl_pct:+.1f}% ({pnl_usd:+.1f}U)")

        pos_text = "\n".join(position_lines)
        header = f"📊 <b>持仓 {len(open_trades)} 笔 | 浮盈 {total_pnl:+.2f}U</b>\n{pos_text}"
    else:
        header = "📭 <b>当前无持仓</b>"

    # 风控简报
    risk = load_json(RISK_FILE, {})
    daily_loss = risk.get('daily_loss', 0)
    paused = risk.get('paused_until')
    risk_status = "🟢" if not paused and daily_loss < config.RISK_MAX_DAILY_LOSS else "🔴"

    balance = get_dynamic_balance()

    return (
        f"{header}\n\n"
        f"💰 余额: {balance:.2f}U | {risk_status} 风控: 亏{daily_loss:.1f}/{config.RISK_MAX_DAILY_LOSS}U"
    )


# ══════════════════════════════════════════════════════════════════
#  指令路由
# ══════════════════════════════════════════════════════════════════

COMMANDS = {
    '/status': cmd_status,
    '/balance': cmd_balance,
    '/positions': cmd_positions,
    '/candidates': cmd_candidates,
    '/risk': cmd_risk,
    '/help': cmd_help,
    '/start': cmd_help,  # TG bot 首次 /start 也显示帮助
}


def handle_command(text: str) -> str:
    """路由指令到对应处理函数"""
    cmd = text.strip().split()[0].lower().split('@')[0]  # 去掉 @botname 后缀
    handler = COMMANDS.get(cmd)
    if handler:
        try:
            return handler()
        except Exception as e:
            logger.error(f"指令处理异常 ({cmd}): {e}")
            return f"❌ 指令执行失败: {e}"
    return None  # 非指令消息，不回复


# ══════════════════════════════════════════════════════════════════
#  Telegram API 轮询
# ══════════════════════════════════════════════════════════════════

def _send_reply(chat_id: str, text: str):
    """发送回复消息"""
    try:
        _requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except Exception as e:
        logger.error(f"回复消息失败: {e}")


def _poll_updates():
    """拉取新消息"""
    global _last_update_id
    try:
        resp = _requests.get(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates",
            params={
                "offset": _last_update_id + 1,
                "timeout": 30,  # 长轮询 30 秒
                "allowed_updates": '["message"]',
            },
            timeout=35,
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        if not data.get("ok"):
            return []

        return data.get("result", [])
    except _requests.exceptions.Timeout:
        return []  # 长轮询超时是正常的
    except Exception as e:
        logger.error(f"拉取消息异常: {e}")
        return []


def run_bot():
    """TG Bot 主循环（阻塞式）"""
    global _last_update_id

    if not TG_BOT_TOKEN:
        logger.error("TG_BOT_TOKEN 未设置，TG Bot 无法启动")
        return

    logger.info("🤖 TG Bot 已启动，等待指令...")

    while True:
        try:
            updates = _poll_updates()

            for update in updates:
                _last_update_id = update.get("update_id", _last_update_id)

                message = update.get("message", {})
                text = message.get("text", "")
                chat_id = str(message.get("chat", {}).get("id", ""))

                # 安全检查：只响应配置的 chat_id
                if TG_CHAT_ID and chat_id != TG_CHAT_ID:
                    logger.warning(f"拒绝未授权 chat_id: {chat_id}")
                    continue

                if not text.startswith('/'):
                    continue

                # 处理指令
                reply = handle_command(text)
                if reply:
                    _send_reply(chat_id, reply)
                    logger.info(f"处理指令: {text.split()[0]} → 已回复")

        except Exception as e:
            logger.error(f"Bot 循环异常: {e}")
            time.sleep(5)


def start_bot_thread():
    """在后台线程启动 TG Bot（非阻塞）"""
    if not TG_BOT_TOKEN:
        logger.warning("TG_BOT_TOKEN 未设置，跳过 TG Bot")
        return None

    thread = threading.Thread(target=run_bot, daemon=True, name="tg-bot")
    thread.start()
    logger.info("TG Bot 后台线程已启动")
    return thread


if __name__ == '__main__':
    run_bot()
