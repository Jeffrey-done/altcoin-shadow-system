#!/usr/bin/env python3
"""
宏观信号过滤器
替代原有的简单 BTC 趋势过滤（signal_score.check_btc_filter），
提供更全面的市场环境判断。

被 engine_adapter.run_confirm() 在信号确认前调用：
  1. 判断当前宏观环境是否允许做空
  2. 调节信号评分（加/减分）
  3. 调节仓位大小（乘数）

向后兼容：
  - 如果 MS/CMM 数据全部不可用，自动 fallback 到原有 BTC 过滤逻辑
  - 可通过 config.MACRO_FILTER_ENABLED = False 完全关闭
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from macro.collector import MacroCollector, MacroCollectorConfig, MacroSignal

logger = logging.getLogger("macro.filter")

# 全局单例（懒加载）
_collector: Optional[MacroCollector] = None


def _get_collector() -> MacroCollector:
    """获取全局 MacroCollector 单例"""
    global _collector
    if _collector is None:
        # 尝试从 config 读取路径配置
        config_obj = MacroCollectorConfig()
        try:
            import config as cfg
            ms_path = getattr(cfg, 'MS_SIGNAL_PATH', '')
            cmm_path = getattr(cfg, 'CMM_OUTPUT_PATH', '')
            cmm_enabled = getattr(cfg, 'CMM_ENABLED', True)
            if ms_path:
                config_obj.ms_signal_path = ms_path
            if cmm_path:
                config_obj.cmm_output_path = cmm_path
            config_obj.cmm_enabled = cmm_enabled
        except Exception:
            pass
        _collector = MacroCollector(config_obj)
    return _collector


@dataclass
class MacroFilterResult:
    """宏观过滤结果"""
    allowed: bool = True                # 是否允许做空
    stake_multiplier: float = 1.0       # 仓位调节系数
    score_bonus: int = 0                # 信号评分加减分
    environment: str = 'neutral'        # 当前市场环境
    reason: str = ''                    # 判断原因
    data_quality: str = 'unavailable'   # 数据质量

    def __bool__(self):
        return self.allowed


def check_macro_filter() -> MacroFilterResult:
    """
    执行宏观过滤检查。

    返回:
      MacroFilterResult:
        - allowed=True: 可以做空
        - allowed=False: 暂停做空
        - stake_multiplier: 仓位调节（0.5=减半, 1.0=正常, 1.2=增强）
        - score_bonus: 信号评分加减分

    用法:
      result = check_macro_filter()
      if not result.allowed:
          logger.warning(f"宏观过滤暂停做空: {result.reason}")
          return
      signal.stake *= result.stake_multiplier
      signal.score += result.score_bonus
    """
    # 检查是否启用
    try:
        import config as cfg
        if not getattr(cfg, 'MACRO_FILTER_ENABLED', True):
            return MacroFilterResult(
                allowed=True,
                reason="宏观过滤已关闭",
                data_quality='disabled',
            )
    except Exception:
        pass

    # 采集宏观信号
    try:
        collector = _get_collector()
        macro = collector.collect()
    except Exception as e:
        logger.warning(f"宏观信号采集异常，fallback 到旧逻辑: {e}")
        return _fallback_btc_filter()

    # 数据完全不可用时 fallback
    if macro.data_quality == 'unavailable':
        logger.debug("宏观数据不可用，fallback 到 BTC 过滤")
        return _fallback_btc_filter()

    # 构建结果
    result = MacroFilterResult(
        allowed=macro.short_friendly,
        stake_multiplier=macro.stake_multiplier,
        score_bonus=macro.score_bonus,
        environment=macro.environment,
        reason=macro.reason,
        data_quality=macro.data_quality,
    )

    # 日志
    if not result.allowed:
        logger.warning(f"🚫 宏观过滤暂停做空: {result.reason}")
    elif result.stake_multiplier != 1.0 or result.score_bonus != 0:
        logger.info(
            f"📊 宏观调节: env={result.environment} "
            f"stake×{result.stake_multiplier:.1f} "
            f"bonus={result.score_bonus:+d} | {result.reason}"
        )

    return result


def get_macro_summary() -> str:
    """
    获取宏观信号摘要（用于 TG 推送 / dashboard 展示）。
    """
    try:
        collector = _get_collector()
        macro = collector.collect()
    except Exception:
        return "宏观信号: 不可用"

    ms = macro.ms
    cmm = macro.cmm

    lines = [f"📡 宏观环境: {macro.environment.upper()}"]

    if not ms.is_stale:
        lines.append(
            f"  MS: score={ms.score} signal={ms.signal} "
            f"(smart={ms.smart_money_score} tech={ms.technical_score})"
        )
    else:
        lines.append("  MS: 数据过期")

    if not cmm.is_stale:
        lines.append(
            f"  CMM: {cmm.action} conf={cmm.confidence:.0f}% risk={cmm.risk_level}/5"
        )

    lines.append(f"  做空: {'✅允许' if macro.short_friendly else '🚫暂停'}")
    if macro.stake_multiplier != 1.0:
        lines.append(f"  仓位: ×{macro.stake_multiplier:.1f}")
    if macro.score_bonus != 0:
        lines.append(f"  评分: {macro.score_bonus:+d}")

    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════════════
#  Fallback: 原有 BTC 趋势过滤
# ══════════════════════════════════════════════════════════════════

def _fallback_btc_filter() -> MacroFilterResult:
    """
    当 MS/CMM 不可用时，退回到原有的 BTC 24h 涨跌幅过滤。
    逻辑与原 signal_score.check_btc_filter() 完全一致。
    """
    try:
        import config as cfg
        if not getattr(cfg, 'BTC_FILTER_ENABLED', True):
            return MacroFilterResult(allowed=True, reason="BTC过滤已关闭(fallback)")

        from exchange_manager import get_btc_24h_change_multi
        btc_pct = get_btc_24h_change_multi()

        threshold = getattr(cfg, 'BTC_CRASH_THRESHOLD', -5.0)
        if btc_pct <= threshold:
            return MacroFilterResult(
                allowed=False,
                reason=f"BTC 24h={btc_pct:.1f}% <= {threshold}%（暴跌暂停做空）",
                environment='extreme_bullish_rebound',
                data_quality='fallback',
            )

        # BTC 大涨时做空环境好（山寨冲高回落）
        pump_threshold = getattr(cfg, 'BTC_PUMP_THRESHOLD', 8.0)
        bonus = 0
        if btc_pct >= pump_threshold:
            bonus = 5

        return MacroFilterResult(
            allowed=True,
            score_bonus=bonus,
            reason=f"BTC 24h={btc_pct:+.1f}%(fallback)",
            data_quality='fallback',
        )

    except Exception as e:
        logger.debug(f"BTC fallback 也失败: {e}")
        return MacroFilterResult(allowed=True, reason="所有过滤不可用", data_quality='unavailable')
