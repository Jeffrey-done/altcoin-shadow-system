#!/usr/bin/env python3
"""
统一调度器入口（S2 修复 — 2026-05）

历史背景
========
在 v5.x 之前，本仓库存在三套并存的调度入口：

  1. ``async_engine.py``  —— asyncio + aiohttp 主循环（生产就绪）
  2. ``altcoin_scanner.py [scan|check|both]`` —— 旧版 cron 单步入口
  3. ``altcoin_tracker.py [--check-only]`` —— 旧版 cron 单步入口

docker-compose 已经只跑 ``async_engine.py``，但 CLI 入口残留导致：
  * 新人不知道哪个才是"主循环"
  * 容易在 cron / supervisor 里重复启动多套调度器，互相打架
  * runtime_config 热加载、宏观过滤、SAFE_MODE 等启动序列只在
    ``async_engine._run_startup_sequence()`` 里有，CLI 模式下会缺一截

S2 修复：把"调度器"这个名字唯一化
======================================

  * **本文件 ``scheduler.py``** —— 唯一推荐的调度器进程入口，
    内部 100% 委托给 ``async_engine.main()``。等价的命令是：

        python3 scheduler.py
        python3 async_engine.py        # 同义

  * **``altcoin_scanner.py / altcoin_tracker.py`` 的 ``__main__`` 块**
    保留作为运维一次性工具（debug / 手动触发 / 旧 cron 兼容），
    但启动时会打印 deprecation 提示。生产环境请使用本文件。

  * **``realtime_monitor.py``** 是独立的 WebSocket 风险闭环进程，
    职责正交（紧贴价格做止损），与本调度器解耦，**不在 S2 范围内**。

迁移指引
========
旧 cron 配置：
    */5 * * * * python3 altcoin_scanner.py check
    */1 * * * * python3 altcoin_tracker.py --check-only

新方式（推荐，docker-compose 已切换）：
    python3 scheduler.py            # 一个进程跑全部周期任务

  → 内部由 async_engine 的 TaskGroup 管理：
    scan(每小时) / confirm(可配置) / tracker(每分钟) / 对账 / 健康审计 / 日报 / 归档

详细文档见 ``docs/UNIFIED_ARCHITECTURE.md``。
"""

from __future__ import annotations

import sys


def main() -> None:
    """委托给 async_engine.main —— 这是唯一的调度器入口"""
    from async_engine import main as _async_main
    _async_main()


if __name__ == '__main__':
    sys.exit(main() or 0)
