#!/usr/bin/env python3
"""
ML A/B 影子测试框架 v1.0

在实盘中同时运行 ML 评分和线性评分，但只按线性结果开仓。
ML 结果仅记录到 DB/日志，用于积累验证数据。

目的：
  - 验证 ML 模型在实盘环境中的预测准确率
  - 积累足够样本后（50+ 笔）才切换到 ML 主导
  - 零风险验证（不影响实际交易决策）

流程：
  1. 每次信号确认时，同时计算 linear_score 和 ml_score
  2. 交易决策仍按 linear_score
  3. 记录两个评分 + 最终交易结果（盈/亏）
  4. 定期生成对比报告：ML 是否比 linear 准确

用法：
  from ml.ab_test import ABTestManager
  ab = ABTestManager()

  # 在信号确认时
  ab.record_prediction(
      symbol='PEPE/USDT',
      linear_score=72, linear_grade='A',
      ml_score=81, ml_grade='A', ml_probability=0.78,
      actual_decision='open',  # 实际按 linear 开仓了
  )

  # 交易平仓后
  ab.record_outcome(trade_id='xxx', pnl=12.5)

  # 生成报告
  report = ab.generate_report()
  print(report)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ml.ab_test")

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AB_TEST_LOG_FILE = os.path.join(_SCRIPT_DIR, 'ml_ab_test_log.json')


@dataclass
class ABTestRecord:
    """单条 A/B 测试记录"""
    timestamp: str = ''
    symbol: str = ''
    trade_id: str = ''

    # Linear 评分
    linear_score: int = 0
    linear_grade: str = ''         # A / B / SKIP
    linear_decision: str = ''      # 'open' / 'skip'

    # ML 评分
    ml_score: int = 0
    ml_grade: str = ''
    ml_probability: float = 0.0
    ml_decision: str = ''          # 'open' / 'skip' (假设用 ML 会怎么决策)

    # 实际结果（平仓后填入）
    actual_pnl: Optional[float] = None       # USDT
    actual_profitable: Optional[bool] = None

    # 分析
    linear_correct: Optional[bool] = None    # linear 的决策是否正确
    ml_correct: Optional[bool] = None        # ML 的决策是否正确
    ml_would_have_been_better: Optional[bool] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ABTestReport:
    """A/B 测试报告"""
    total_samples: int = 0
    samples_with_outcome: int = 0

    # Linear 统计
    linear_opens: int = 0
    linear_skips: int = 0
    linear_accuracy: float = 0.0       # 开仓决策正确率
    linear_avg_score_winners: float = 0.0
    linear_avg_score_losers: float = 0.0

    # ML 统计
    ml_opens: int = 0
    ml_skips: int = 0
    ml_accuracy: float = 0.0
    ml_avg_probability_winners: float = 0.0
    ml_avg_probability_losers: float = 0.0

    # 对比
    ml_better_count: int = 0           # ML 比 linear 更准确的次数
    linear_better_count: int = 0
    tie_count: int = 0
    ml_improvement_pct: float = 0.0    # ML 准确率 - Linear 准确率

    # 建议
    recommendation: str = ''           # 'switch_to_ml' / 'keep_linear' / 'need_more_data'
    min_samples_for_switch: int = 50

    def summary(self) -> str:
        lines = [
            "═" * 50,
            "  ML A/B 影子测试报告",
            "═" * 50,
            f"  总样本: {self.total_samples} | 有结果: {self.samples_with_outcome}",
            "",
            "── Linear 评分 ──",
            f"  开仓: {self.linear_opens} | 跳过: {self.linear_skips}",
            f"  准确率: {self.linear_accuracy:.1%}",
            "",
            "── ML 评分 ──",
            f"  (假设)开仓: {self.ml_opens} | 跳过: {self.ml_skips}",
            f"  准确率: {self.ml_accuracy:.1%}",
            f"  平均 P(profit) 盈利笔: {self.ml_avg_probability_winners:.1%}",
            f"  平均 P(profit) 亏损笔: {self.ml_avg_probability_losers:.1%}",
            "",
            "── 对比 ──",
            f"  ML 更优: {self.ml_better_count} 次",
            f"  Linear 更优: {self.linear_better_count} 次",
            f"  持平: {self.tie_count} 次",
            f"  ML 准确率提升: {self.ml_improvement_pct:+.1%}",
            "",
            f"  建议: {self.recommendation}",
            "═" * 50,
        ]
        return "\n".join(lines)


class ABTestManager:
    """
    ML A/B 测试管理器。
    """

    def __init__(self, log_file: str = ''):
        self._log_file = log_file or AB_TEST_LOG_FILE
        self._records: List[ABTestRecord] = []
        self._load()

    def record_prediction(
        self,
        symbol: str,
        linear_score: int,
        linear_grade: str,
        ml_score: int = 0,
        ml_grade: str = '',
        ml_probability: float = 0.0,
        actual_decision: str = 'open',
        trade_id: str = '',
    ):
        """记录一次信号评分对比"""
        record = ABTestRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            trade_id=trade_id,
            linear_score=linear_score,
            linear_grade=linear_grade,
            linear_decision=actual_decision,
            ml_score=ml_score,
            ml_grade=ml_grade,
            ml_probability=ml_probability,
            ml_decision='open' if ml_grade in ('A', 'B') else 'skip',
        )
        self._records.append(record)
        self._save()

        logger.info(
            f"📊 A/B记录: {symbol} | "
            f"Linear={linear_score}[{linear_grade}] | "
            f"ML={ml_score}[{ml_grade}] P={ml_probability:.1%} | "
            f"决策={actual_decision}"
        )

    def record_outcome(self, trade_id: str, pnl: float):
        """记录交易结果（平仓后调用）"""
        for record in reversed(self._records):
            if record.trade_id == trade_id and record.actual_pnl is None:
                record.actual_pnl = pnl
                record.actual_profitable = pnl > 0

                # 判断谁的决策更正确
                # Linear 开仓了：盈利=正确，亏损=错误
                if record.linear_decision == 'open':
                    record.linear_correct = pnl > 0
                else:
                    # Linear 跳过了：如果本来能盈利=错误，亏损=正确
                    record.linear_correct = pnl <= 0

                if record.ml_decision == 'open':
                    record.ml_correct = pnl > 0
                else:
                    record.ml_correct = pnl <= 0

                record.ml_would_have_been_better = (
                    record.ml_correct and not record.linear_correct
                )
                break

        self._save()

    def generate_report(self) -> ABTestReport:
        """生成 A/B 测试报告"""
        report = ABTestReport()
        report.total_samples = len(self._records)

        with_outcome = [r for r in self._records if r.actual_pnl is not None]
        report.samples_with_outcome = len(with_outcome)

        if not with_outcome:
            report.recommendation = 'need_more_data'
            return report

        # Linear 统计
        report.linear_opens = sum(1 for r in self._records if r.linear_decision == 'open')
        report.linear_skips = sum(1 for r in self._records if r.linear_decision == 'skip')

        linear_correct = [r for r in with_outcome if r.linear_correct]
        report.linear_accuracy = len(linear_correct) / len(with_outcome) if with_outcome else 0

        # ML 统计
        report.ml_opens = sum(1 for r in self._records if r.ml_decision == 'open')
        report.ml_skips = sum(1 for r in self._records if r.ml_decision == 'skip')

        ml_correct = [r for r in with_outcome if r.ml_correct]
        report.ml_accuracy = len(ml_correct) / len(with_outcome) if with_outcome else 0

        # ML probability 统计
        winners = [r for r in with_outcome if r.actual_profitable]
        losers = [r for r in with_outcome if not r.actual_profitable]
        if winners:
            report.ml_avg_probability_winners = sum(r.ml_probability for r in winners) / len(winners)
        if losers:
            report.ml_avg_probability_losers = sum(r.ml_probability for r in losers) / len(losers)

        # Linear 分数统计
        linear_open_outcomes = [r for r in with_outcome if r.linear_decision == 'open']
        lin_winners = [r for r in linear_open_outcomes if r.actual_profitable]
        lin_losers = [r for r in linear_open_outcomes if not r.actual_profitable]
        if lin_winners:
            report.linear_avg_score_winners = sum(r.linear_score for r in lin_winners) / len(lin_winners)
        if lin_losers:
            report.linear_avg_score_losers = sum(r.linear_score for r in lin_losers) / len(lin_losers)

        # 对比
        for r in with_outcome:
            if r.ml_correct and not r.linear_correct:
                report.ml_better_count += 1
            elif r.linear_correct and not r.ml_correct:
                report.linear_better_count += 1
            else:
                report.tie_count += 1

        report.ml_improvement_pct = report.ml_accuracy - report.linear_accuracy

        # 建议
        if report.samples_with_outcome < report.min_samples_for_switch:
            report.recommendation = f'need_more_data (需要 {report.min_samples_for_switch - report.samples_with_outcome} 笔)'
        elif report.ml_improvement_pct >= 0.05:  # ML 准确率高 5%+
            report.recommendation = 'switch_to_ml ✅ (ML 显著优于 Linear)'
        elif report.ml_improvement_pct >= 0:
            report.recommendation = 'keep_linear (ML 略好但不显著，继续观察)'
        else:
            report.recommendation = 'keep_linear (ML 暂不如 Linear)'

        return report

    def _load(self):
        """加载历史记录"""
        if os.path.exists(self._log_file):
            try:
                with open(self._log_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._records = [ABTestRecord(**r) for r in data]
            except Exception as e:
                logger.warning(f"A/B 日志加载失败: {e}")
                self._records = []

    def _save(self):
        """保存记录"""
        try:
            with open(self._log_file, 'w', encoding='utf-8') as f:
                json.dump([r.to_dict() for r in self._records], f,
                         ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"A/B 日志保存失败: {e}")


# ══════════════════════════════════════════════════════════════════
#  全局单例
# ══════════════════════════════════════════════════════════════════

_manager: Optional[ABTestManager] = None


def get_ab_manager() -> ABTestManager:
    """获取 A/B 测试管理器单例"""
    global _manager
    if _manager is None:
        _manager = ABTestManager()
    return _manager
