#!/usr/bin/env python3
"""
结构化任务执行事件记录器

scheduler.run_task 每次任务结束(无论成功/失败/超时)写一行 JSON 到
``task_metrics.jsonl``。append-only,自带滚动上限避免无限增长。
admin panel "任务监控" tab + ``diagnose_timeout.py`` 都从这里读。

事件 schema:
    {
        "ts":           float,   # UTC 秒级时间戳（任务结束时刻）
        "name":         str,     # 任务名 e.g. "候选确认"
        "mode":         str,     # "thread" | "process"
        "status":       str,     # "ok" | "error" | "timeout" | "killed"
        "duration_sec": float,   # 任务总耗时（spawn → 结束）
        "timeout_sec":  int,     # 当时配置的超时值
        "exitcode":     int|None,# 子进程退出码（process 模式）
        "pid":          int|None,# 子进程 PID（process 模式）
        "spawn_dt":     float|None, # spawn → 子进程内 entered 的间隔
        "error":        str|None,   # 异常摘要
    }

滚动策略:
    每次写入后,若文件 > 100 KB 才检查行数;超过 1000 行时
    截断为最后 500 行。这样 99% 的写入路径不做磁盘读。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import List, Optional

logger = logging.getLogger("task_metrics")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
METRICS_FILE = os.path.join(SCRIPT_DIR, 'task_metrics.jsonl')

# 滚动参数
_TRUNCATE_KEEP = 500           # 截断时保留最后 N 行
_TRUNCATE_THRESHOLD_LINES = 1000   # 超过这个行数才 truncate
_TRUNCATE_CHECK_BYTES = 100 * 1024  # 文件 < 100KB 直接跳过 truncate 检查


# ══════════════════════════════════════════════════════════════════
#  写入
# ══════════════════════════════════════════════════════════════════

def record(event: dict, *, _path: Optional[str] = None) -> None:
    """追加一条任务事件。失败不抛(metrics 永远不能影响主流程)。

    Args:
        event: 事件 dict;``ts`` 字段如果缺失会自动补当前时间。
        _path: 测试注入路径,生产环境不要传。
    """
    path = _path or METRICS_FILE
    try:
        ev = dict(event)
        ev.setdefault('ts', time.time())
        line = json.dumps(ev, ensure_ascii=False, default=str) + '\n'

        # append 是 POSIX 上 < PIPE_BUF (4096) 的小写入是原子的;
        # 我们的事件 < 500 字节,多进程并发 append 也不会撕裂。
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception as e:
        # metrics 失败不应影响 scheduler;只记 logger
        logger.warning(f"task_metrics.record 失败: {e}")
        return

    # 周期性 truncate(成功写入后才检查,避免每次都 stat)
    try:
        if os.path.getsize(path) >= _TRUNCATE_CHECK_BYTES:
            _maybe_truncate(path)
    except OSError:
        pass


def _maybe_truncate(path: str) -> None:
    """如果文件行数超过 _TRUNCATE_THRESHOLD_LINES,保留最后 _TRUNCATE_KEEP 行"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return

    if len(lines) <= _TRUNCATE_THRESHOLD_LINES:
        return

    keep = lines[-_TRUNCATE_KEEP:]
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.writelines(keep)
        os.replace(tmp, path)
        logger.info(
            f"task_metrics 已截断: {len(lines)} → {len(keep)} 行"
        )
    except OSError as e:
        logger.warning(f"task_metrics 截断失败: {e}")
        # 清理临时文件
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════════
#  读取
# ══════════════════════════════════════════════════════════════════

def read_all(*, _path: Optional[str] = None) -> List[dict]:
    """读全部事件(按 ts 升序)。文件不存在或损坏时返回空列表。"""
    path = _path or METRICS_FILE
    if not os.path.exists(path):
        return []
    out: List[dict] = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        out.append(obj)
                except Exception:
                    # 单行损坏不影响整体
                    continue
    except OSError as e:
        logger.warning(f"task_metrics 读取失败: {e}")
        return []
    out.sort(key=lambda r: r.get('ts', 0))
    return out


def read_recent(limit: int = 100, name: Optional[str] = None,
                *, _path: Optional[str] = None) -> List[dict]:
    """读最近 N 条事件,可按 task name 过滤。

    Args:
        limit:   返回最多几条;<= 0 表示不限制。
        name:    只返回指定 task name 的事件;None 表示全部。

    Returns:
        list[dict],按 ts 升序(最旧在前,最新在后)。
    """
    events = read_all(_path=_path)
    if name:
        events = [e for e in events if e.get('name') == name]
    if limit > 0 and len(events) > limit:
        events = events[-limit:]
    return events


def summary_by_task(*, _path: Optional[str] = None) -> dict:
    """聚合每个任务名的统计:总次数 / 各状态计数 / 平均耗时 / 最近一次时间。

    返回:
        {
            "候选确认": {
                "total": 48,
                "ok": 45, "timeout": 2, "killed": 0, "error": 1,
                "avg_duration_sec": 312.4,
                "max_duration_sec": 599.2,
                "last_ts": 1737037200.5,
                "last_status": "timeout",
            },
            ...
        }
    """
    events = read_all(_path=_path)
    by_name: dict = {}
    for ev in events:
        name = ev.get('name', '<unknown>')
        bucket = by_name.setdefault(name, {
            'total': 0, 'ok': 0, 'error': 0, 'timeout': 0, 'killed': 0,
            'durations': [],
            'last_ts': 0.0, 'last_status': None,
        })
        bucket['total'] += 1
        status = ev.get('status', 'error')
        if status in ('ok', 'error', 'timeout', 'killed'):
            bucket[status] = bucket.get(status, 0) + 1
        d = ev.get('duration_sec')
        if isinstance(d, (int, float)):
            bucket['durations'].append(float(d))
        ts = ev.get('ts', 0)
        if ts > bucket['last_ts']:
            bucket['last_ts'] = ts
            bucket['last_status'] = status

    # 计算统计
    out = {}
    for name, b in by_name.items():
        ds = b.pop('durations')
        b['avg_duration_sec'] = round(sum(ds) / len(ds), 2) if ds else None
        b['max_duration_sec'] = round(max(ds), 2) if ds else None
        out[name] = b
    return out
