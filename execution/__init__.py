"""
执行层 v2.0 — 统一订单执行接口

模块组成：
  - executor.py          — 统一订单执行器（并发/重试/超时）
  - smart_order.py       — 智能订单引擎 v2.0（TWAP/VWAP/Iceberg/Adaptive/Participation）
  - orderbook_monitor.py — 实时 Order Book 深度监控 v2.0（含 pre_execution_check）
  - ws_order.py          — WebSocket 低延迟下单引擎

v2.0 新增能力：
  - VWAP 真实实现（基于 intraday volume profile 加权拆单）
  - Iceberg 完整实现（限价挂单 + 暴露控制 + 自动刷新 + 超时 fallback）
  - Participation Rate（市场跟随型执行，限制自身占比 ≤ 15%）
  - Adaptive 增强（实时深度反馈 + 流动性降级时动态切换策略）
  - DepthMonitor 增强（pre_execution_check / depth_change_rate / spread_history）

用法：
  # 智能执行（自动选择最优算法）
  from execution.smart_order import get_smart_order_engine, OrderSide, AlgoType
  engine = get_smart_order_engine()
  result = engine.execute(
      symbol='PEPE/USDT',
      side=OrderSide.SELL,
      amount=500000000,
      notional_usdt=3000,
  )

  # 指定算法
  result = engine.execute(..., algo=AlgoType.VWAP)
  result = engine.execute(..., algo=AlgoType.ICEBERG)
  result = engine.execute(..., algo=AlgoType.PARTICIPATION)

  # 开仓前深度检查
  from execution.orderbook_monitor import get_depth_monitor
  monitor = get_depth_monitor()
  check = monitor.pre_execution_check('PEPE/USDT', side='sell', notional_usdt=3000)
  if check['ok']:
      engine.execute(..., algo=AlgoType[check['recommended_algo'].upper()])
"""
