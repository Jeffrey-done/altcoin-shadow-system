#!/usr/bin/env python3
"""
小币种影子空单追踪器
每日推送持仓盈亏状态到Telegram
"""
import json, os, ccxt, requests
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADES_FILE = os.path.join(SCRIPT_DIR, 'altcoin_shadow_trades.json')

from dotenv import load_dotenv
load_dotenv(os.path.join(SCRIPT_DIR, '.env'), override=True)
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', '')
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID', '8068489553')

def send_tg(msg):
    token = TG_BOT_TOKEN
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
        timeout=10
    )

def track():
    if not os.path.exists(TRADES_FILE):
        print("无交易记录")
        return

    trades = json.load(open(TRADES_FILE))
    open_trades = [t for t in trades if t.get('status') == 'open']
    closed_trades = [t for t in trades if t.get('status') == 'closed']

    if not open_trades:
        print("无持仓中的空单")
        return

    binance = ccxt.binance({'enableRateLimit': True})
    now = datetime.utcnow().isoformat()
    updated = False

    lines = ["📊 <b>小币种影子空单日报</b>", f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]

    for t in open_trades:
        symbol = t['symbol']
        entry  = t['entry_price']
        stake  = t['stake']
        tp     = t['take_profit']
        sl     = t['stop_loss']

        try:
            current = binance.fetch_ticker(symbol)['last']
        except:
            current = entry

        # 做空盈亏：价格下跌=盈利（第一档触发后按剩余仓位计算）
        pnl_pct = (entry - current) / entry * 100
        effective_stake = t.get('stake_remaining', stake)
        pnl_usd = effective_stake * pnl_pct / 100
        t['pnl'] = round(pnl_usd, 2)
        t['current_price'] = current

        emoji = "🟢" if pnl_pct > 0 else "🔴"
        status_str = f"{pnl_pct:+.1f}% ({pnl_usd:+.2f}U)"

        # ── 移动止损：更新历史最高盈利点 ──
        best_pnl = t.get('best_pnl_pct', 0)
        if pnl_pct > best_pnl:
            t['best_pnl_pct'] = round(pnl_pct, 2)
            trail_pct = t.get('trail_stop_pct', 0.10)
            # 移动止损触发价 = 入场价 × (1 - (最高盈利% - 止损回撤%))
            trail_trigger = entry * (1 - (pnl_pct/100 - trail_pct))
            t['trail_stop_price'] = round(trail_trigger, 6)
            updated = True

        # ── 分批止盈 + 移动止损 + 时间止损 ──
        closed_reason = None
        tp1 = t.get('take_profit_1')
        tp2 = t.get('take_profit_2', tp)
        tp1_done = t.get('tp1_triggered', False)
        trail_price = t.get('trail_stop_price')
        max_days = t.get('max_hold_days', 7)
        opened_at = t.get('opened_at', now)
        hold_days = (datetime.utcnow() - datetime.fromisoformat(opened_at[:19])).days

        if not tp1_done and tp1 and current <= tp1:
            # 第一档止盈-20%：锁定50%仓位
            t['tp1_triggered'] = True
            t['stake_remaining'] = t.get('stake', 200) / 2
            pnl_usd = (t['stake'] / 2) * pnl_pct / 100
            t['pnl'] = round(pnl_usd, 2)
            closed_reason = "✅ 第一档止盈（-20%，50%仓位锁定）"
            updated = True
        elif tp1_done and current <= tp2:
            # 第二档止盈-35%：全仓平仓
            closed_reason = "✅ 第二档止盈（-35%，全仓平仓）"
            t['status'] = 'closed'
            t['closed_at'] = now
            updated = True
        elif trail_price and t.get('best_pnl_pct', 0) >= 5 and current >= trail_price:
            # 移动止损：最高盈利≥5%后，回撤10%触发（防止刚开仓就止损）
            closed_reason = f"🛑 移动止损触发（最高盈利{t.get('best_pnl_pct',0):.1f}%，回撤至{pnl_pct:.1f}%）"
            t['status'] = 'closed'
            t['closed_at'] = now
            updated = True
        elif hold_days >= max_days and pnl_pct < 5:
            # 时间止损：持仓超7天且盈利<5%，强制平仓
            closed_reason = f"⏰ 时间止损（持仓{hold_days}天，盈利仅{pnl_pct:.1f}%）"
            t['status'] = 'closed'
            t['closed_at'] = now
            updated = True

        if closed_reason:
            lines.append(f"{closed_reason} <b>{symbol}</b>")
            lines.append(f"   入场: {entry:.5f} → 现价: {current:.5f}")
            lines.append(f"   盈亏: <b>{pnl_usd:+.2f}U ({pnl_pct:+.1f}%)</b>")
            # 止盈触发时立即单独推送通知
            alert_msg = (
                f"🎯 <b>止盈触发提醒</b>\n\n"
                f"币种：<b>{symbol}</b>\n"
                f"方向：做空\n"
                f"入场价：{entry:.5f}\n"
                f"平仓价：{current:.5f}\n"
                f"盈亏：<b>{pnl_usd:+.2f}U（{pnl_pct:+.1f}%）</b>\n\n"
                f"影子空单已自动记录平仓 ✅"
            )
            send_tg(alert_msg)
        else:
            lines.append(f"{emoji} <b>{symbol}</b> 做空持仓中")
            lines.append(f"   入场: {entry:.5f} | 现价: {current:.5f}")
            lines.append(f"   浮动盈亏: <b>{status_str}</b>")
            trail_price = t.get('trail_stop_price')
            trail_str = f" | 移动止损: {trail_price:.5f}" if trail_price else ""
            if tp1_done:
                lines.append(f"   ✅第一档已触发 | 第二档止盈: {tp2:.5f}(-35%){trail_str}")
            else:
                lines.append(f"   第一档: {tp1:.5f}(-20%) | 第二档: {tp2:.5f}(-35%){trail_str}")
        lines.append("")

    # 汇总
    total_open_pnl = sum(t.get('pnl', 0) for t in open_trades if t.get('status') == 'open')
    total_closed_pnl = sum(t.get('pnl', 0) for t in trades if t.get('status') == 'closed')
    lines.append(f"💰 持仓浮盈: <b>{total_open_pnl:+.2f}U</b>")
    lines.append(f"💰 已实现盈亏: <b>{total_closed_pnl:+.2f}U</b>")

    # 保存更新
    json.dump(trades, open(TRADES_FILE, 'w'), indent=2, ensure_ascii=False)

    msg = "\n".join(lines)
    send_tg(msg)
    print(msg)

if __name__ == '__main__':
    import sys
    check_only = '--check-only' in sys.argv
    if check_only:
        # 静默模式：只检查止盈触发（分批止盈逻辑），不发日报
        if os.path.exists(TRADES_FILE):
            trades = json.load(open(TRADES_FILE))
            binance = ccxt.binance({'enableRateLimit': True})
            now = datetime.utcnow().isoformat()
            changed = False
            for t in [x for x in trades if x.get('status') == 'open']:
                try:
                    current = binance.fetch_ticker(t['symbol'])['last']
                    entry   = t['entry_price']
                    stake   = t['stake']
                    tp1     = t.get('take_profit_1')
                    tp2     = t.get('take_profit_2', t.get('take_profit'))
                    tp1_done = t.get('tp1_triggered', False)
                    effective_stake = t.get('stake_remaining', stake)

                    pnl_pct = (entry - current) / entry * 100
                    pnl_usd = effective_stake * pnl_pct / 100
                    t['current_price'] = current
                    t['pnl'] = round(pnl_usd, 2)

                    # 更新移动止损最高盈利点
                    best_pnl = t.get('best_pnl_pct', 0)
                    if pnl_pct > best_pnl:
                        t['best_pnl_pct'] = round(pnl_pct, 2)
                        trail_pct = t.get('trail_stop_pct', 0.10)
                        trail_trigger = entry * (1 - (pnl_pct/100 - trail_pct))
                        t['trail_stop_price'] = round(trail_trigger, 6)
                        changed = True

                    alert_msg = None
                    trail_price = t.get('trail_stop_price')
                    max_days = t.get('max_hold_days', 7)
                    opened_at = t.get('opened_at', now)
                    hold_days = (datetime.utcnow() - datetime.fromisoformat(opened_at[:19])).days

                    if not tp1_done and tp1 and current <= tp1:
                        # 第一档止盈-20%
                        t['tp1_triggered'] = True
                        t['stake_remaining'] = stake / 2
                        pnl_usd = (stake / 2) * pnl_pct / 100
                        t['pnl'] = round(pnl_usd, 2)
                        t['current_price'] = current
                        alert_msg = (
                            f"🎯 <b>第一档止盈触发（-20%）</b>\n\n"
                            f"币种：<b>{t['symbol']}</b>\n"
                            f"入场价：{entry:.5f} → 现价：{current:.5f}\n"
                            f"50%仓位锁定盈利：<b>{pnl_usd:+.2f}U（{pnl_pct:+.1f}%）</b>\n"
                            f"剩余50%等待第二档-35%止盈 ✅"
                        )
                        changed = True
                        print(f"[第一档止盈-20%] {t['symbol']} @ {current}")

                    elif tp1_done and tp2 and current <= tp2:
                        # 第二档止盈-35%全仓平仓
                        t['status'] = 'closed'
                        t['closed_at'] = now
                        t['pnl'] = round(pnl_usd, 2)
                        t['current_price'] = current
                        alert_msg = (
                            f"🎯 <b>第二档止盈触发（-35%全仓平仓）</b>\n\n"
                            f"币种：<b>{t['symbol']}</b>\n"
                            f"入场价：{entry:.5f} → 现价：{current:.5f}\n"
                            f"最终盈亏：<b>{pnl_usd:+.2f}U（{pnl_pct:+.1f}%）</b>\n"
                            f"影子空单已全部平仓 ✅"
                        )
                        changed = True
                        print(f"[第二档止盈-35%] {t['symbol']} @ {current}")

                    elif trail_price and t.get('best_pnl_pct', 0) >= 5 and current >= trail_price:
                        # 移动止损触发（最高盈利≥5%后生效）
                        t['status'] = 'closed'
                        t['closed_at'] = now
                        t['pnl'] = round(pnl_usd, 2)
                        t['current_price'] = current
                        alert_msg = (
                            f"🛑 <b>移动止损触发</b>\n\n"
                            f"币种：<b>{t['symbol']}</b>\n"
                            f"入场价：{entry:.5f} → 现价：{current:.5f}\n"
                            f"历史最高：{t.get('best_pnl_pct',0):.1f}% | 当前：{pnl_pct:.1f}%\n"
                            f"盈亏：<b>{pnl_usd:+.2f}U</b> | 已自动平仓 ✅"
                        )
                        changed = True
                        print(f"[移动止损] {t['symbol']} @ {current}")

                    elif hold_days >= max_days and pnl_pct < 5:
                        # 时间止损：持仓超7天盈利<5%强制平仓
                        t['status'] = 'closed'
                        t['closed_at'] = now
                        t['pnl'] = round(pnl_usd, 2)
                        t['current_price'] = current
                        alert_msg = (
                            f"⏰ <b>时间止损触发</b>\n\n"
                            f"币种：<b>{t['symbol']}</b>\n"
                            f"持仓{hold_days}天，盈利仅{pnl_pct:.1f}%，强制平仓\n"
                            f"盈亏：<b>{pnl_usd:+.2f}U</b> ✅"
                        )
                        changed = True
                        print(f"[时间止损] {t['symbol']} 持仓{hold_days}天")

                    if alert_msg:
                        send_tg(alert_msg)

                except Exception as e:
                    print(f"检查失败 {t.get('symbol','')}: {e}")
            if changed:
                json.dump(trades, open(TRADES_FILE, 'w'), indent=2, ensure_ascii=False)
    else:
        track()
