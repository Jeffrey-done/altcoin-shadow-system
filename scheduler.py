#!/usr/bin/env python3
"""
定时任务调度器 — 替代 crontab
在 Docker 容器内按计划执行所有策略模块。
专注做空策略调度。
"""

import time
import threading
import traceback
from datetime import datetime, timezone

from common import setup_logger

logger = setup_logger("scheduler")


def run_task(name: str, func, timeout: int = None):
    """安全执行任务，捕获异常，支持超时"""
    import config
    if timeout is None:
        timeout = config.TASK_TIMEOUT_SECONDS
    
    result = [None]
    exception = [None]
    
    def target():
        try:
            func()
        except Exception as e:
            exception[0] = e
    
    logger.info(f"[{name}] 开始执行（超时={timeout}s）")
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    
    if thread.is_alive():
        logger.error(f"[{name}] ⚠️ 超时（>{timeout}s），跳过本次")
        from common import send_tg
        send_tg(f"⚠️ <b>任务超时</b>\n\n任务: {name}\n超时: {timeout}s\n已跳过本次执行")
    elif exception[0]:
        logger.error(f"[{name}] 异常: {exception[0]}\n{traceback.format_exc()}")
    else:
        logger.info(f"[{name}] 完成")


def get_utc_hour():
    return datetime.now(timezone.utc).hour


def get_utc_minute():
    return datetime.now(timezone.utc).minute


def main_loop():
    """主调度循环，每分钟检查一次"""
    logger.info("=== 调度器启动 ===")

    # 启动时对账一次：修正风控状态与交易记录的漂移（幽灵亏损预防）
    try:
        from risk_control import reconcile_risk_state
        diff = reconcile_risk_state(notify=True)
        if diff:
            logger.warning(f"启动对账修正了 {len(diff)} 项风控字段: {list(diff.keys())}")
        else:
            logger.info("启动对账：风控状态一致 ✅")
    except Exception as e:
        logger.error(f"启动对账异常: {e}")

    # 启动快速预筛后台线程
    try:
        from hot_scanner import start_hot_scanner_thread
        start_hot_scanner_thread()
    except Exception as e:
        logger.warning(f"快速预筛启动失败（非致命）: {e}")

    # 启动 TG Bot 后台线程
    try:
        from tg_bot import start_bot_thread
        start_bot_thread()
    except Exception as e:
        logger.warning(f"TG Bot 启动失败（非致命）: {e}")

    last_scan_hour = -1
    last_check_min = -1
    last_tracker_min = -1
    last_health_hour = -1
    last_daily_report_done = False
    last_optimize_done = False

    while True:
        now = datetime.now(timezone.utc)
        hour = now.hour
        minute = now.minute
        day = now.day

        # ── 每小时整点：日线扫描 ──
        if minute == 0 and hour != last_scan_hour:
            last_scan_hour = hour
            from altcoin_scanner import scan_daily
            run_task("日线扫描", scan_daily)

        # ── 每小时30分：候选确认 ──
        if minute == 30 and hour != last_check_min:
            last_check_min = hour
            from altcoin_scanner import check_candidates
            run_task("候选确认", check_candidates)

        # ── 每小时15分：止盈止损检查 ──
        if minute == 15 and hour != last_tracker_min:
            last_tracker_min = hour
            from altcoin_tracker import run as tracker_run
            run_task("止盈检查", lambda: tracker_run(check_only=True))

        # ── 每6小时：健康检查 ──
        if hour % 6 == 0 and minute == 45 and hour != last_health_hour:
            last_health_hour = hour
            from health_check import run_health_check
            run_task("健康检查", run_health_check)

        # ── 每天8:00 UTC：日报 ──
        if hour == 8 and minute == 0 and not last_daily_report_done:
            last_daily_report_done = True
            from altcoin_tracker import run as tracker_run
            run_task("日报推送", lambda: tracker_run(check_only=False))

        # ── 每周一9:00 UTC：自动优化建议 ──
        import config
        if hour == 9 and minute == 0 and now.weekday() == config.AUTO_OPTIMIZE_DAY and not last_optimize_done:
            last_optimize_done = True
            from auto_optimize import run_auto_optimize
            run_task("自动优化", run_auto_optimize)

        # 日期变更重置
        if hour == 0 and minute == 1:
            last_daily_report_done = False
            # 每日清理过期交易
            from common import cleanup_old_trades
            run_task("清理过期交易", cleanup_old_trades)

        # 周二重置优化标志（周一执行后，周二0点重置）
        if now.weekday() == 1 and hour == 0 and minute == 1:
            last_optimize_done = False

        # 睡眠30秒
        time.sleep(30)


if __name__ == '__main__':
    main_loop()
