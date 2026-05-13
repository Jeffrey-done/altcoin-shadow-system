#!/usr/bin/env python3
"""
定时任务调度器 — 替代 crontab
在 Docker 容器内按计划执行所有策略模块。

v4.1 改进（防止任务漏跑）：
  - 每个任务用"上次执行时间 + 间隔"判断，而不是精确到分钟的等值比较
  - 即使主循环被 GC/IO 卡住跨过了整点，下一轮仍然会补跑
  - 所有时间记录使用 UTC
"""

import time
import threading
import traceback
import multiprocessing
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from common import setup_logger

logger = setup_logger("scheduler")


def _process_target(module: str, func_name: str, args: tuple, kwargs: dict):
    """
    子进程入口：在新进程里 import 模块并执行函数。
    子进程会继承一个全新的 Python 解释器，所以 fcntl.flock 锁会被 OS 在
    进程退出时自动释放，避免线程终止时 flock 残留。
    """
    try:
        # 保证子进程也能找到工作目录的模块
        script_dir = os.path.dirname(os.path.abspath(__file__))
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        mod = __import__(module, fromlist=[func_name])
        func = getattr(mod, func_name)
        func(*args, **kwargs)
    except Exception as e:
        # 写到 stderr，父进程通过 logger 捕获不了子进程异常
        print(f"[subprocess] {module}.{func_name} failed: {e}", file=sys.stderr)
        traceback.print_exc()


def run_task(name: str, func, timeout: int = None,
             use_process: bool = False,
             process_module: Optional[str] = None,
             process_func: Optional[str] = None,
             process_args: tuple = (),
             process_kwargs: Optional[dict] = None):
    """
    安全执行任务，捕获异常，支持超时。
    超时后尝试通过 ctypes 向线程注入异常来终止它，防止僵尸线程持续占用资源。

    M10: 若 use_process=True，改用子进程运行；超时直接 process.terminate()
    让 OS 回收所有资源（包括 fcntl.flock）。因为 multiprocessing.Process 不能
    序列化闭包/lambda，所以需要传入顶层的 module/func name。
    """
    import config
    if timeout is None:
        timeout = config.TASK_TIMEOUT_SECONDS

    if use_process:
        if not process_module or not process_func:
            logger.error(f"[{name}] use_process=True 但未提供 process_module/process_func，回退线程")
            use_process = False

    if use_process:
        logger.info(f"[{name}] 开始执行（子进程模式，超时={timeout}s）")
        p = multiprocessing.Process(
            target=_process_target,
            args=(process_module, process_func, process_args, process_kwargs or {}),
            daemon=True,
        )
        p.start()
        p.join(timeout=timeout)

        if p.is_alive():
            logger.error(f"[{name}] ⚠️ 子进程超时（>{timeout}s），terminate")
            p.terminate()
            p.join(timeout=5)
            if p.is_alive():
                logger.error(f"[{name}] 子进程 terminate 失败，强制 kill")
                p.kill()
                p.join(timeout=2)
            from common import send_tg
            send_tg(f"⚠️ <b>任务超时</b>\n\n任务: {name}\n超时: {timeout}s\n子进程已强制终止，flock 已由 OS 释放")
        elif p.exitcode != 0:
            logger.error(f"[{name}] 子进程异常退出 exitcode={p.exitcode}")
        else:
            logger.info(f"[{name}] 完成")
        return

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
        logger.error(f"[{name}] ⚠️ 超时（>{timeout}s），尝试终止线程")
        # 尝试向僵尸线程注入 SystemExit 异常
        _try_kill_thread(thread)
        from common import send_tg
        send_tg(f"⚠️ <b>任务超时</b>\n\n任务: {name}\n超时: {timeout}s\n已尝试终止线程")
    elif exception[0]:
        logger.error(f"[{name}] 异常: {exception[0]}\n{traceback.format_exc()}")
    else:
        logger.info(f"[{name}] 完成")


def _try_kill_thread(thread: threading.Thread) -> bool:
    """
    尝试通过 ctypes 向目标线程注入 SystemExit 异常。
    这不是100%可靠的（如果线程阻塞在 C 扩展中不会生效），但覆盖了大多数 Python 代码场景。
    返回 True 表示成功发送，False 表示失败。
    """
    import ctypes
    if not thread.is_alive():
        return True
    tid = thread.ident
    if tid is None:
        return False
    try:
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(tid),
            ctypes.py_object(SystemExit)
        )
        if res == 0:
            logger.warning(f"线程 {tid} 已不存在，无需终止")
            return False
        elif res == 1:
            logger.info(f"已向线程 {tid} 注入 SystemExit")
            return True
        else:
            # res > 1 表示出错，需要清理
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(tid), None
            )
            logger.error(f"线程 {tid} 终止失败（异常状态）")
            return False
    except Exception as e:
        logger.error(f"终止线程异常: {e}")
        return False


# ══════════════════════════════════════════════════════════════════
#  任务调度状态（记录每个任务的上次执行时间）
# ══════════════════════════════════════════════════════════════════

_last_run: dict = {}  # task_name -> datetime (UTC)


def _due_for_hourly(name: str, now: datetime, minute_offset: int) -> bool:
    """
    判断一个"每小时第 N 分执行"的任务是否该跑。
    只要当前时间 >= 本小时的目标时刻 且 本小时内还没跑过，就返回 True。
    """
    target = now.replace(minute=minute_offset, second=0, microsecond=0)
    last = _last_run.get(name)
    if now < target:
        return False
    # 本小时还没跑过（last 要么没有，要么是上一小时或更早）
    return last is None or last < target


def _due_for_interval(name: str, now: datetime, interval_hours: int, minute_offset: int) -> bool:
    """
    判断"每 N 小时（在 hour % N == 0 那一小时的第 minute_offset 分）执行"的任务是否该跑。
    """
    if now.hour % interval_hours != 0:
        return False
    target = now.replace(minute=minute_offset, second=0, microsecond=0)
    last = _last_run.get(name)
    if now < target:
        return False
    return last is None or last < target


def _due_for_daily(name: str, now: datetime, hour: int, minute: int) -> bool:
    """判断"每日 HH:MM UTC"任务是否该跑"""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    last = _last_run.get(name)
    if now < target:
        return False
    return last is None or last < target


def _due_for_weekly(name: str, now: datetime, weekday: int, hour: int, minute: int) -> bool:
    """判断"每周 weekday HH:MM UTC"任务是否该跑（weekday: 0=周一）"""
    if now.weekday() != weekday:
        return False
    return _due_for_daily(name, now, hour, minute)


def _mark_done(name: str, now: datetime):
    """标记任务已执行"""
    _last_run[name] = now


def main_loop():
    """主调度循环，每分钟检查一次；任务用'上次执行+间隔'判断，避免漏跑。"""
    logger.info("=== 调度器启动 v4.1 ===")

    # 启动时先做 in-flight journal 恢复：反查交易所 pending clOrdId，
    # 发现"交易所已成交但 trades.json 没记录"的幽灵订单立即告警。
    try:
        from journal_recovery import recover_inflight
        stats = recover_inflight()
        if stats.get('ghost', 0) > 0:
            logger.critical(
                f"🚨 启动发现 {stats['ghost']} 个幽灵订单，需人工处理（详见 TG）"
            )
    except Exception as e:
        logger.error(f"journal 恢复异常（非致命，跳过）: {e}")

    # 启动时对账：遍历所有交易账户，修正风控状态与交易记录的漂移（幽灵亏损预防）
    try:
        from risk_control import reconcile_risk_state
        from common import get_all_trading_account_ids, get_current_account_id        # 收集需要对账的账户：活跃账户 + 所有配置了凭证的交易账户
        account_ids_to_reconcile = set()
        active_id = get_current_account_id()
        if active_id:
            account_ids_to_reconcile.add(active_id)
        for acc_id in get_all_trading_account_ids():
            account_ids_to_reconcile.add(acc_id)

        if not account_ids_to_reconcile:
            # 单账户兼容模式：不传 account_id，对账默认账户
            diff = reconcile_risk_state(notify=True)
            if diff:
                logger.warning(f"启动对账修正了 {len(diff)} 项风控字段: {list(diff.keys())}")
            else:
                logger.info("启动对账：风控状态一致 ✅")
        else:
            total_diffs = 0
            for acc_id in account_ids_to_reconcile:
                diff = reconcile_risk_state(account_id=acc_id, notify=True)
                if diff:
                    total_diffs += len(diff)
                    logger.warning(f"启动对账 [{acc_id}] 修正了 {len(diff)} 项: {list(diff.keys())}")
            if total_diffs == 0:
                logger.info(f"启动对账：所有 {len(account_ids_to_reconcile)} 个账户风控状态一致 ✅")
            else:
                logger.warning(f"启动对账：共修正 {total_diffs} 项偏差（覆盖 {len(account_ids_to_reconcile)} 个账户）")
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

    import config

    while True:
        now = datetime.now(timezone.utc)

        # ── 热加载 runtime_config（admin panel 改了配置后 30s 内生效）──
        try:
            from runtime_config import apply_overrides as _apply_rc
            _apply_rc()
        except Exception as e:
            logger.debug(f"runtime_config 应用异常（非致命）: {e}")

        # ── 每小时 :00 日线扫描 ──
        if _due_for_hourly('scan_daily', now, 0):
            # M10: 用子进程，超时 OS 自动释放 flock
            run_task(
                "日线扫描", None,
                use_process=True,
                process_module='altcoin_scanner', process_func='scan_daily',
            )
            _mark_done('scan_daily', now)

        # ── 每小时 :15 止盈止损检查 ──
        if _due_for_hourly('tracker_check', now, 15):
            # 止盈检查不耗时，保持线程模式
            from altcoin_tracker import run as tracker_run
            run_task("止盈检查", lambda: tracker_run(check_only=True))
            _mark_done('tracker_check', now)

        # ── 每小时 :30 候选确认 ──
        if _due_for_hourly('check_candidates', now, 30):
            # M10: 候选确认可能触发多所并行开仓，长耗时任务用子进程
            run_task(
                "候选确认", None,
                use_process=True,
                process_module='altcoin_scanner', process_func='check_candidates',
            )
            _mark_done('check_candidates', now)

        # ── 每 6 小时 :45 健康检查 ──
        if _due_for_interval('health_check', now, 6, 45):
            from health_check import run_health_check
            run_task("健康检查", run_health_check)
            _mark_done('health_check', now)

        # ── 每日 08:00 UTC 日报 ──
        if _due_for_daily('daily_report', now, 8, 0):
            # M10: 日报扫描全部持仓 + TG 推送，用子进程
            run_task(
                "日报推送", None,
                use_process=True,
                process_module='altcoin_tracker', process_func='run',
                process_kwargs={'check_only': False},
            )
            _mark_done('daily_report', now)

        # ── 每日 00:01 UTC 清理过期交易 + journal ──
        if _due_for_daily('archive_trades', now, 0, 1):
            from common import cleanup_old_trades, journal_cleanup_failed
            def _daily_cleanup():
                cleanup_old_trades()
                cleared = journal_cleanup_failed(retain_hours=72)
                if cleared > 0:
                    logger.info(f"清理 journal 过期 failed 条目 {cleared} 个")
            run_task("清理过期交易", _daily_cleanup)
            _mark_done('archive_trades', now)

        # ── 每周一 09:00 UTC 自动优化建议 ──
        if _due_for_weekly('auto_optimize', now, config.AUTO_OPTIMIZE_DAY, 9, 0):
            # M10: 自动优化耗时很久，必须用子进程防止阻塞主循环
            run_task(
                "自动优化", None,
                timeout=max(config.TASK_TIMEOUT_SECONDS, 1800),  # 至少 30 分钟
                use_process=True,
                process_module='auto_optimize', process_func='run_auto_optimize',
            )
            _mark_done('auto_optimize', now)

        # 睡眠30秒
        time.sleep(30)


if __name__ == '__main__':
    main_loop()
