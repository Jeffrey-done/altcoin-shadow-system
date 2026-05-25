"""
做空超买策略 — 从 altcoin_scanner/tracker/signal_score 重构而来。

策略逻辑:
  1. 全市场扫描日线 RSI>75 + 涨幅>10% + 量>50万U → 候选池
  2. 4h RSI 回落或弃盘点确认 → 信号评分
  3. 评分 ≥ 40 → 触发开仓
  4. 多档止盈 + 硬止损 + 移动止损 + 时间止损

注册方式:
  from strategies.short_overbought import strategy_class
  registry.register(strategy_class())

  或自动发现: registry.auto_discover()
"""

from strategies.short_overbought.strategy import ShortOverboughtStrategy

# 自动发现接口
strategy_class = ShortOverboughtStrategy
