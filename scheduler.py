#!/usr/bin/env python3
"""
定时任务调度器 — 替代 crontab
在 Docker 容器内按计划执行所有策略模块。

v4.3 修复（彻底解决子进程 8 分钟启动延迟）：
  - multiprocessing 启动方式从 fork 改成 spawn
  - fork 在多线程父进程里不安全：hot_scanner / tg_bot 后台线程持有的
    threading.Lock / ccxt SSL 连接池 / logging._lock 会被子进程"半继承"
    （锁状态复制了，但持锁线程没复制）→ 子进程首次 import 网络模块
    或 logger.info 时永久阻塞，直到 TCP 默认重传超时才"诈尸"。
  - 实测：父进程"开始执行"日志 → 子进程内首条业务日志间隔 8 分钟。
  - spawn 子进程从零启动新解释器，不继承父进程任何线程/锁/socket 状态。
    代价：启动慢 ~500ms，对 10 分钟级别的任务完全可以接受。

v4.2 改进（提升信号响应速度）：
  - check_candidates: 每小时 → 每 15 分钟（入场延迟从最差59分降到14分）
  - tracker_check: 每小时 → 每 10 分钟（止盈止损响应加快6倍）
  - scan_daily: 保持每小时（日线 RSI 变化慢，不需要更频繁）
  - API 调用量整体不增（候选池通常 0~6 币，15分钟一次 ≈ 24次调用，远低于限制）

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


# ══════════════════════════════════════════════════════════════════
#  spawn 启动上下文（避免 fork-after-thread 死锁）
# ══════════════════════════════════════════════════════════════════
#  Linux 上 multiprocessing 默认 start_method='fork'。
#  fork 只复制调用线程，但父进程的所有锁状态（threading.Lock、
#  ccxt HTTPSConnectionPool、SSL session、logging._lock）都会被
#  原样复制到子进程，造成"锁是 LOCKED 状态但没有持锁线程"的死锁。
#
#  我们已经知道父进程启动时会创建 hot_scanner / tg_bot 后台线程，
#  所以 fork 在本项目里 100% 不安全。改用 spawn，子进程从零启动
#  全新的 Python 解释器，不继承任何父进程状态。
#
#  注意：set_start_method 全局只能调一次；用 get_context('spawn')
#  返回的上下文创建 Process 是更稳的做法（不污染全局，也不会与
#  其他可能导入的库冲突）。
# ══════════════════════════════════════════════════════════════════
_MP_CTX = multiprocessing.get_context('spawn')


def _classify_task_failure(err_text: str) -> str:
    t = (err_text or '').lower()
    retry_tokens = ('timeout', 'timed out', 'tempor', 'rate limit', 'too many requests', 'connection reset', 'network', 'dns', 'unavailable')
    return 'retryable' if any(k in t for k in retry_tokens) else 'non_retryable'


def _process_target(module: str, func_name: str, args: tuple, kwargs: dict):
    """
    子进程入口：在新进程里 import 模块并执行函数。
    spawn 模式下子进程是全新解释器，所有 import / 锁 / 连接池都从零创建，
    不会继承父进程的"半锁定"状态。

    v4.4 修复（解决"子进程 2s 跑完业务但父进程仍等 600s"）：
      根因：业务函数（altcoin_scanner.check_candidates 等）内部用了
      ThreadPoolExecutor，worker 线程默认非 daemon。即便业务调用了
      executor.shutdown(wait=False)，CPython 还是会在 _python_exit 这个
      atexit hook 里强制 join 全局 _threads_queues 表里的所有 worker。
      ccxt/requests 的 socket read 阻塞时，worker join 会一直等到 socket
      timeout（默认很长），子进程 PID 不释放 → 父进程 p.join(600) 一直
      等到超时 terminate。
      shutdown(wait=False) 治不了这个，因为它只让"调用者不等"，没把
      worker 从 _threads_queues 摘掉。

      修复：业务函数 return 后立刻 os._exit(exitcode)，跳过整个 Python
      解释器关闭流程（atexit / threading.shutdown / GC）。OS 层立刻
      回收 PID，父进程 p.join() 秒级返回。
      代价：丢失 stdio buffer 和 atexit handlers——已在 _exit 前显式
      flush stderr/stdout 并 logging.shutdown()，所以日志不会丢。
    """
    exitcode = 0
    try:
        # 保证子进程也能找到工作目录的模块
        script_dir = os.path.dirname(os.path.abspath(__file__))
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)

        # ── 诊断日志：子进程"实际开始执行"的时间戳 ──
        # 父进程会先打 "开始执行（子进程模式...）"，再 fork/spawn。
        # 这里立刻打 "[subprocess pid=X] entered"，两条日志的时间差
        # 就是 spawn + import + 锁等待的真实开销。
        # 如果这条日志距离父进程那条超过 5s，说明 spawn/import 卡了。
        import time as _t
        _t0 = _t.monotonic()
        print(
            f"[subprocess pid={os.getpid()}] entered _process_target "
            f"target={module}.{func_name}",
            file=sys.stderr, flush=True,
        )

        mod = __import__(module, fromlist=[func_name])
        _t_import = _t.monotonic() - _t0
        print(
            f"[subprocess pid={os.getpid()}] import done dt={_t_import:.3f}s "
            f"-> calling {func_name}",
            file=sys.stderr, flush=True,
        )

        func = getattr(mod, func_name)
        func(*args, **kwargs)

        _t_total = _t.monotonic() - _t0
        print(
            f"[subprocess pid={os.getpid()}] {module}.{func_name} returned "
            f"total_dt={_t_total:.3f}s -> os._exit(0)",
            file=sys.stderr, flush=True,
        )
    except Exception as e:
        # 写到 stderr，父进程通过 logger 捕获不了子进程异常
        print(f"[subprocess pid={os.getpid()}] {module}.{func_name} failed: {e}",
              file=sys.stderr, flush=True)
        traceback.print_exc()
        exitcode = 1
    finally:
        # ── 关键：跳过 Python 解释器关闭流程，OS 层立刻回收 PID ──
        # 不能用 sys.exit(): sys.exit 抛 SystemExit，仍会触发 atexit。
        # 必须 os._exit()，它直接走 _exit(2) syscall。
        try:
            sys.stderr.flush()
            sys.stdout.flush()
        except Exception:
            pass
        # logging.shutdown() 会按注册顺序 close 所有 handler（含 RotatingFileHandler 的
        # 文件刷盘）。即便业务用了 BufferedHandler 也保证日志落盘。
        try:
            import logging
            logging.shutdown()
        except Exception:
            pass
        os._exit(exitcode)


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
        logger.info(f"[{name}] 开始执行（子进程模式 spawn，超时={timeout}s）")
        _t_spawn = time.monotonic()
        p = _MP_CTX.Process(
            target=_process_target,
            args=(process_module, process_func, process_args, process_kwargs or {}),
            daemon=True,
        )
        p.start()
        _t_started = time.monotonic()
        spawn_dt = _t_started - _t_spawn
        logger.info(
            f"[{name}] spawn 完成 pid={p.pid} 启动耗时={spawn_dt:.2f}s"
        )
        p.join(timeout=timeout)

        # 任务监控（task_metrics）：所有分支统一在 finally-style block 末尾记录一次
        _metric_status = 'ok'
        _metric_error: Optional[str] = None

        if p.is_alive():
            logger.error(f"[{name}] ⚠️ 子进程超时（>{timeout}s），terminate")
            p.terminate()
            p.join(timeout=5)
            killed_force = False
            if p.is_alive():
                logger.error(f"[{name}] 子进程 terminate 失败，强制 kill")
                p.kill()
                p.join(timeout=2)
                killed_force = True
            from common import send_tg
            send_tg(f"⚠️ <b>任务超时</b>\n\n任务: {name}\n超时: {timeout}s\n子进程已强制终止，flock 已由 OS 释放")
            _metric_status = 'killed' if killed_force else 'timeout'
            _metric_error = f"超过 {timeout}s 被强杀" if killed_force else f"超过 {timeout}s"
        elif p.exitcode != 0:
            logger.error(f"[{name}] 子进程异常退出 exitcode={p.exitcode}")
            _metric_status = 'error'
            _metric_error = f"exitcode={p.exitcode}"
            logger.error(f"[{name}] failure_class={_classify_task_failure(_metric_error)}")
        else:
            logger.info(f"[{name}] 完成")

        # 写一行任务监控事件（失败不抛，不影响主流程）
        try:
            import task_metrics
            task_metrics.record({
                'name': name,
                'mode': 'process',
                'status': _metric_status,
                'duration_sec': round(time.monotonic() - _t_spawn, 2),
                'timeout_sec': timeout,
                'exitcode': p.exitcode,
                'pid': p.pid,
                'spawn_dt': round(spawn_dt, 3),
                'error': _metric_error,
            })
        except Exception as _e:
            logger.warning(f"[{name}] task_metrics.record 失败: {_e}")
        return

    exception = [None]

    def target():
        try:
            func()
        except Exception as e:
            exception[0] = e

    logger.info(f"[{name}] 开始执行（超时={timeout}s）")
    _t_thread_start = time.monotonic()
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    _metric_status = 'ok'
    _metric_error: Optional[str] = None
    if thread.is_alive():
        logger.error(f"[{name}] ⚠️ 超时（>{timeout}s），尝试终止线程")
        # 尝试向僵尸线程注入 SystemExit 异常
        _try_kill_thread(thread)
        from common import send_tg
        send_tg(f"⚠️ <b>任务超时</b>\n\n任务: {name}\n超时: {timeout}s\n已尝试终止线程")
        _metric_status = 'timeout'
        _metric_error = f"超过 {timeout}s（线程模式无法强杀）"
        logger.error(f"[{name}] failure_class={_classify_task_failure(_metric_error)}")
    elif exception[0]:
        logger.error(f"[{name}] 异常: {exception[0]}\n{traceback.format_exc()}")
        _metric_status = 'error'
        _metric_error = f"{exception[0].__class__.__name__}: {exception[0]}"
        logger.error(f"[{name}] failure_class={_classify_task_failure(_metric_error)}")
    else:
        logger.info(f"[{name}] 完成")

    # 任务监控事件
    try:
        import task_metrics
        task_metrics.record({
            'name': name,
            'mode': 'thread',
            'status': _metric_status,
            'duration_sec': round(time.monotonic() - _t_thread_start, 2),
            'timeout_sec': timeout,
            'exitcode': None,
            'pid': None,
            'spawn_dt': None,
            'error': _metric_error,
        })
    except Exception as _e:
        logger.warning(f"[{name}] task_metrics.record 失败: {_e}")


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
_health_audit_fail_streak: int = 0
_health_audit_last_alert_ts: float = 0.0


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


def _due_for_minutes(name: str, now: datetime, interval_minutes: int) -> bool:
    """
    判断"每 N 分钟"执行一次的任务是否该跑。
    基于上次执行时间 + 间隔判断，不依赖整点对齐。
    """
    last = _last_run.get(name)
    if last is None:
        return True
    elapsed = (now - last).total_seconds()
    return elapsed >= interval_minutes * 60


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
    global _health_audit_fail_streak, _health_audit_last_alert_ts
    """主调度循环，每分钟检查一次；任务用'上次执行+间隔'判断，避免漏跑。"""
    logger.info("=== 调度器启动 v4.2 ===")
    logger.info("  频率: scan_daily=1h | check_candidates=15min | tracker=10min")

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

    # M-6: 启动时校验配置一致性，对致命组合（DEFAULT_STAKE > balance）告警
    # 多账号修复：遍历所有账号分别校验，而不是只校验活跃账号；否则非活跃账号
    # 的死配置（譬如 stake > balance）切换过去前不会被发现，切换瞬间风控全拒。
    try:
        # P2-3 修复延伸（2026-05）：启动一致性检查必须先 apply_overrides 一次，
        # 否则 _position_scale_state 还是初始值（effective_balance=None），
        # proportional 模式下校验会 fallback 到 baseline 100U → 错误地报
        # COMPOUND_MAX_STAKE > 100U 警告 spam 启动日志。
        try:
            from runtime_config import apply_overrides as _early_apply
            _early_apply(force=True)
        except Exception as _ae:
            logger.debug(f"启动 apply_overrides 失败（非致命）: {_ae}")

        from runtime_config import (
            validate_cross_field_consistency, load_account_overrides,
            load_global_overrides,
        )
        from common import send_tg, tg_escape
        from admin_secrets import list_accounts

        all_errors: list = []
        all_warnings: list = []
        global_over = load_global_overrides()

        accounts = list_accounts() or []
        if not accounts:
            # 单账号兼容：没有 admin_secrets 配置时，校验当前 config
            errs, warns = validate_cross_field_consistency({})
            all_errors.extend([f"(默认) {e}" for e in errs])
            all_warnings.extend([f"(默认) {w}" for w in warns])
        else:
            for acc in accounts:
                acc_id = acc['id']
                acc_name = acc.get('name', acc_id)
                # 用该账号的 overrides 合并到 _config 当前值做模拟"如果切到这个账号"
                acc_over = load_account_overrides(acc_id) or {}
                merged = {**global_over, **acc_over}
                errs, warns = validate_cross_field_consistency(
                    merged, account_id=acc_id
                )
                all_errors.extend([f"[{acc_name}] {e}" for e in errs])
                all_warnings.extend([f"[{acc_name}] {w}" for w in warns])

        if all_errors:
            err_text = "\n".join(f"• {e}" for e in all_errors)
            logger.error(f"启动配置一致性 ERROR:\n{err_text}")
            send_tg(
                f"🚫 <b>启动配置一致性致命错误</b>\n\n"
                + "\n".join(f"• {tg_escape(e)}" for e in all_errors)
                + "\n\n⚠️ 标注的账号配置会让风控永远拒绝开仓。"
                "请立即在 admin panel 调整后系统才能正常工作。"
            )
        if all_warnings:
            warn_text = "\n".join(f"• {w}" for w in all_warnings)
            logger.warning(f"启动配置一致性 WARNING:\n{warn_text}")
            send_tg(
                f"⚠️ <b>启动配置一致性警告</b>\n\n"
                + "\n".join(f"• {tg_escape(w)}" for w in all_warnings)
                + "\n\n建议在 admin panel 调整 DEFAULT_STAKE / RISK_MAX_POSITION_PCT / "
                "ACCOUNT_BALANCE 三者关系。"
            )
    except Exception as e:
        logger.debug(f"配置一致性校验异常（非致命）: {e}")

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

        # ── 每 10 分钟止盈止损检查（加快止损响应速度）──
        if _due_for_minutes('tracker_check', now, 10):
            # 止盈检查不耗时，保持线程模式
            from altcoin_tracker import run as tracker_run
            run_task("止盈检查", lambda: tracker_run(check_only=True))
            _mark_done('tracker_check', now)

        # ── 候选确认（可配置分钟级轮询）──
        check_interval = max(1, int(getattr(config, 'CHECK_CANDIDATES_INTERVAL_MINUTES', 15)))
        if _due_for_minutes('check_candidates', now, check_interval):
            # M10: 候选确认可能触发多所并行开仓，长耗时任务用子进程
            run_task(
                "候选确认", None,
                timeout=getattr(config, 'CHECK_CANDIDATES_HARD_TIMEOUT_SEC', 120),
                use_process=True,
                process_module='altcoin_scanner', process_func='check_candidates',
            )
            _mark_done('check_candidates', now)

        # ── 每 5 分钟同步一次交易所/本地状态（防止条件单状态滞后）──
        if _due_for_minutes('exchange_reconcile', now, 5):
            def _exchange_reconcile():
                try:
                    from common import load_json, LockedJsonFile, TRADES_FILE
                    from live_executor import get_live_exchange

                    trades = load_json(TRADES_FILE, [])
                    live = [t for t in trades if t.get('status') == 'open' and t.get('exchange') == 'binance']
                    if not live:
                        return

                    # 多账号：按 account_id 分组逐个拉交易所状态
                    by_acc = {}
                    for t in live:
                        aid = t.get('account_id') or ''
                        by_acc.setdefault(aid, []).append(t)

                    exchange_state = {}  # account_id -> {'algo_ids': set, 'pos_map': dict}
                    for aid in by_acc.keys():
                        ex = get_live_exchange(aid or None)
                        if not ex:
                            continue
                        try:
                            ex.load_markets()
                            algo_all = ex.fapiPrivateGetOpenAlgoOrders({})
                            algo_ids = {str(a.get('algoId')) for a in algo_all}
                        except Exception:
                            algo_ids = set()
                        pos_map = {}
                        try:
                            poss = ex.fetch_positions()
                            for p in poss:
                                sym = p.get('symbol') or ''
                                if sym:
                                    pos_map[sym] = float(p.get('contracts') or 0)
                        except Exception:
                            pass
                        exchange_state[aid] = {'algo_ids': algo_ids, 'pos_map': pos_map}

                    with LockedJsonFile(TRADES_FILE, default=[]) as (raw, save):
                        changed = False
                        for t in raw:
                            if t.get('status') != 'open' or t.get('exchange') != 'binance':
                                continue
                            aid = t.get('account_id') or ''
                            st = exchange_state.get(aid)
                            if not st:
                                continue
                            sym = t.get('symbol')
                            if not sym:
                                continue
                            fapi = sym.replace('/USDT', 'USDT').replace('/', '')
                            contracts = st['pos_map'].get(fapi, None)
                            algo_ids = st['algo_ids']
                            # 若本地仍标记 stage1 但 TP1 algo 已不在，且持仓降为 0，清理 stage/订单标记
                            if (t.get('protect_stage') == 'stage1' and t.get('protect_tp_algo_id') and str(t.get('protect_tp_algo_id')) not in algo_ids and (contracts is not None and contracts <= 0)):
                                t['protect_tp_algo_id'] = None
                                t['protect_stop_algo_id'] = None
                                changed = True
                        if changed:
                            save(raw)
                except Exception as e:
                    logger.warning(f"交易所同步对账异常（非致命）: {e}")
            run_task("交易所对账", _exchange_reconcile)
            _mark_done('exchange_reconcile', now)

        # ── 每 15 分钟全账号审计（仅 WARN/FAIL 推送）──
        if _due_for_minutes('health_audit_all', now, 15):
            def _run_health_audit_all():
                import subprocess
                res = subprocess.run(['python3', 'health_audit.py', '--all', '--tg'], check=False)
                if res.returncode != 0:
                    raise RuntimeError(f'health_audit exited with code {res.returncode}')

            run_task("健康审计(全账号)", _run_health_audit_all)

            try:
                import task_metrics as _tm
                _evs = _tm.read_recent(limit=1, name="健康审计(全账号)")
                if _evs:
                    _st = (_evs[0].get('status') or '').lower()
                    if _st in ('error', 'timeout', 'killed'):
                        _health_audit_fail_streak += 1
                    elif _st == 'ok':
                        _health_audit_fail_streak = 0
                if _health_audit_fail_streak >= 3 and (time.time() - _health_audit_last_alert_ts) >= 1800:
                    from common import send_tg
                    send_tg(
                        f"🚨 <b>健康审计连续失败告警</b>\n\n"
                        f"任务: 健康审计(全账号)\n"
                        f"连续失败次数: {_health_audit_fail_streak}\n"
                        f"请尽快检查 scheduler / health_audit 日志"
                    )
                    _health_audit_last_alert_ts = time.time()
            except Exception as _e:
                logger.debug(f"health_audit 连续失败计数更新异常: {_e}")

            _mark_done('health_audit_all', now)
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
