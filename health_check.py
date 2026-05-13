#!/usr/bin/env python3
"""
异常报警模块 v2.0
定期检查系统健康状态，异常时 TG 告警。

检查项：
  1. 连续N天无信号 → 市场冷淡提醒
  2. API 连接异常 → 网络/Binance 故障
  3. 数据文件损坏/过大 → 磁盘/逻辑异常
  4. 累计亏损过大 → 策略失效警告
  5. 系统运行状态 → 各模块最后执行时间

用法：
  python3 health_check.py        # 执行健康检查
  python3 health_check.py --test # 测试TG推送
"""

import os
import sys

import requests

import config
from common import (
    TRADES_FILE, CANDIDATES_FILE, RISK_FILE,
    setup_logger, send_tg, load_json, utcnow, today_str, parse_iso,
)

logger = setup_logger("health_check")

# ── 告警阈值 ──
NO_SIGNAL_DAYS = 3          # 连续N天无触发信号
MAX_TOTAL_LOSS_PCT = 50     # 累计亏损占本金比例超过此值告警（%）
MAX_FILE_SIZE_MB = 10       # 数据文件超过此大小告警
API_TIMEOUT = 10            # API 超时秒数


def check_api_connectivity() -> tuple:
    """检查 Binance API 是否可达"""
    try:
        r = requests.get("https://api.binance.com/api/v3/ping", timeout=API_TIMEOUT)
        if r.status_code == 200:
            return True, "Binance API 正常"
        return False, f"Binance API 返回 HTTP {r.status_code}"
    except requests.Timeout:
        return False, "Binance API 超时"
    except requests.ConnectionError:
        return False, "Binance API 连接失败（网络问题）"
    except Exception as e:
        return False, f"Binance API 异常: {e}"


def check_no_signal() -> tuple:
    """检查是否连续多天无信号"""
    trades = load_json(TRADES_FILE, [])
    if not trades:
        return True, "无交易记录（可能刚部署）"

    # 找最近一次开仓时间
    open_times = [t.get('opened_at', '') for t in trades if t.get('opened_at')]
    if not open_times:
        return True, "无开仓记录"

    latest = max(open_times)
    latest_dt = parse_iso(latest)
    days_since = (utcnow() - latest_dt).days

    if days_since >= NO_SIGNAL_DAYS:
        return False, f"已连续 {days_since} 天无新信号（最后开仓：{latest[:10]}）"
    return True, f"最近 {days_since} 天前有信号"


def check_total_loss() -> tuple:
    """检查累计亏损是否过大"""
    trades = load_json(TRADES_FILE, [])
    closed = [t for t in trades if t.get('status') == 'closed']
    if not closed:
        return True, "无已平仓交易"

    total_pnl = sum(t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in closed)
    loss_pct = abs(total_pnl) / config.ACCOUNT_BALANCE * 100 if total_pnl < 0 else 0

    if total_pnl < 0 and loss_pct >= MAX_TOTAL_LOSS_PCT:
        return False, f"累计亏损 {total_pnl:.1f}U（本金的 {loss_pct:.0f}%），策略可能失效"
    return True, f"累计盈亏 {total_pnl:+.1f}U"


def check_file_health() -> tuple:
    """检查数据文件是否正常"""
    issues = []
    files = [TRADES_FILE, CANDIDATES_FILE, RISK_FILE]

    for filepath in files:
        if not os.path.exists(filepath):
            continue

        # 文件大小
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            issues.append(f"{os.path.basename(filepath)} 过大（{size_mb:.1f}MB）")

        # 尝试读取
        data = load_json(filepath, None)
        if data is None:
            issues.append(f"{os.path.basename(filepath)} 读取失败/损坏")

    if issues:
        return False, "文件异常: " + "; ".join(issues)
    return True, "数据文件正常"


def check_daily_performance() -> tuple:
    """检查今日表现"""
    trades = load_json(TRADES_FILE, [])
    today = today_str()

    today_closed = [
        t for t in trades
        if t.get('status') == 'closed' and t.get('closed_at', '').startswith(today)
    ]

    if not today_closed:
        return True, "今日无平仓"

    today_pnl = sum(t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in today_closed)
    wins = sum(1 for t in today_closed if (t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)) > 0)

    msg = f"今日 {len(today_closed)} 笔，盈亏 {today_pnl:+.1f}U，胜率 {wins}/{len(today_closed)}"

    # 今日大亏告警
    if today_pnl < -config.RISK_MAX_DAILY_LOSS * 0.8:
        return False, msg + "（接近日亏上限！）"
    return True, msg


def run_health_check():
    """执行完整健康检查"""
    logger.info("=== 执行健康检查 ===")

    checks = [
        ("API连接", check_api_connectivity),
        ("信号频率", check_no_signal),
        ("累计盈亏", check_total_loss),
        ("文件健康", check_file_health),
        ("今日表现", check_daily_performance),
    ]

    results = []
    has_alert = False

    for name, func in checks:
        try:
            ok, msg = func()
            status = "✅" if ok else "⚠️"
            if not ok:
                has_alert = True
            results.append(f"{status} {name}: {msg}")
            logger.info(f"  {status} {name}: {msg}")
        except Exception as e:
            results.append(f"❌ {name}: 检查异常 ({e})")
            has_alert = True
            logger.error(f"  ❌ {name}: {e}")

    # 有异常时推送 TG
    if has_alert:
        msg = "🏥 <b>系统健康检查告警</b>\n\n" + "\n".join(results)
        send_tg(msg)
        logger.warning("健康检查发现异常，已推送告警")
    else:
        logger.info("健康检查全部通过")

    return has_alert, results


if __name__ == '__main__':
    if '--test' in sys.argv:
        send_tg("🏥 <b>健康检查测试</b>\n\nTG 推送功能正常 ✅")
        print("TG 测试推送已发送")
    else:
        has_alert, results = run_health_check()
        print("\n".join(results))
        sys.exit(1 if has_alert else 0)
