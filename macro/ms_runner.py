#!/usr/bin/env python3
"""
宏观数据采集运行器 v2 — 内嵌版本
直接调用 macro/sources/ 模块，不再需要外部仓库。

scheduler.py 每 2 小时调用一次 run_macro_collection()。
"""

import logging

logger = logging.getLogger("macro.ms_runner")


def run_macro_collection():
    """
    scheduler 调用的顶层接口：触发宏观数据采集。

    v2: 直接调用内嵌的 MacroCollector（它会调用 5 个 sources 模块），
    不再 subprocess bash 也不再搜索外部仓库路径。
    """
    logger.info("=== 宏观数据采集（内嵌模式）===")

    try:
        from macro.collector import MacroCollector
        collector = MacroCollector()
        collector.invalidate_cache()  # 强制刷新
        macro = collector.collect()

        logger.info(
            f"  ✅ 采集完成: score={macro.ms.score} signal={macro.ms.signal} "
            f"env={macro.environment} short_ok={macro.short_friendly}"
        )
        return {'success': True, 'score': macro.ms.score, 'environment': macro.environment}

    except Exception as e:
        logger.error(f"  ❌ 宏观采集失败: {e}")
        return {'success': False, 'error': str(e)}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    result = run_macro_collection()
    print(result)
