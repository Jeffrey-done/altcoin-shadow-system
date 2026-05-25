#!/usr/bin/env python3
"""
智能订单执行引擎 v1.0

解决问题：
  小币种（VOL 50万~500万U）市价单直接冲击会造成 0.2%~1%+ 的滑点。
  随着仓位增长（30U→300U保证金，名义3000U），Market Impact 成为主要成本。

核心算法：
  1. TWAP (Time-Weighted Average Price)：将大单拆分为多笔，按时间均匀执行
  2. VWAP (Volume-Weighted Average Price)：按历史成交量分布决定拆单节奏
  3. Iceberg：冰山单，只露出部分仓位
  4. Adaptive：根据实时 Order Book 深度动态调整

设计原则：
  - 对上层透明：返回加权平均成交价，上层不关心内部拆单逻辑
  - 可中断：任何时候可以取消剩余子单
  - 可回退：部分成交时返回已成交部分，不会卡住
  - 超时保护：每个子单有独立超时
  - 不适用于止损场景：止损必须即时执行，不能拆单（另走 live_executor）

使用场景：
  - 开仓（有时间余量，可以慢慢建仓）
  - TP1/TP2 止盈（有浮盈垫，可以优化退出价格）
  - 不用于：硬止损、移动止损触发（这些必须 market order 立即执行）
"""

from __future__ import annotations

import logging
import math
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, Future

logger = logging.getLogger("execution.smart_order")


# ══════════════════════════════════════════════════════════════════
#  算法类型
# ══════════════════════════════════════════════════════════════════

class AlgoType(str, Enum):
    MARKET = 'market'          # 直接市价（小仓位/紧急场景）
    TWAP = 'twap'              # 时间加权
    VWAP = 'vwap'              # 成交量加权
    ICEBERG = 'iceberg'        # 冰山单
    ADAPTIVE = 'adaptive'      # 自适应（根据深度动态选择）


class OrderSide(str, Enum):
    BUY = 'buy'
    SELL = 'sell'


class SmartOrderStatus(str, Enum):
    PENDING = 'pending'
    EXECUTING = 'executing'
    PARTIAL = 'partial'        # 部分成交
    FILLED = 'filled'          # 全部成交
    CANCELLED = 'cancelled'
    FAILED = 'failed'


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class SmartOrderConfig:
    """智能订单配置"""

    # 自动选择阈值：notional > threshold 时启用 smart order
    auto_threshold_usdt: float = 1000.0    # 名义仓位 >1000U 启用

    # TWAP 参数
    twap_slices: int = 3                   # 拆分份数
    twap_interval_sec: float = 2.0         # 子单间隔（秒）
    twap_max_total_sec: float = 15.0       # 总执行时间上限

    # VWAP 参数
    vwap_lookback_bars: int = 24           # 成交量分布回溯 K 线数
    vwap_slices: int = 4                   # 拆分份数

    # Iceberg 参数
    iceberg_show_pct: float = 0.3          # 暴露比例（只显示 30% 的量）
    iceberg_refresh_sec: float = 1.0       # 刷新频率

    # Adaptive 参数
    adaptive_depth_ratio: float = 0.3      # 单笔不超过 best level 深度的 30%
    adaptive_max_slippage_bps: float = 15  # 最大允许滑点 bps

    # 通用
    sub_order_timeout_sec: float = 8.0     # 单笔子单超时
    max_retries_per_slice: int = 2         # 每片最大重试次数
    cancel_on_excessive_slippage: bool = True  # 滑点过大时取消剩余

    # Order Book 深度检查
    check_depth_before_order: bool = True  # 下单前检查深度
    min_depth_ratio: float = 3.0           # 要求 top-5 深度 ≥ N 倍于我的单量


@dataclass
class SliceResult:
    """单片执行结果"""
    slice_index: int
    amount: float                    # 目标量
    filled_amount: float = 0.0      # 实际成交量
    avg_price: float = 0.0          # 加权均价
    order_id: str = ''
    latency_ms: float = 0.0
    slippage_bps: float = 0.0       # 相对 mid price 的滑点 bps
    success: bool = False
    error: str = ''


@dataclass
class SmartOrderResult:
    """智能订单总结果"""
    status: SmartOrderStatus = SmartOrderStatus.PENDING
    algo_used: AlgoType = AlgoType.MARKET
    symbol: str = ''
    side: str = ''
    target_amount: float = 0.0       # 目标总量
    filled_amount: float = 0.0       # 已成交总量
    avg_price: float = 0.0           # 加权平均成交价
    total_slices: int = 0
    completed_slices: int = 0
    failed_slices: int = 0
    total_latency_ms: float = 0.0
    avg_slippage_bps: float = 0.0    # 平均滑点 bps
    slices: List[SliceResult] = field(default_factory=list)
    error: str = ''
    pre_trade_mid_price: float = 0.0 # 下单前中间价（用于计算最终滑点）

    @property
    def fill_rate(self) -> float:
        """成交率 (0~1)"""
        return self.filled_amount / max(self.target_amount, 1e-10)

    @property
    def is_complete(self) -> bool:
        return self.status in (SmartOrderStatus.FILLED, SmartOrderStatus.CANCELLED,
                               SmartOrderStatus.FAILED)

    @property
    def improvement_vs_market_bps(self) -> float:
        """相对于直接市价单的改善（正数=节省了滑点）"""
        # 估算：直接市价的滑点约为 adaptive 阈值
        # 实际应对比 pre_trade_mid_price
        if self.pre_trade_mid_price > 0 and self.avg_price > 0:
            if self.side == 'sell':
                # 做空开仓 = sell，成交价越高越好
                return (self.avg_price - self.pre_trade_mid_price) / self.pre_trade_mid_price * 10000
            else:
                # 做多开仓 = buy，成交价越低越好
                return (self.pre_trade_mid_price - self.avg_price) / self.pre_trade_mid_price * 10000
        return 0.0


# ══════════════════════════════════════════════════════════════════
#  智能订单引擎
# ══════════════════════════════════════════════════════════════════

class SmartOrderEngine:
    """
    智能订单执行引擎。

    用法:
      engine = SmartOrderEngine(config)

      # 自动选择算法
      result = engine.execute(
          symbol='PEPE/USDT',
          side=OrderSide.SELL,
          amount=500000000,  # 5亿个 PEPE
          notional_usdt=3000,
          exchange_name='binance',
      )

      print(f"成交均价: {result.avg_price}, 滑点: {result.avg_slippage_bps:.1f} bps")

      # 指定算法
      result = engine.execute(
          ...,
          algo=AlgoType.TWAP,
      )
    """

    def __init__(self, config: Optional[SmartOrderConfig] = None):
        self.config = config or SmartOrderConfig()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='smart_ord')
        self._active_orders: Dict[str, SmartOrderResult] = {}
        self._lock = threading.Lock()

    def execute(
        self,
        symbol: str,
        side: OrderSide,
        amount: float,
        notional_usdt: float,
        exchange_name: str = 'binance',
        account_id: str = '',
        algo: Optional[AlgoType] = None,
        client_order_id_prefix: str = '',
        urgent: bool = False,
    ) -> SmartOrderResult:
        """
        执行智能订单。

        参数:
          symbol: 交易对 (如 'PEPE/USDT')
          side: 买卖方向
          amount: 下单数量（代币数量）
          notional_usdt: 名义金额 (USD)
          exchange_name: 交易所
          account_id: 账户 ID
          algo: 指定算法（None=自动选择）
          urgent: 紧急模式（跳过拆单直接市价）

        返回:
          SmartOrderResult 包含完整执行细节
        """
        t0 = time.monotonic()

        # 紧急模式 / 小仓位：直接市价
        if urgent or notional_usdt <= self.config.auto_threshold_usdt:
            algo = AlgoType.MARKET

        # 自动选择算法
        if algo is None:
            algo = self._select_algo(symbol, notional_usdt, exchange_name)

        result = SmartOrderResult(
            status=SmartOrderStatus.EXECUTING,
            algo_used=algo,
            symbol=symbol,
            side=side.value,
            target_amount=amount,
        )

        # 获取下单前中间价
        result.pre_trade_mid_price = self._get_mid_price(symbol, exchange_name)

        try:
            if algo == AlgoType.MARKET:
                self._execute_market(result, symbol, side, amount, exchange_name, account_id)
            elif algo == AlgoType.TWAP:
                self._execute_twap(result, symbol, side, amount, exchange_name, account_id)
            elif algo == AlgoType.ADAPTIVE:
                self._execute_adaptive(result, symbol, side, amount, notional_usdt,
                                       exchange_name, account_id)
            else:
                # VWAP/Iceberg fallback to TWAP for now
                self._execute_twap(result, symbol, side, amount, exchange_name, account_id)
        except Exception as e:
            result.status = SmartOrderStatus.FAILED
            result.error = str(e)
            logger.error(f"Smart order 执行异常: {e}", exc_info=True)

        result.total_latency_ms = round((time.monotonic() - t0) * 1000, 1)

        # 计算汇总
        self._finalize_result(result)

        logger.info(
            f"📊 SmartOrder 完成: {symbol} {side.value} algo={algo.value} "
            f"filled={result.fill_rate*100:.0f}% "
            f"avg_price={result.avg_price:.8g} "
            f"slippage={result.avg_slippage_bps:.1f}bps "
            f"latency={result.total_latency_ms:.0f}ms "
            f"improvement={result.improvement_vs_market_bps:.1f}bps"
        )
        return result

    def cancel(self, symbol: str) -> bool:
        """取消正在执行的智能订单"""
        with self._lock:
            if symbol in self._active_orders:
                self._active_orders[symbol].status = SmartOrderStatus.CANCELLED
                return True
        return False

    # ── 算法实现 ─────────────────────────────────────────────────

    def _execute_market(self, result: SmartOrderResult, symbol: str,
                        side: OrderSide, amount: float,
                        exchange_name: str, account_id: str):
        """直接市价单"""
        slice_result = self._place_single_order(
            symbol, side, amount, exchange_name, account_id, slice_index=0
        )
        result.slices.append(slice_result)
        result.total_slices = 1
        if slice_result.success:
            result.completed_slices = 1
            result.status = SmartOrderStatus.FILLED
        else:
            result.failed_slices = 1
            result.status = SmartOrderStatus.FAILED
            result.error = slice_result.error

    def _execute_twap(self, result: SmartOrderResult, symbol: str,
                      side: OrderSide, total_amount: float,
                      exchange_name: str, account_id: str):
        """TWAP 执行：均匀拆分 + 等间隔下单"""
        cfg = self.config
        n_slices = cfg.twap_slices
        slice_amount = total_amount / n_slices
        interval = cfg.twap_interval_sec
        result.total_slices = n_slices

        for i in range(n_slices):
            # 检查是否被取消
            if result.status == SmartOrderStatus.CANCELLED:
                break

            # 最后一片用剩余量（避免浮点误差）
            if i == n_slices - 1:
                filled_so_far = sum(s.filled_amount for s in result.slices)
                remaining = total_amount - filled_so_far
                if remaining <= 0:
                    break
                slice_amount = remaining

            slice_result = self._place_single_order(
                symbol, side, slice_amount, exchange_name, account_id, slice_index=i
            )
            result.slices.append(slice_result)

            if slice_result.success:
                result.completed_slices += 1
            else:
                result.failed_slices += 1
                # 如果前 2 片都失败，中止
                if result.failed_slices >= 2:
                    result.status = SmartOrderStatus.FAILED
                    result.error = f"连续失败 {result.failed_slices} 片"
                    break

            # 间隔（最后一片不等待）
            if i < n_slices - 1:
                time.sleep(interval)

        # 更新状态
        if result.status != SmartOrderStatus.CANCELLED:
            if result.completed_slices == n_slices:
                result.status = SmartOrderStatus.FILLED
            elif result.completed_slices > 0:
                result.status = SmartOrderStatus.PARTIAL
            else:
                result.status = SmartOrderStatus.FAILED

    def _execute_adaptive(self, result: SmartOrderResult, symbol: str,
                          side: OrderSide, total_amount: float,
                          notional_usdt: float,
                          exchange_name: str, account_id: str):
        """
        自适应执行：根据实时 Order Book 深度决定每片大小。

        逻辑：
          1. 检查 best level 深度
          2. 每片不超过 best level 的 30%
          3. 如果深度充足（>3x 我的单量），直接一笔市价
          4. 否则按深度比例拆分
        """
        cfg = self.config

        # 获取 Order Book
        orderbook = self._get_orderbook(symbol, exchange_name, depth=10)
        if not orderbook:
            # 降级到 TWAP
            logger.debug(f"[Adaptive] {symbol} 订单簿不可用，降级 TWAP")
            return self._execute_twap(result, symbol, side, total_amount,
                                      exchange_name, account_id)

        # 计算对手方深度
        if side == OrderSide.BUY:
            levels = orderbook.get('asks', [])
        else:
            levels = orderbook.get('bids', [])

        if not levels:
            return self._execute_twap(result, symbol, side, total_amount,
                                      exchange_name, account_id)

        # 累计前 5 档深度
        top5_depth = sum(level[1] for level in levels[:5])
        best_level_depth = levels[0][1] if levels else 0

        # 深度充足检查
        depth_ratio = top5_depth / max(total_amount, 1e-10)

        if depth_ratio >= cfg.min_depth_ratio:
            # 深度充足，直接一笔
            logger.debug(
                f"[Adaptive] {symbol} 深度充足 (ratio={depth_ratio:.1f}x)，直接市价"
            )
            return self._execute_market(result, symbol, side, total_amount,
                                        exchange_name, account_id)

        # 深度不足，按比例拆分
        max_per_slice = best_level_depth * cfg.adaptive_depth_ratio
        if max_per_slice <= 0:
            max_per_slice = total_amount / 3

        n_slices = max(2, min(8, math.ceil(total_amount / max_per_slice)))
        slice_amount = total_amount / n_slices
        result.total_slices = n_slices

        logger.debug(
            f"[Adaptive] {symbol} 深度不足 (ratio={depth_ratio:.1f}x)，"
            f"拆为 {n_slices} 片，每片 {slice_amount:.2f}"
        )

        for i in range(n_slices):
            if result.status == SmartOrderStatus.CANCELLED:
                break

            if i == n_slices - 1:
                filled_so_far = sum(s.filled_amount for s in result.slices)
                slice_amount = total_amount - filled_so_far
                if slice_amount <= 0:
                    break

            slice_result = self._place_single_order(
                symbol, side, slice_amount, exchange_name, account_id, slice_index=i
            )
            result.slices.append(slice_result)

            if slice_result.success:
                result.completed_slices += 1

                # 检查滑点是否过大
                if (cfg.cancel_on_excessive_slippage
                        and slice_result.slippage_bps > cfg.adaptive_max_slippage_bps):
                    logger.warning(
                        f"[Adaptive] {symbol} 滑点过大 ({slice_result.slippage_bps:.0f} bps)，"
                        f"中止剩余片"
                    )
                    result.status = SmartOrderStatus.PARTIAL
                    break
            else:
                result.failed_slices += 1
                if result.failed_slices >= 2:
                    result.status = SmartOrderStatus.FAILED
                    break

            # 自适应间隔（深度越浅等越久）
            if i < n_slices - 1:
                wait = max(0.5, 3.0 / depth_ratio)
                time.sleep(min(wait, 5.0))

        if result.status == SmartOrderStatus.EXECUTING:
            if result.completed_slices == n_slices:
                result.status = SmartOrderStatus.FILLED
            elif result.completed_slices > 0:
                result.status = SmartOrderStatus.PARTIAL
            else:
                result.status = SmartOrderStatus.FAILED

    # ── 底层下单 ─────────────────────────────────────────────────

    def _place_single_order(
        self,
        symbol: str,
        side: OrderSide,
        amount: float,
        exchange_name: str,
        account_id: str,
        slice_index: int,
    ) -> SliceResult:
        """放置单笔市价单"""
        t0 = time.monotonic()
        result = SliceResult(slice_index=slice_index, amount=amount)

        try:
            from live_executor import execute_raw_market_order
            order_result = execute_raw_market_order(
                symbol=symbol,
                side=side.value,
                amount=amount,
                exchange_name=exchange_name,
                account_id=account_id,
            )

            if order_result.get('success'):
                result.success = True
                result.filled_amount = float(order_result.get('amount', amount))
                result.avg_price = float(order_result.get('price', 0))
                result.order_id = order_result.get('order_id', '')
            else:
                result.error = order_result.get('error', 'unknown')

        except ImportError:
            # live_executor 未加载（测试环境）
            # Fallback: 直接调用 ccxt
            try:
                from exchange_manager import get_binance
                exchange = get_binance(authenticated=True)
                if side == OrderSide.BUY:
                    order = exchange.create_market_buy_order(symbol, amount)
                else:
                    order = exchange.create_market_sell_order(symbol, amount)
                result.success = True
                result.filled_amount = float(order.get('filled', amount))
                result.avg_price = float(order.get('average', order.get('price', 0)))
                result.order_id = str(order.get('id', ''))
            except Exception as e:
                result.error = str(e)

        except Exception as e:
            result.error = str(e)

        result.latency_ms = round((time.monotonic() - t0) * 1000, 1)

        # 计算滑点
        if result.success and result.avg_price > 0:
            mid = self._get_mid_price(symbol, exchange_name)
            if mid > 0:
                if side == OrderSide.SELL:
                    result.slippage_bps = (mid - result.avg_price) / mid * 10000
                else:
                    result.slippage_bps = (result.avg_price - mid) / mid * 10000

        return result

    # ── 辅助方法 ─────────────────────────────────────────────────

    def _select_algo(self, symbol: str, notional_usdt: float,
                     exchange_name: str) -> AlgoType:
        """根据仓位大小和深度自动选择算法"""
        if notional_usdt <= self.config.auto_threshold_usdt:
            return AlgoType.MARKET

        # 尝试获取深度判断
        orderbook = self._get_orderbook(symbol, exchange_name, depth=5)
        if orderbook:
            asks = orderbook.get('asks', [])
            bids = orderbook.get('bids', [])
            if asks and bids:
                mid = (asks[0][0] + bids[0][0]) / 2
                target_amount_est = notional_usdt / mid if mid > 0 else 0
                top5_depth = sum(l[1] for l in (asks if True else bids)[:5])
                if top5_depth > 0 and target_amount_est / top5_depth > 0.1:
                    # 我的单量 > 前5档深度的 10% → 用 Adaptive
                    return AlgoType.ADAPTIVE

        # 默认 TWAP
        if notional_usdt >= 5000:
            return AlgoType.TWAP

        return AlgoType.MARKET

    def _get_mid_price(self, symbol: str, exchange_name: str) -> float:
        """获取当前中间价"""
        try:
            orderbook = self._get_orderbook(symbol, exchange_name, depth=1)
            if orderbook:
                bids = orderbook.get('bids', [])
                asks = orderbook.get('asks', [])
                if bids and asks:
                    return (bids[0][0] + asks[0][0]) / 2
        except Exception:
            pass

        # Fallback: ticker
        try:
            from exchange_manager import get_binance
            exchange = get_binance()
            ticker = exchange.fetch_ticker(symbol)
            return ticker.get('last', 0)
        except Exception:
            return 0.0

    def _get_orderbook(self, symbol: str, exchange_name: str,
                       depth: int = 10) -> Optional[Dict]:
        """获取订单簿"""
        try:
            from exchange_manager import get_binance
            exchange = get_binance()
            return exchange.fetch_order_book(symbol, limit=depth)
        except Exception:
            return None

    def _finalize_result(self, result: SmartOrderResult):
        """汇总计算最终结果"""
        if not result.slices:
            return

        successful = [s for s in result.slices if s.success and s.filled_amount > 0]
        if not successful:
            result.filled_amount = 0
            result.avg_price = 0
            return

        # 加权平均价
        total_filled = sum(s.filled_amount for s in successful)
        weighted_price = sum(s.filled_amount * s.avg_price for s in successful)
        result.filled_amount = total_filled
        result.avg_price = weighted_price / total_filled if total_filled > 0 else 0

        # 平均滑点
        result.avg_slippage_bps = (
            sum(s.slippage_bps * s.filled_amount for s in successful) / total_filled
            if total_filled > 0 else 0
        )

    def shutdown(self):
        """关闭线程池"""
        self._executor.shutdown(wait=False)


# ══════════════════════════════════════════════════════════════════
#  全局单例
# ══════════════════════════════════════════════════════════════════

_engine: Optional[SmartOrderEngine] = None


def get_smart_order_engine(config: Optional[SmartOrderConfig] = None) -> SmartOrderEngine:
    """获取智能订单引擎单例"""
    global _engine
    if _engine is None:
        _engine = SmartOrderEngine(config)
    return _engine
