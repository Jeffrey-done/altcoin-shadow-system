#!/usr/bin/env python3
"""
扫描器诊断脚本 — 快速定位"为什么没信号"
检查项：
  1. BTC 过滤器状态（是否阻断了所有信号）
  2. 候选池内容（是否为空/已过期）
  3. 风控状态（是否暂停/达上限）
  4. 冷却期（哪些币在冷却中）
  5. 最近交易记录（连亏情况）
  6. 市场扫描预检（当前有多少币满足基础过滤条件）
"""

import json
import os
import sys

# 确保项目目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from common import (
    CANDIDATES_FILE, TRADES_FILE, RISK_FILE,
    load_json, utcnow, parse_iso, today_str,
    get_current_account_id, filter_trades_by_account,
)


def section(title):
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print(f"{'═' * 60}")


def check_btc_filter():
    """检查 BTC 过滤器"""
    section("1. BTC 趋势过滤")

    if not config.BTC_FILTER_ENABLED:
        print("  ✅ BTC 过滤已关闭，不会阻断信号")
        return

    print(f"  配置: BTC_CRASH_THRESHOLD = {config.BTC_CRASH_THRESHOLD}%")

    try:
        from exchange_manager import get_btc_24h_change_multi
        btc_pct = get_btc_24h_change_multi()
        print(f"  当前 BTC 24h 涨跌幅: {btc_pct:+.2f}%")

        if btc_pct <= config.BTC_CRASH_THRESHOLD:
            print(f"  🔴 BTC 过滤器已触发！（{btc_pct:.2f}% <= {config.BTC_CRASH_THRESHOLD}%）")
            print(f"     → 所有做空信号被阻断，直到 BTC 24h 跌幅回到 {config.BTC_CRASH_THRESHOLD}% 以内")
        else:
            print(f"  ✅ BTC 过滤器未触发，不会阻断信号")
    except Exception as e:
        print(f"  ⚠️ 无法获取 BTC 数据: {e}")


def check_candidates():
    """检查候选池"""
    section("2. 候选池状态")

    candidates = load_json(CANDIDATES_FILE, [])
    print(f"  候选池文件: {CANDIDATES_FILE}")
    print(f"  候选数量: {len(candidates)}")

    if not candidates:
        print("  🔴 候选池为空！没有币种等待触发")
        print("     可能原因:")
        print(f"     - 当前市场没有小币满足: 价格<{config.PRICE_MAX}U + 24h涨>{config.PCT_24H_MIN}% + 日线RSI>{config.DAILY_RSI_MIN}")
        print(f"     - 候选过期时间: {config.CANDIDATE_EXPIRE_HOURS}h（候选加入后{config.CANDIDATE_EXPIRE_HOURS}h未触发会被清理）")
        return

    now = utcnow()
    print(f"\n  候选详情:")
    print(f"  {'币种':<16} {'RSI_1d':<8} {'24h%':<8} {'妖币':<6} {'加入时间':<22} {'剩余(h)':<10} {'已触发'}")
    print(f"  {'─' * 90}")

    for c in candidates:
        symbol = c.get('symbol', '?')
        rsi_1d = c.get('rsi_1d', 0)
        pct24h = c.get('pct24h', 0)
        yao_score = c.get('yao_score', 0)
        added_at = c.get('added_at', '')
        triggered = c.get('triggered', False)

        remaining = ''
        if added_at:
            try:
                added_dt = parse_iso(added_at)
                age_hours = (now - added_dt).total_seconds() / 3600
                remaining_h = config.CANDIDATE_EXPIRE_HOURS - age_hours
                remaining = f"{remaining_h:.1f}h"
                if remaining_h < 0:
                    remaining = "⚠️已过期"
            except Exception:
                remaining = "?"

        trigger_mark = "✅是" if triggered else "❌否"
        print(f"  {symbol:<16} {rsi_1d:<8.1f} {pct24h:<8.1f} {yao_score:<6} {added_at[:19]:<22} {remaining:<10} {trigger_mark}")


def check_risk_state():
    """检查风控状态"""
    section("3. 风控状态")

    risk_data = load_json(RISK_FILE, {})

    if not risk_data:
        print("  ⚠️ 风控状态文件为空或不存在")
        return

    # 处理 v1/v2 格式
    if '_version' not in risk_data and 'date' in risk_data:
        # v1 格式
        accounts = {'_default': risk_data}
    else:
        accounts = risk_data.get('accounts', {})

    if not accounts:
        print("  ⚠️ 无账户风控数据")
        return

    now = utcnow()
    any_blocked = False

    for acc_id, state in accounts.items():
        print(f"\n  账户: {acc_id}")
        print(f"    日期: {state.get('date', '?')}")

        daily_loss = state.get('daily_loss', 0)
        daily_trades = state.get('daily_trades_opened', 0)
        consec_losses = state.get('consecutive_losses', 0)
        total_stake = state.get('total_open_stake', 0)
        paused_until = state.get('paused_until')

        # 日亏损
        loss_status = "🔴 已达上限!" if daily_loss >= config.RISK_MAX_DAILY_LOSS else "✅"
        print(f"    今日亏损: {daily_loss:.1f} / {config.RISK_MAX_DAILY_LOSS}U  {loss_status}")
        if daily_loss >= config.RISK_MAX_DAILY_LOSS:
            any_blocked = True

        # 日开仓次数
        trades_status = "🔴 已达上限!" if daily_trades >= config.RISK_MAX_DAILY_TRADES else "✅"
        print(f"    今日开仓: {daily_trades} / {config.RISK_MAX_DAILY_TRADES}次  {trades_status}")
        if daily_trades >= config.RISK_MAX_DAILY_TRADES:
            any_blocked = True

        # 连亏
        consec_status = ""
        if consec_losses >= config.RISK_CONSECUTIVE_LOSS_PAUSE:
            consec_status = "🔴 触发暂停!"
            any_blocked = True
        elif consec_losses >= 2:
            consec_status = "⚠️ 接近上限"
        else:
            consec_status = "✅"
        print(f"    连续亏损: {consec_losses} / {config.RISK_CONSECUTIVE_LOSS_PAUSE}次  {consec_status}")

        # 暂停状态
        if paused_until:
            try:
                pause_end = parse_iso(paused_until)
                if now < pause_end:
                    remaining = (pause_end - now).total_seconds() / 3600
                    print(f"    暂停状态: 🔴 暂停中！剩余 {remaining:.1f}h")
                    any_blocked = True
                else:
                    print(f"    暂停状态: ✅ 暂停已过期（下次开仓时自动解除）")
            except Exception:
                print(f"    暂停状态: ⚠️ 解析失败: {paused_until}")
        else:
            print(f"    暂停状态: ✅ 未暂停")

        # 持仓占比
        print(f"    当前持仓: {total_stake:.0f}U")

    if any_blocked:
        print(f"\n  🔴 存在风控阻断！部分或全部账户无法开仓")
    else:
        print(f"\n  ✅ 所有账户风控正常")


def check_cooldowns():
    """检查冷却期"""
    section("4. 冷却期检查")

    trades = load_json(TRADES_FILE, [])
    now = utcnow()
    cooldown_hours = config.COOLDOWN_HOURS

    # 找出最近平仓的币种
    symbols_in_cooldown = []
    symbols_today_closed = []

    today_dt = now.date()

    for t in reversed(trades):
        if t.get('status') != 'closed':
            continue
        symbol = t.get('symbol', '?')
        closed_at = t.get('closed_at', '')
        if not closed_at:
            continue

        close_type = t.get('close_type', '')
        close_reason = t.get('close_reason', '')

        try:
            closed_dt = parse_iso(closed_at)
        except Exception:
            continue

        # 检查止损冷却
        is_stop = '止损' in close_reason or 'stop' in close_reason.lower() or close_type in ('hard_stop', 'trail_stop', 'time_stop')
        if is_stop:
            hours_since = (now - closed_dt).total_seconds() / 3600
            if hours_since < cooldown_hours:
                remaining = cooldown_hours - hours_since
                symbols_in_cooldown.append((symbol, f"止损冷却中（{hours_since:.1f}h/{cooldown_hours}h，剩余{remaining:.1f}h）"))

        # 检查同日平仓
        if closed_dt.date() == today_dt:
            symbols_today_closed.append((symbol, f"今日已平仓（{closed_at[:16]}）"))

    if symbols_in_cooldown:
        print(f"  止损冷却中的币种 ({len(symbols_in_cooldown)}):")
        for sym, reason in symbols_in_cooldown:
            print(f"    ❄️ {sym}: {reason}")
    else:
        print("  ✅ 无币种在止损冷却中")

    if symbols_today_closed:
        print(f"\n  今日已平仓的币种 ({len(symbols_today_closed)}):")
        print("  ⚠️ 注意: is_in_cooldown() 会阻止这些币种今日再次开仓")
        for sym, reason in symbols_today_closed:
            print(f"    🔒 {sym}: {reason}")
    else:
        print("  ✅ 今日无平仓记录")


def check_recent_trades():
    """检查最近交易"""
    section("5. 最近交易记录")

    trades = load_json(TRADES_FILE, [])
    print(f"  总交易数: {len(trades)}")

    open_trades = [t for t in trades if t.get('status') == 'open']
    closed_trades = [t for t in trades if t.get('status') == 'closed']

    print(f"  当前持仓: {len(open_trades)}")
    print(f"  已平仓: {len(closed_trades)}")

    if open_trades:
        print(f"\n  当前持仓详情:")
        for t in open_trades:
            symbol = t.get('symbol', '?')
            entry = t.get('entry_price', 0)
            opened = t.get('opened_at', '?')[:16]
            exchange = t.get('exchange', '?')
            stake = t.get('stake', 0)
            print(f"    📌 {symbol} @ {entry:.6f} | {exchange} | {stake}U | 开仓:{opened}")

    # 最近5笔已平仓
    if closed_trades:
        recent = sorted(closed_trades, key=lambda x: x.get('closed_at', ''), reverse=True)[:5]
        print(f"\n  最近5笔平仓:")
        print(f"  {'币种':<14} {'PnL':<10} {'平仓原因':<20} {'平仓时间':<18}")
        print(f"  {'─' * 65}")
        for t in recent:
            symbol = t.get('symbol', '?')
            pnl = t.get('pnl', 0) + t.get('tp1_locked_pnl', 0)
            reason = t.get('close_reason', t.get('close_type', '?'))[:18]
            closed_at = t.get('closed_at', '?')[:16]
            pnl_mark = f"+{pnl:.2f}U" if pnl >= 0 else f"{pnl:.2f}U"
            print(f"  {symbol:<14} {pnl_mark:<10} {reason:<20} {closed_at:<18}")

        # 统计连亏
        consecutive_losses = 0
        for t in sorted(closed_trades, key=lambda x: x.get('closed_at', ''), reverse=True):
            pnl = t.get('pnl', 0) + t.get('tp1_locked_pnl', 0)
            if pnl < 0:
                consecutive_losses += 1
            else:
                break
        if consecutive_losses > 0:
            print(f"\n  ⚠️ 当前连续亏损: {consecutive_losses} 笔")
        else:
            print(f"\n  ✅ 最近一笔是盈利的")


def check_market_conditions():
    """检查当前市场是否有币满足基础过滤"""
    section("6. 市场条件预检（需网络）")

    try:
        import ccxt
        exchange = ccxt.binance({'enableRateLimit': True})
        print("  正在获取全市场行情...")
        tickers = exchange.fetch_tickers()

        # 统计各层过滤
        usdt_pairs = {k: v for k, v in tickers.items() if k.endswith('/USDT')}
        print(f"  USDT 交易对总数: {len(usdt_pairs)}")

        price_ok = {k: v for k, v in usdt_pairs.items()
                    if (v.get('last') or 0) > 0 and (v.get('last') or 999) <= config.PRICE_MAX}
        print(f"  价格 <= {config.PRICE_MAX}U: {len(price_ok)}")

        vol_ok = {k: v for k, v in price_ok.items()
                  if (v.get('quoteVolume') or 0) >= config.VOL_MIN}
        print(f"  + 成交量 >= {config.VOL_MIN/10000:.0f}万U: {len(vol_ok)}")

        pct_ok = {k: v for k, v in vol_ok.items()
                  if (v.get('percentage') or 0) >= config.PCT_24H_MIN}
        print(f"  + 24h涨幅 >= {config.PCT_24H_MIN}%: {len(pct_ok)}")

        if pct_ok:
            print(f"\n  满足基础过滤的币种（还需日线RSI>{config.DAILY_RSI_MIN}才能入候选池）:")
            sorted_coins = sorted(pct_ok.items(), key=lambda x: x[1].get('percentage', 0), reverse=True)[:10]
            print(f"  {'币种':<14} {'价格':<12} {'24h涨幅':<10} {'成交量(万U)':<14}")
            print(f"  {'─' * 55}")
            for sym, ticker in sorted_coins:
                price = ticker.get('last', 0)
                pct = ticker.get('percentage', 0)
                vol = (ticker.get('quoteVolume') or 0) / 10000
                print(f"  {sym:<14} {price:<12.6f} {pct:<10.1f}% {vol:<14.0f}")
        else:
            print(f"\n  🔴 当前市场没有任何币种同时满足价格+成交量+涨幅条件！")
            print(f'     这是最可能的"无信号"原因 - 市场太平静')

    except Exception as e:
        print(f"  ⚠️ 市场检查失败（需要网络连接）: {e}")
        print(f"     如果在本地运行，这不影响其他诊断结果")


def check_scheduler():
    """检查调度器相关"""
    section("7. 附加信息")
    print(f"  策略参数摘要:")
    print(f"    DAILY_RSI_MIN = {config.DAILY_RSI_MIN} (日线RSI超买阈值)")
    print(f"    H4_RSI_ENTER = {config.H4_RSI_ENTER} (4h RSI需跌破此值)")
    print(f"    H4_RSI_DROP = {config.H4_RSI_DROP} (4h RSI需从峰值回落的点数)")
    print(f"    PRICE_MAX = {config.PRICE_MAX}U")
    print(f"    VOL_MIN = {config.VOL_MIN/10000:.0f}万U")
    print(f"    PCT_24H_MIN = {config.PCT_24H_MIN}%")
    print(f"    CANDIDATE_EXPIRE_HOURS = {config.CANDIDATE_EXPIRE_HOURS}h")
    print(f"    COOLDOWN_HOURS = {config.COOLDOWN_HOURS}h")
    print(f"    SCORE_SKIP_THRESHOLD = {config.SCORE_SKIP_THRESHOLD}分")
    print(f"    LIVE_MODE = {config.LIVE_MODE}")
    print(f"    OKX_LIVE_MODE = {config.OKX_LIVE_MODE}")
    print(f"    BTC_FILTER_ENABLED = {config.BTC_FILTER_ENABLED}")
    print(f"    BTC_CRASH_THRESHOLD = {config.BTC_CRASH_THRESHOLD}%")


if __name__ == '__main__':
    print("🔍 扫描器诊断报告")
    print(f"   时间: {utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"   当日: {today_str()}")

    check_btc_filter()
    check_candidates()
    check_risk_state()
    check_cooldowns()
    check_recent_trades()
    check_market_conditions()
    check_scheduler()

    print(f"\n{'═' * 60}")
    print("  诊断完成")
    print(f"{'═' * 60}")
    print("\n💡 排查优先级:")
    print("   1. BTC过滤是否触发 → 等BTC企稳即可")
    print("   2. 候选池是否为空 → 市场太冷/条件太严")
    print("   3. 风控是否暂停 → 检查连亏/日亏损上限")
    print("   4. 冷却期 → 等24h自动解除")
    print("   5. 市场条件 → 无币满足过滤=正常（行情问题）")
