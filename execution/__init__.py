"""
执行层 — 统一订单执行接口

模块组成：
  - executor.py         — 统一订单执行器（并发/重试/超时）
  - smart_order.py      — 智能订单引擎（TWAP/VWAP/Adaptive 拆单）
  - orderbook_monitor.py — 实时 Order Book 深度监控

用法：
  # 普通执行
  from execution.executor import OrderExecutor
  executor = OrderExecutor()
  result = executor.execute_signal(signal)

  # 智能执行（大仓位自动拆单）
  from execution.smart_order import get_smart_order_engine, OrderSide
  engine = get_smart_order_engine()
  result = engine.execute(symbol, OrderSide.SELL, amount, notional_usdt)

  # 深度分析
  from execution.orderbook_monitor import get_depth_monitor
  monitor = get_depth_monitor()
  analysis = monitor.analyze('PEPE/USDT', side='sell', notional_usdt=3000)
"""
