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


def run_task(name: str, func):
    """安全执行任务，捕获异常"""
    try:
        logger.info(f"[{name}] 开始执行")
        func()
        logger.info(f"[{name}] 完成")
    except Exception as e:
        logger.error(f"[{name}] 异常: {e}\n{traceback.format_exc()}")


def get_utc_hour():
    return datetime.now(timezone.utc).hour


def get_utc_minute():
    return datetime.now(timezone.utc).minute


def main_loop():
    """主调度循环，每分钟检查一次"""
    logger.info("=== 调度器启动 ===")

    last_scan_hour = -1
    last_check_min = -1
    last_tracker_min = -1
    last_health_hour = -1
    last_daily_report_done = False

    while True:
        now = datetime.now(timezone.utc)
        hour = now.hour
        minute = now.minute
        day = now.day

        # ── 每4小时：日线扫描（0, 4, 8, 12, 16, 20）──
        if hour % 4 == 0 and minute == 0 and hour != last_scan_hour:
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

        # 日期变更重置
        if hour == 0 and minute == 1:
            last_daily_report_done = False

        # 睡眠30秒
        time.sleep(30)


if __name__ == '__main__':
    main_loop()
