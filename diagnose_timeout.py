#!/usr/bin/env python3
"""
任务超时诊断 — 读 task_metrics.jsonl,按时间排序展示每次 scheduler 任务执行,
标红超时/失败,辅助定位卡死的任务和阶段。

CLI 用法:
    python3 diagnose_timeout.py                  # 默认显示最近 30 条
    python3 diagnose_timeout.py --limit 100      # 显示更多
    python3 diagnose_timeout.py --name 候选确认   # 只看某个任务
    python3 diagnose_timeout.py --json           # 机器可解析输出
    python3 diagnose_timeout.py --no-color       # 无 ANSI

后台 UI: 在 admin panel "任务监控" tab 查看同样数据(调 /api/task-metrics)。

数据来源: scheduler.run_task 每次任务结束(成功/超时/异常/被强杀)后写入
``task_metrics.jsonl``。新部署还没产生事件时这里会显示 "暂无数据"。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import List

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import task_metrics


# ANSI
_C = {
    "reset": "\033[0m", "bold": "\033[1m",
    "green": "\033[92m", "red": "\033[91m",
    "yellow": "\033[93m", "cyan": "\033[96m",
    "gray": "\033[90m", "blue": "\033[94m",
}

_STATUS_GLYPH = {'ok': '✓', 'error': '✗', 'timeout': '⏰', 'killed': '☠'}
_STATUS_GLYPH_ASCII = {'ok': 'OK', 'error': 'ER', 'timeout': 'TO', 'killed': 'KL'}
_STATUS_COLOR = {'ok': 'green', 'error': 'red', 'timeout': 'yellow', 'killed': 'red'}


def _glyphs_for_stdout():
    """挑 unicode 还是 ascii glyph —— 取决于 stdout encoding 能不能表达"""
    enc = (getattr(sys.stdout, 'encoding', None) or 'ascii').lower()
    try:
        '✓⏰☠'.encode(enc)
        return _STATUS_GLYPH
    except (UnicodeEncodeError, LookupError):
        return _STATUS_GLYPH_ASCII


def _color(text: str, color: str, use_color: bool) -> str:
    if not use_color or color not in _C:
        return text
    return f"{_C[color]}{text}{_C['reset']}"


def _fmt_ts(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return "?"


def _fmt_duration(sec) -> str:
    if sec is None:
        return "—"
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return "?"
    if sec < 60:
        return f"{sec:.1f}s"
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m}m{s:04.1f}s"


def render_timeline(events: List[dict], use_color: bool = True) -> str:
    if not events:
        return _color(
            "暂无任务执行事件 — 等 scheduler 跑过几轮后再回来,"
            "或确认 task_metrics.jsonl 文件路径", "gray", use_color
        )

    glyphs = _glyphs_for_stdout()
    lines: List[str] = []
    lines.append(_color("═" * 88, "cyan", use_color))
    lines.append(_color(
        f"{'时间(UTC)':<20} {'任务':<12} {'状态':<8} "
        f"{'耗时':<10} {'模式':<8} {'退出码':<8} {'诊断':<20}",
        "bold", use_color))
    lines.append(_color("═" * 88, "cyan", use_color))

    # 倒序展示(最新在最上)
    for ev in reversed(events):
        status = ev.get('status', '?')
        glyph = glyphs.get(status, '?')
        color = _STATUS_COLOR.get(status, 'gray')

        ts = _fmt_ts(ev.get('ts', 0))
        name = (ev.get('name') or '?')[:11]
        status_cell = f"{glyph} {status}"
        duration = _fmt_duration(ev.get('duration_sec'))
        mode = ev.get('mode') or '?'
        exitcode = ev.get('exitcode')
        exit_cell = '—' if exitcode is None else str(exitcode)

        # 诊断信息
        diag_parts = []
        timeout_sec = ev.get('timeout_sec', 0) or 0
        d = ev.get('duration_sec')
        if isinstance(d, (int, float)) and timeout_sec and d >= timeout_sec * 0.9:
            diag_parts.append(f"{int(d/timeout_sec*100)}% 接近超时")
        spawn_dt = ev.get('spawn_dt')
        if isinstance(spawn_dt, (int, float)) and spawn_dt > 5:
            diag_parts.append(f"spawn 慢 {spawn_dt:.1f}s")
        err = ev.get('error')
        if err:
            diag_parts.append(str(err)[:30])
        diag = " | ".join(diag_parts) if diag_parts else ""

        row = (
            f"{ts:<20} {name:<12} {status_cell:<8} "
            f"{duration:<10} {mode:<8} {exit_cell:<8} {diag:<20}"
        )
        lines.append(_color(row, color, use_color))

    lines.append(_color("═" * 88, "cyan", use_color))
    return "\n".join(lines)


def render_summary(events: List[dict], use_color: bool = True) -> str:
    """按任务名聚合的汇总块"""
    if not events:
        return ""
    by_name: dict = {}
    for ev in events:
        name = ev.get('name', '?')
        b = by_name.setdefault(name, {
            'total': 0, 'ok': 0, 'error': 0, 'timeout': 0, 'killed': 0,
            'durations': [],
        })
        b['total'] += 1
        st = ev.get('status', 'error')
        if st in b:
            b[st] += 1
        d = ev.get('duration_sec')
        if isinstance(d, (int, float)):
            b['durations'].append(float(d))

    lines = ["", _color("【按任务聚合】", "bold", use_color)]
    header = (
        f"{'任务':<14} {'总数':>4} {'成功':>4} {'失败':>4} "
        f"{'超时':>4} {'强杀':>4} {'平均耗时':>10} {'最大耗时':>10}"
    )
    lines.append(_color(header, "bold", use_color))
    lines.append(_color("─" * len(header), "gray", use_color))
    for name, b in sorted(by_name.items()):
        ds = b['durations']
        avg = sum(ds) / len(ds) if ds else 0
        mx = max(ds) if ds else 0
        bad = b['timeout'] + b['killed'] + b['error']
        color = "red" if bad else ("yellow" if avg > 60 else "green")
        row = (
            f"{name[:13]:<14} {b['total']:>4} {b['ok']:>4} {b['error']:>4} "
            f"{b['timeout']:>4} {b['killed']:>4} "
            f"{_fmt_duration(avg):>10} {_fmt_duration(mx):>10}"
        )
        lines.append(_color(row, color, use_color))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="diagnose_timeout.py",
        description="任务超时诊断 — 读 task_metrics.jsonl 渲染时间线",
    )
    parser.add_argument("--limit", type=int, default=30,
                        help="最多显示几条事件,默认 30")
    parser.add_argument("--name", type=str, default=None,
                        help="只看某个任务名,如 '候选确认'")
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON 而非彩色表格")
    parser.add_argument("--no-color", action="store_true",
                        help="禁用 ANSI 颜色")
    parser.add_argument("--no-summary", action="store_true",
                        help="不显示按任务聚合的汇总块")
    args = parser.parse_args(argv)

    events = task_metrics.read_recent(limit=args.limit, name=args.name)

    if args.json:
        payload = {
            "events": events,
            "summary": task_metrics.summary_by_task(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        use_color = (not args.no_color) and sys.stdout.isatty()
        print(render_timeline(events, use_color=use_color))
        if not args.no_summary:
            print(render_summary(events, use_color=use_color))

    # 有 timeout/killed 时返回非 0,方便接 cron / CI
    bad_count = sum(
        1 for e in events
        if e.get('status') in ('timeout', 'killed', 'error')
    )
    return 0 if bad_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
