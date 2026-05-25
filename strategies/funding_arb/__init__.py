"""
Funding Rate 套利策略

利用永续合约资金费率极端偏离时的回归特性进行套利：
  - 正极端费率（多头过热）→ 做空吃费率 + 价格回落
  - 负极端费率（空头过热）→ 做多吃费率 + 价格反弹

市场中性特性：
  - 不依赖方向性判断，纯利用费率均值回归
  - 持仓周期短（8~24h），吃 1~3 次费率结算
  - 胜率极高（75%+），但单笔收益小（目标 0.5~2%）
"""

from strategies.funding_arb.strategy import FundingArbStrategy

strategy_class = FundingArbStrategy
