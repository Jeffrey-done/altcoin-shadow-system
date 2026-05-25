#!/usr/bin/env python3
"""
宏观信号过滤器 v2 — 内嵌版本
所有数据源直接在本进程内采集，无外部依赖。

被 engine_adapter.run_confirm() 在信号确认前调用：
  1. 判断当前宏观环境是否允许做空
  2. 调节信号评分（加/减分）
  3. 调节仓位大小（乘数）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from macro.collector import MacroCollector, MacroCollectorConfig, MacroSignal

logger = logging.getLogger("macro.filter")

_collector: Optional[MacroCollector] = None


def _get_collector() -> MacroCollector:
    """获取全局 MacroCollector 单例"""
    global _collector
    if _collector is None:
        _collector = MacroCollector()
    return _collector


@dataclass
class MacroFilterResult:
    """宏观过滤结果"""
    allowed: bool = True
    stake_multiplier: float = 1.0
    score_bonus: int = 0
    environment: str = 'neutral'
    reason: str = ''
    data_quality: str = 'good'

    def __bool__(self):
        return self.allowed


def check_macro_filter() -> MacroFilterResult:
    """
    执行宏观过滤检查。

    返回:
      MacroFilterResult:
        - allowed=True: 可以做空
        - allowed=False: 暂停做空
        - stake_multiplier: 仓位调节
        - score_bonus: 信号评分加减分
    """
    try:
        import config as cfg
        if not getattr(cfg, 'MACRO_FILTER_ENABLED', True):
            return MacroFilterResult(reason="宏观过滤已关闭", data_quality='disabled')
    except Exception:
        pass

    try:
        collector = _get_collector()
        macro = collector.collect()
    except Exception as e:
        logger.warning(f"宏观信号采集异常: {e}")
        return MacroFilterResult(reason=f"采集异常: {e}", data_quality='error')

    result = MacroFilterResult(
        allowed=macro.short_friendly,
        stake_multiplier=macro.stake_multiplier,
        score_bonus=macro.score_bonus,
        environment=macro.environment,
        reason=macro.reason,
        data_quality=macro.data_quality,
    )

    if not result.allowed:
        logger.warning(f"🚫 宏观过滤暂停做空: {result.reason}")

    return result


def get_macro_summary() -> str:
    """获取宏观信号摘要文本"""
    try:
        collector = _get_collector()
        macro = collector.collect()
    except Exception:
        return "宏观信号: 不可用"

    ms = macro.ms
    lines = [f"📡 宏观环境: {macro.environment.upper()}"]
    lines.append(
        f"  评分: {ms.score} ({ms.signal}) | "
        f"SM={ms.smart_money_score} Mom={ms.momentum_score} "
        f"Tech={ms.technical_score} FG={ms.fear_greed}"
    )
    lines.append(f"  做空: {'✅允许' if macro.short_friendly else '🚫暂停'}")
    if macro.stake_multiplier != 1.0:
        lines.append(f"  仓位: ×{macro.stake_multiplier:.1f}")
    if macro.score_bonus != 0:
        lines.append(f"  评分调节: {macro.score_bonus:+d}")
    return '\n'.join(lines)
