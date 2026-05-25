"""
超卖做多策略 — 对冲做空策略的单边风险

核心逻辑：
  - 山寨币暴跌后（24h -15%+，日线 RSI < 25），市场恐慌性抛售结束后
    有较高概率出现技术性反弹（dead cat bounce / V 形反转）
  - 利用超卖后的均值回归做多，持仓期短（24~48h），吃反弹 5~10%

与 short_overbought 的互补关系：
  - 牛市环境：short_overbought 受损，long_oversold 因无超卖信号而不开仓
  - 熊市环境：short_overbought 盈利，long_oversold 捕捉超跌反弹
  - 震荡市：两个策略各自捕捉超买/超卖极端，净暴露接近中性
"""

from strategies.long_oversold.strategy import LongOversoldStrategy

strategy_class = LongOversoldStrategy
