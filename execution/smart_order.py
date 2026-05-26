#!/usr/bin/env python3
"""
智能订单执行引擎 v2.0

解决问题：
  小币种（VOL 50万~500万U）市价单直接冲击会造成 0.2%~1%+ 的滑点。
  随着仓位增长（30U→300U保证金，名义3000U），Market Impact 成为主要成本。

核心算法：
  1. TWAP (Time-Weighted Average Price)：将大单拆分为多笔，按时间均匀执行
  2. VWAP (Volume-Weighted Average Price)：按历史成交量分布决定拆单节奏（v2.0 完整实现）
  3. Iceberg：冰山单，限价挂单 + 暴露比例控制 + 自动刷新（v2.0 完整实现）
  4. Adaptive：根据实时 Order Book 深度动态切换策略（v2.0 增强）
  5. Participation：市场跟随型执行，限制自身成交占市场比例（v2.0 新增）

v2.0 新增特性：
  - VWAP 真实实现：基于历史 intraday volume profile 加权拆单
  - Iceberg 完整实现：限价挂单 + show_pct 暴露控制 + 自动刷新
  - Participation Rate：监控市场成交流速，跟随市场节奏执行
  - Adaptive 增强：集成 DepthMonitor 实时反馈，执行中动态切换策略
  - 执行中深度监控：每片下单前/后检查深度变化，流动性枯竭时暂停

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
    MARKET = 'market'              # 直接市价（小仓位/紧急场景）
    TWAP = 'twap'                  # 时间加权
    VWAP = 'vwap'                  # 成交量加权（v2.0 完整实现）
    ICEBERG = 'iceberg'            # 冰山单（v2.0 完整实现）
    ADAPTIVE = 'adaptive'          # 自适应（深度驱动动态切换）
    PARTICIPATION = 'participation'  # 市场跟随（v2.0 新增）


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

    # VWAP 参数（v2.0 完整）
    vwap_lookback_hours: int = 24          # 成交量分布回溯小时数
    vwap_slices: int = 4                   # 拆分份数
    vwap_interval_sec: float = 3.0         # 基础间隔（实际按权重动态调整）
    vwap_max_total_sec: float = 30.0       # VWAP 总执行时间上限
    vwap_min_weight: float = 0.05          # 最小权重阈值（低于此的时段合并）

    # Iceberg 参数（v2.0 完整）
    iceberg_show_pct: float = 0.3          # 暴露比例（只显示 30% 的量）
    iceberg_refresh_sec: float = 1.5       # 刷新频率
    iceberg_price_offset_bps: float = 2.0  # 限价单偏移 bps（相对 mid price）
    iceberg_max_wait_sec: float = 8.0      # 单片最大等待时间
    iceberg_fallback_to_market: bool = True  # 超时后回退市价

    # Adaptive 参数（v2.0 增强）
    adaptive_depth_ratio: float = 0.3      # 单笔不超过 best level 深度的 30%
    adaptive_max_slippage_bps: float = 15  # 最大允许滑点 bps
    adaptive_recheck_depth: bool = True    # 每片之间重新检查深度
    adaptive_pause_on_drain: bool = True   # 深度骤降时暂停执行
    adaptive_drain_threshold: float = 0.5  # 深度骤降阈值（减少 50%+）

    # Participation 参数（v2.0 新增）
    participation_rate: float = 0.15       # 目标参与率（自身成交/市场成交 ≤ 15%）
    participation_check_interval_sec: float = 2.0  # 市场流速检查间隔
    participation_max_total_sec: float = 60.0      # 最大执行时间
    participation_min_market_volume: float = 100.0  # 最小市场流速 (USDT/s)

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
    algo_phase: str = ''            # v2.0: 标记该片属于哪个算法阶段


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
    pre_trade_mid_price: float = 0.0  # 下单前中间价（用于计算最终滑点）
    # v2.0 新增
    depth_checks: int = 0             # 深度检查次数
    algo_switches: int = 0            # 算法动态切换次数
    volume_profile_used: bool = False  # 是否使用了 volume profile

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
        if self.pre_trade_mid_price > 0 and self.avg_price > 0:
            if self.side == 'sell':
                return (self.avg_price - self.pre_trade_mid_price) / self.pre_trade_mid_price * 10000
            else:
                return (self.pre_trade_mid_price - self.avg_price) / self.pre_trade_mid_price * 10000
        return 0.0



# ══════════════════════════════════════════════════════════════════
#  Volume Profile 辅助（VWAP 核心数据源）
# ══════════════════════════════════════════════════════════════════

class VolumeProfiler:
    """
    Intraday Volume Profile 分析器。

    用途：
      - 统计过去 N 天同一时段的成交量分布
      - 为 VWAP 算法提供各时段的权重
      - 识别高流动性窗口（欧美盘/亚洲盘切换时段）

    数据源：
      - Binance 1h K 线成交量（`GET /fapi/v1/klines`）
      - 回溯 7 天取均值，消除单日异常波动
    """

    def __init__(self, lookback_days: int = 7):
        self._lookback_days = lookback_days
        self._cache: Dict[str, Tuple[List[float], float]] = {}  # symbol → (hourly_weights, cache_time)
        self._cache_ttl = 3600  # 1h 缓存

    def get_hourly_weights(self, symbol: str) -> List[float]:
        """
        获取 24 小时的成交量权重分布。

        返回: 长度 24 的列表，weights[h] 表示 UTC h:00~h:59 的成交量占比。
              所有权重之和 = 1.0
        """
        # 检查缓存
        cached = self._cache.get(symbol)
        if cached and (time.time() - cached[1]) < self._cache_ttl:
            return cached[0]

        # 获取历史 1h K 线
        try:
            hourly_volumes = self._fetch_hourly_volumes(symbol)
            if hourly_volumes and len(hourly_volumes) >= 24:
                weights = self._compute_weights(hourly_volumes)
                self._cache[symbol] = (weights, time.time())
                return weights
        except Exception as e:
            logger.debug(f"VolumeProfiler 获取 {symbol} 失败: {e}")

        # Fallback：均匀分布
        return [1.0 / 24] * 24

    def get_current_weight(self, symbol: str) -> float:
        """获取当前小时的成交量权重"""
        import datetime
        weights = self.get_hourly_weights(symbol)
        current_hour = datetime.datetime.utcnow().hour
        return weights[current_hour]

    def get_slice_weights(self, symbol: str, n_slices: int,
                          duration_minutes: int = 10) -> List[float]:
        """
        为 VWAP 拆单生成各片的权重。

        基于当前时刻开始往后 duration_minutes 分钟内的预期成交量分布。
        如果跨小时边界，按比例混合两个小时的权重。

        参数:
          symbol: 交易对
          n_slices: 拆分片数
          duration_minutes: 总执行时间窗口（分钟）

        返回:
          长度为 n_slices 的权重列表，和为 1.0
        """
        import datetime
        now = datetime.datetime.utcnow()
        current_hour = now.hour
        current_minute = now.minute

        weights = self.get_hourly_weights(symbol)

        # 计算每片覆盖的时间段
        slice_duration_min = duration_minutes / n_slices
        slice_weights = []

        for i in range(n_slices):
            start_min = current_minute + i * slice_duration_min
            end_min = start_min + slice_duration_min

            # 该片覆盖的小时权重（可能跨小时）
            w = 0.0
            t = start_min
            while t < end_min:
                hour_idx = (current_hour + int(t / 60)) % 24
                # 该分钟在其小时内的占比
                minute_weight = weights[hour_idx] / 60.0
                remaining_in_step = min(end_min - t, 60 - (t % 60))
                w += minute_weight * remaining_in_step
                t += remaining_in_step

            slice_weights.append(max(w, 0.001))

        # 归一化
        total = sum(slice_weights)
        if total > 0:
            slice_weights = [w / total for w in slice_weights]
        else:
            slice_weights = [1.0 / n_slices] * n_slices

        return slice_weights

    def _fetch_hourly_volumes(self, symbol: str) -> List[float]:
        """从 Binance 获取历史逐小时成交量"""
        try:
            import requests
            sym = symbol.replace('/USDT', 'USDT').replace('/', '')
            limit = self._lookback_days * 24
            r = requests.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": sym, "interval": "1h", "limit": min(limit, 500)},
                timeout=10,
            )
            if r.status_code == 200:
                klines = r.json()
                # 提取每根 K 线的 quote volume (index 7)
                return [float(k[7]) for k in klines]
        except Exception as e:
            logger.debug(f"获取 {symbol} 历史成交量失败: {e}")
        return []

    def _compute_weights(self, hourly_volumes: List[float]) -> List[float]:
        """
        从历史成交量计算 24h 权重分布。

        对多天数据取同一时段的平均值，消除单日异常。
        """
        # 按 UTC 小时聚合（取多天同一小时的均值）
        hour_sums = [0.0] * 24
        hour_counts = [0] * 24

        for i, vol in enumerate(hourly_volumes):
            hour_idx = i % 24
            hour_sums[hour_idx] += vol
            hour_counts[hour_idx] += 1

        # 计算均值
        hour_avgs = []
        for h in range(24):
            if hour_counts[h] > 0:
                hour_avgs.append(hour_sums[h] / hour_counts[h])
            else:
                hour_avgs.append(1.0)

        # 归一化为权重
        total = sum(hour_avgs)
        if total > 0:
            return [v / total for v in hour_avgs]
        return [1.0 / 24] * 24



# ══════════════════════════════════════════════════════════════════
#  智能订单引擎 v2.0
# ══════════════════════════════════════════════════════════════════

class SmartOrderEngine:
    """
    智能订单执行引擎 v2.0。

    v2.0 新增能力:
      - VWAP 真实实现（基于 intraday volume profile）
      - Iceberg 完整实现（限价 + 自动刷新 + 超时 fallback）
      - Participation Rate（市场跟随执行）
      - Adaptive 增强（实时深度反馈 + 动态策略切换）
      - 执行中深度监控（每片间检查流动性变化）

    用法:
      engine = SmartOrderEngine(config)

      # 自动选择算法
      result = engine.execute(
          symbol='PEPE/USDT',
          side=OrderSide.SELL,
          amount=500000000,
          notional_usdt=3000,
          exchange_name='binance',
      )

      # 指定算法
      result = engine.execute(..., algo=AlgoType.VWAP)
      result = engine.execute(..., algo=AlgoType.ICEBERG)
      result = engine.execute(..., algo=AlgoType.PARTICIPATION)
    """

    def __init__(self, config: Optional[SmartOrderConfig] = None):
        self.config = config or SmartOrderConfig()
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix='smart_ord')
        self._active_orders: Dict[str, SmartOrderResult] = {}
        self._lock = threading.Lock()
        self._volume_profiler = VolumeProfiler()

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
            algo = self._select_algo(symbol, notional_usdt, exchange_name, side)

        result = SmartOrderResult(
            status=SmartOrderStatus.EXECUTING,
            algo_used=algo,
            symbol=symbol,
            side=side.value,
            target_amount=amount,
        )

        # 注册活跃订单
        with self._lock:
            self._active_orders[symbol] = result

        # 获取下单前中间价
        result.pre_trade_mid_price = self._get_mid_price(symbol, exchange_name)

        try:
            if algo == AlgoType.MARKET:
                self._execute_market(result, symbol, side, amount,
                                     exchange_name, account_id)
            elif algo == AlgoType.TWAP:
                self._execute_twap(result, symbol, side, amount,
                                   exchange_name, account_id)
            elif algo == AlgoType.VWAP:
                self._execute_vwap(result, symbol, side, amount, notional_usdt,
                                   exchange_name, account_id)
            elif algo == AlgoType.ICEBERG:
                self._execute_iceberg(result, symbol, side, amount, notional_usdt,
                                      exchange_name, account_id)
            elif algo == AlgoType.PARTICIPATION:
                self._execute_participation(result, symbol, side, amount,
                                            notional_usdt, exchange_name, account_id)
            elif algo == AlgoType.ADAPTIVE:
                self._execute_adaptive(result, symbol, side, amount, notional_usdt,
                                       exchange_name, account_id)
            else:
                self._execute_twap(result, symbol, side, amount,
                                   exchange_name, account_id)
        except Exception as e:
            result.status = SmartOrderStatus.FAILED
            result.error = str(e)
            logger.error(f"Smart order 执行异常: {e}", exc_info=True)

        result.total_latency_ms = round((time.monotonic() - t0) * 1000, 1)

        # 计算汇总
        self._finalize_result(result)

        # 清理活跃订单
        with self._lock:
            self._active_orders.pop(symbol, None)

        logger.info(
            f"📊 SmartOrder 完成: {symbol} {side.value} algo={algo.value} "
            f"filled={result.fill_rate*100:.0f}% "
            f"avg_price={result.avg_price:.8g} "
            f"slippage={result.avg_slippage_bps:.1f}bps "
            f"latency={result.total_latency_ms:.0f}ms "
            f"improvement={result.improvement_vs_market_bps:.1f}bps "
            f"depth_checks={result.depth_checks} "
            f"algo_switches={result.algo_switches}"
        )
        return result

    def cancel(self, symbol: str) -> bool:
        """取消正在执行的智能订单"""
        with self._lock:
            if symbol in self._active_orders:
                self._active_orders[symbol].status = SmartOrderStatus.CANCELLED
                return True
        return False



    # ══════════════════════════════════════════════════════════════
    #  MARKET — 直接市价单
    # ══════════════════════════════════════════════════════════════

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

    # ══════════════════════════════════════════════════════════════
    #  TWAP — 时间加权平均
    # ══════════════════════════════════════════════════════════════

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
            slice_result.algo_phase = 'twap'
            result.slices.append(slice_result)

            if slice_result.success:
                result.completed_slices += 1
            else:
                result.failed_slices += 1
                if result.failed_slices >= 2:
                    result.status = SmartOrderStatus.FAILED
                    result.error = f"连续失败 {result.failed_slices} 片"
                    break

            # 间隔（最后一片不等待）
            if i < n_slices - 1:
                time.sleep(interval)

        if result.status not in (SmartOrderStatus.CANCELLED, SmartOrderStatus.FAILED):
            if result.completed_slices == n_slices:
                result.status = SmartOrderStatus.FILLED
            elif result.completed_slices > 0:
                result.status = SmartOrderStatus.PARTIAL
            else:
                result.status = SmartOrderStatus.FAILED



    # ══════════════════════════════════════════════════════════════
    #  VWAP — 成交量加权平均（v2.0 完整实现）
    # ══════════════════════════════════════════════════════════════

    def _execute_vwap(self, result: SmartOrderResult, symbol: str,
                      side: OrderSide, total_amount: float,
                      notional_usdt: float,
                      exchange_name: str, account_id: str):
        """
        VWAP 执行：按历史成交量分布加权拆单。

        核心逻辑：
          1. 获取 intraday volume profile（24h 各时段成交量占比）
          2. 根据当前时刻 + 执行窗口，计算各片应分配的权重
          3. 权重高的时段（流动性好）多执行，权重低的时段少执行
          4. 每片间隔根据权重动态调整（高权重时段间隔短，抓紧执行）

        优势：
          - 与市场节奏同步，减少市场冲击
          - 高流动性时段集中执行，降低滑点
          - 避免在低流动性时段大量下单
        """
        cfg = self.config
        n_slices = cfg.vwap_slices

        # 获取 volume profile 权重
        slice_weights = self._volume_profiler.get_slice_weights(
            symbol, n_slices,
            duration_minutes=int(cfg.vwap_max_total_sec / 60) or 1
        )
        result.volume_profile_used = True

        # 按权重分配每片数量
        slice_amounts = [total_amount * w for w in slice_weights]

        # 合并过小的片（低于最小权重阈值的合并到相邻片）
        min_amount = total_amount * cfg.vwap_min_weight
        merged_slices = []
        carry = 0.0
        for i, amt in enumerate(slice_amounts):
            amt += carry
            carry = 0.0
            if amt < min_amount and i < len(slice_amounts) - 1:
                carry = amt  # 合并到下一片
            else:
                merged_slices.append(amt)

        slice_amounts = merged_slices
        n_slices = len(slice_amounts)
        result.total_slices = n_slices

        # 计算每片间隔（权重高的间隔短）
        # 总可用时间 = vwap_max_total_sec - 预估执行时间
        available_time = cfg.vwap_max_total_sec - n_slices * 1.0  # 每片预留 1s 执行
        intervals = []
        for i in range(n_slices - 1):
            # 间隔与下一片权重成反比（下一片权重越大，间隔越短——越早执行）
            next_weight = slice_weights[min(i + 1, len(slice_weights) - 1)]
            avg_weight = 1.0 / n_slices
            ratio = avg_weight / max(next_weight, 0.001)
            interval = cfg.vwap_interval_sec * ratio
            interval = max(1.0, min(interval, 8.0))  # 限制在 1~8s
            intervals.append(interval)

        logger.info(
            f"[VWAP] {symbol} 拆为 {n_slices} 片, "
            f"amounts={[f'{a:.2f}' for a in slice_amounts]}, "
            f"intervals={[f'{i:.1f}s' for i in intervals]}"
        )

        # 执行各片
        for i in range(n_slices):
            if result.status == SmartOrderStatus.CANCELLED:
                break

            # 最后一片用剩余量
            if i == n_slices - 1:
                filled_so_far = sum(s.filled_amount for s in result.slices)
                current_amount = max(0, total_amount - filled_so_far)
            else:
                current_amount = slice_amounts[i]

            if current_amount <= 0:
                break

            # v2.0: 每片前检查深度
            if cfg.check_depth_before_order and i > 0:
                depth_ok = self._check_depth_before_slice(
                    symbol, side, current_amount, notional_usdt / n_slices,
                    exchange_name, result
                )
                if not depth_ok:
                    logger.warning(f"[VWAP] {symbol} 第{i}片深度不足，暂停 3s")
                    time.sleep(3.0)

            slice_result = self._place_single_order(
                symbol, side, current_amount, exchange_name, account_id, slice_index=i
            )
            slice_result.algo_phase = f'vwap_slice_{i}_w{slice_weights[min(i, len(slice_weights)-1)]:.2f}'
            result.slices.append(slice_result)

            if slice_result.success:
                result.completed_slices += 1
            else:
                result.failed_slices += 1
                if result.failed_slices >= 2:
                    result.status = SmartOrderStatus.FAILED
                    result.error = f"VWAP 连续失败 {result.failed_slices} 片"
                    break

            # 动态间隔
            if i < n_slices - 1 and i < len(intervals):
                time.sleep(intervals[i])

        if result.status not in (SmartOrderStatus.CANCELLED, SmartOrderStatus.FAILED):
            if result.completed_slices >= n_slices:
                result.status = SmartOrderStatus.FILLED
            elif result.completed_slices > 0:
                result.status = SmartOrderStatus.PARTIAL
            else:
                result.status = SmartOrderStatus.FAILED



    # ══════════════════════════════════════════════════════════════
    #  ICEBERG — 冰山单（v2.0 完整实现）
    # ══════════════════════════════════════════════════════════════

    def _execute_iceberg(self, result: SmartOrderResult, symbol: str,
                         side: OrderSide, total_amount: float,
                         notional_usdt: float,
                         exchange_name: str, account_id: str):
        """
        Iceberg 冰山单执行。

        核心逻辑：
          1. 只暴露总量的 show_pct（默认 30%）作为限价单
          2. 限价单挂在 mid_price ± offset_bps 处（比市价稍优）
          3. 成交后自动刷新下一片（不等全部成交，部分成交就续挂）
          4. 单片超时未成交 → 撤单 + 降价追单 / 回退市价
          5. 全部完成或总超时后返回

        优势：
          - 对手方看不到真实仓位大小
          - 限价单可能获得 maker rebate（手续费减免）
          - 减少信息泄露（避免被量化检测到大单意图）

        适用场景：
          - 中等仓位（1000~5000U）+ 流动性尚可（VOL > 100万U）
          - 不急于立即成交（可以等几秒）
        """
        cfg = self.config
        show_amount = total_amount * cfg.iceberg_show_pct
        remaining = total_amount
        slice_index = 0
        total_start = time.monotonic()
        max_total_time = cfg.vwap_max_total_sec  # 复用 VWAP 的总时限

        logger.info(
            f"[Iceberg] {symbol} 总量={total_amount:.2f}, "
            f"暴露={show_amount:.2f} ({cfg.iceberg_show_pct*100:.0f}%), "
            f"offset={cfg.iceberg_price_offset_bps:.1f}bps"
        )

        while remaining > 0:
            # 超时检查
            elapsed = time.monotonic() - total_start
            if elapsed > max_total_time:
                logger.warning(f"[Iceberg] {symbol} 总超时 ({elapsed:.0f}s)，剩余 fallback 市价")
                if cfg.iceberg_fallback_to_market and remaining > 0:
                    # 剩余量用市价扫尾
                    slice_result = self._place_single_order(
                        symbol, side, remaining, exchange_name, account_id,
                        slice_index=slice_index
                    )
                    slice_result.algo_phase = 'iceberg_market_fallback'
                    result.slices.append(slice_result)
                    if slice_result.success:
                        result.completed_slices += 1
                        remaining -= slice_result.filled_amount
                break

            # 取消检查
            if result.status == SmartOrderStatus.CANCELLED:
                break

            # 本片暴露量
            current_show = min(show_amount, remaining)
            if current_show <= 0:
                break

            # 计算限价
            limit_price = self._calculate_iceberg_price(symbol, side, exchange_name)
            if limit_price <= 0:
                # 无法获取价格，fallback 市价
                slice_result = self._place_single_order(
                    symbol, side, current_show, exchange_name, account_id,
                    slice_index=slice_index
                )
                slice_result.algo_phase = 'iceberg_no_price_fallback'
                result.slices.append(slice_result)
                if slice_result.success:
                    result.completed_slices += 1
                    remaining -= slice_result.filled_amount
                else:
                    result.failed_slices += 1
                slice_index += 1
                continue

            # 挂限价单
            slice_result = self._place_limit_order(
                symbol, side, current_show, limit_price,
                exchange_name, account_id, slice_index,
                max_wait_sec=cfg.iceberg_max_wait_sec
            )
            slice_result.algo_phase = f'iceberg_limit_{slice_index}'
            result.slices.append(slice_result)

            if slice_result.success and slice_result.filled_amount > 0:
                result.completed_slices += 1
                remaining -= slice_result.filled_amount
            elif not slice_result.success and cfg.iceberg_fallback_to_market:
                # 限价未成交，用市价补
                logger.debug(
                    f"[Iceberg] {symbol} 第{slice_index}片限价未成交，市价补单"
                )
                market_result = self._place_single_order(
                    symbol, side, current_show, exchange_name, account_id,
                    slice_index=slice_index
                )
                market_result.algo_phase = 'iceberg_chase_market'
                result.slices.append(market_result)
                if market_result.success:
                    result.completed_slices += 1
                    remaining -= market_result.filled_amount
                else:
                    result.failed_slices += 1
                    if result.failed_slices >= 3:
                        result.status = SmartOrderStatus.FAILED
                        result.error = "Iceberg 连续失败"
                        break
            else:
                result.failed_slices += 1
                if result.failed_slices >= 3:
                    result.status = SmartOrderStatus.FAILED
                    result.error = "Iceberg 连续失败"
                    break

            slice_index += 1
            result.total_slices = slice_index

            # 刷新间隔
            if remaining > 0:
                time.sleep(cfg.iceberg_refresh_sec)

        # 最终状态
        if result.status not in (SmartOrderStatus.CANCELLED, SmartOrderStatus.FAILED):
            if remaining <= 0:
                result.status = SmartOrderStatus.FILLED
            elif result.completed_slices > 0:
                result.status = SmartOrderStatus.PARTIAL
            else:
                result.status = SmartOrderStatus.FAILED



    # ══════════════════════════════════════════════════════════════
    #  PARTICIPATION — 市场跟随型执行（v2.0 新增）
    # ══════════════════════════════════════════════════════════════

    def _execute_participation(self, result: SmartOrderResult, symbol: str,
                               side: OrderSide, total_amount: float,
                               notional_usdt: float,
                               exchange_name: str, account_id: str):
        """
        Participation Rate 执行：跟随市场成交节奏。

        核心逻辑：
          1. 实时监控市场成交流速（通过 recent trades API）
          2. 限制自身成交量不超过市场的 participation_rate（默认 15%）
          3. 市场交易活跃时多执行，市场冷清时少执行或暂停
          4. 避免被量化检测算法识别为"大单扫盘"

        优势：
          - 隐蔽性最强（与市场背景噪声融为一体）
          - 不会在低流动性时段强行执行
          - 适合较大仓位（>5000U）的长期执行

        适用场景：
          - 大仓位开仓（notional > 5000U）
          - 对时间不敏感（可以等 30-60 秒）
          - 希望最小化市场冲击和信息泄露
        """
        cfg = self.config
        remaining = total_amount
        slice_index = 0
        total_start = time.monotonic()
        last_market_volume = 0.0
        consecutive_low_volume = 0

        logger.info(
            f"[Participation] {symbol} 总量={total_amount:.2f}, "
            f"rate={cfg.participation_rate*100:.0f}%, "
            f"max_time={cfg.participation_max_total_sec:.0f}s"
        )

        while remaining > 0:
            # 超时检查
            elapsed = time.monotonic() - total_start
            if elapsed > cfg.participation_max_total_sec:
                logger.warning(
                    f"[Participation] {symbol} 总超时 ({elapsed:.0f}s)，"
                    f"剩余 {remaining:.2f} 用市价扫尾"
                )
                # 超时：剩余用市价一笔完成
                sweep_result = self._place_single_order(
                    symbol, side, remaining, exchange_name, account_id,
                    slice_index=slice_index
                )
                sweep_result.algo_phase = 'participation_timeout_sweep'
                result.slices.append(sweep_result)
                if sweep_result.success:
                    result.completed_slices += 1
                    remaining -= sweep_result.filled_amount
                break

            if result.status == SmartOrderStatus.CANCELLED:
                break

            # 获取市场最近成交流速
            market_volume_per_sec = self._get_market_flow_rate(symbol, exchange_name)

            # 市场太冷清时等待
            if market_volume_per_sec < cfg.participation_min_market_volume:
                consecutive_low_volume += 1
                if consecutive_low_volume >= 5:
                    logger.debug(
                        f"[Participation] {symbol} 市场流动性持续低迷 "
                        f"({market_volume_per_sec:.0f} U/s)，暂停等待"
                    )
                time.sleep(cfg.participation_check_interval_sec)
                continue

            consecutive_low_volume = 0

            # 计算本轮可执行量
            # 在 check_interval 时间窗口内，市场预期成交量
            expected_market_volume_usdt = (
                market_volume_per_sec * cfg.participation_check_interval_sec
            )
            # 我的目标量（USDT）= 市场量 × participation_rate
            my_target_usdt = expected_market_volume_usdt * cfg.participation_rate

            # 转换为代币数量
            mid_price = self._get_mid_price(symbol, exchange_name)
            if mid_price <= 0:
                time.sleep(1.0)
                continue

            my_target_amount = my_target_usdt / mid_price
            # 限制：不超过剩余量，不低于最小有意义量
            current_slice = min(my_target_amount, remaining)
            min_slice = total_amount * 0.02  # 最小片 = 总量的 2%
            if current_slice < min_slice:
                # 太小了，累积到下一轮
                time.sleep(cfg.participation_check_interval_sec)
                continue

            # 执行本片
            slice_result = self._place_single_order(
                symbol, side, current_slice, exchange_name, account_id,
                slice_index=slice_index
            )
            slice_result.algo_phase = (
                f'participation_{slice_index}_'
                f'mkt{market_volume_per_sec:.0f}U/s_'
                f'my{my_target_usdt:.0f}U'
            )
            result.slices.append(slice_result)

            if slice_result.success:
                result.completed_slices += 1
                remaining -= slice_result.filled_amount
                last_market_volume = market_volume_per_sec
            else:
                result.failed_slices += 1
                if result.failed_slices >= 3:
                    result.status = SmartOrderStatus.FAILED
                    result.error = "Participation 连续失败"
                    break

            slice_index += 1
            result.total_slices = slice_index

            # 等待下一个检查周期
            time.sleep(cfg.participation_check_interval_sec)

        # 最终状态
        if result.status not in (SmartOrderStatus.CANCELLED, SmartOrderStatus.FAILED):
            if remaining <= 0:
                result.status = SmartOrderStatus.FILLED
            elif result.completed_slices > 0:
                result.status = SmartOrderStatus.PARTIAL
            else:
                result.status = SmartOrderStatus.FAILED



    # ══════════════════════════════════════════════════════════════
    #  ADAPTIVE — 深度驱动动态策略切换（v2.0 增强）
    # ══════════════════════════════════════════════════════════════

    def _execute_adaptive(self, result: SmartOrderResult, symbol: str,
                          side: OrderSide, total_amount: float,
                          notional_usdt: float,
                          exchange_name: str, account_id: str):
        """
        Adaptive 执行：根据实时 Order Book 深度动态决策。

        v2.0 增强：
          1. 集成 DepthMonitor 获取实时深度分析（不只是 REST 快照）
          2. 执行过程中持续监控深度变化
          3. 深度骤降时自动暂停执行
          4. 根据流动性等级动态切换算法：
             - Grade A（流动性优秀）：直接市价
             - Grade B（流动性良好）：TWAP 2片
             - Grade C（流动性一般）：VWAP 4片
             - Grade D（流动性差）：Iceberg 或暂停

        核心改进：
          - 每片之间重新评估深度（不再假设深度恒定）
          - 如果某片滑点异常大，立即切换到更保守的策略
          - 支持"暂停 + 恢复"机制（等深度恢复后继续）
        """
        cfg = self.config

        # 首次深度分析
        depth_analysis = self._get_depth_analysis(symbol, side, notional_usdt)
        result.depth_checks += 1

        if depth_analysis is None:
            # 无深度数据，降级 TWAP
            logger.debug(f"[Adaptive] {symbol} 无深度数据，降级 TWAP")
            return self._execute_twap(result, symbol, side, total_amount,
                                      exchange_name, account_id)

        # 根据流动性等级选择初始策略
        grade = depth_analysis.liquidity_grade
        logger.info(
            f"[Adaptive] {symbol} 流动性={grade} "
            f"(score={depth_analysis.liquidity_score:.0f}, "
            f"slippage_est={depth_analysis.estimated_slippage_bps:.1f}bps, "
            f"imbalance={depth_analysis.imbalance_ratio:.2f})"
        )

        if grade == 'A' and depth_analysis.estimated_slippage_bps <= 5:
            # 流动性优秀：直接市价
            logger.debug(f"[Adaptive] {symbol} Grade A → 直接市价")
            return self._execute_market(result, symbol, side, total_amount,
                                        exchange_name, account_id)

        elif grade in ('A', 'B'):
            # 流动性良好：TWAP 2~3 片 + 深度持续监控
            n_slices = depth_analysis.recommended_slices
            n_slices = max(2, min(n_slices, 4))
            return self._adaptive_sliced_execution(
                result, symbol, side, total_amount, notional_usdt,
                exchange_name, account_id, n_slices, grade
            )

        elif grade == 'C':
            # 流动性一般：VWAP 策略（利用高流动性时段）
            logger.debug(f"[Adaptive] {symbol} Grade C → 委托 VWAP")
            result.algo_switches += 1
            return self._execute_vwap(result, symbol, side, total_amount,
                                      notional_usdt, exchange_name, account_id)

        else:
            # 流动性差 (Grade D)：Iceberg + 极保守执行
            if depth_analysis.recommendation == 'abort':
                logger.warning(
                    f"[Adaptive] {symbol} Grade D + abort 建议，"
                    f"预估滑点 {depth_analysis.estimated_slippage_bps:.0f}bps 过高"
                )
                # 仍然尝试 Iceberg（小暴露量）
                result.algo_switches += 1
                return self._execute_iceberg(result, symbol, side, total_amount,
                                             notional_usdt, exchange_name, account_id)
            else:
                result.algo_switches += 1
                return self._execute_iceberg(result, symbol, side, total_amount,
                                             notional_usdt, exchange_name, account_id)

    def _adaptive_sliced_execution(
        self,
        result: SmartOrderResult,
        symbol: str,
        side: OrderSide,
        total_amount: float,
        notional_usdt: float,
        exchange_name: str,
        account_id: str,
        n_slices: int,
        initial_grade: str,
    ):
        """
        Adaptive 分片执行（带实时深度反馈）。

        每片之间：
          1. 重新查询深度
          2. 如果流动性下降，动态增加片数或暂停
          3. 如果某片滑点异常，切换到 Iceberg
        """
        cfg = self.config
        slice_amount = total_amount / n_slices
        result.total_slices = n_slices
        prev_depth = None

        for i in range(n_slices):
            if result.status == SmartOrderStatus.CANCELLED:
                break

            # v2.0: 每片前重新检查深度
            if cfg.adaptive_recheck_depth and i > 0:
                new_analysis = self._get_depth_analysis(
                    symbol, side, notional_usdt / (n_slices - i)
                )
                result.depth_checks += 1

                if new_analysis:
                    # 检查深度是否骤降
                    if cfg.adaptive_pause_on_drain and prev_depth:
                        current_depth = (new_analysis.bid_depth_usdt
                                         if side == OrderSide.SELL
                                         else new_analysis.ask_depth_usdt)
                        if current_depth < prev_depth * cfg.adaptive_drain_threshold:
                            logger.warning(
                                f"[Adaptive] {symbol} 深度骤降 "
                                f"({prev_depth:.0f} → {current_depth:.0f} USDT)，"
                                f"暂停 5s 等待恢复"
                            )
                            time.sleep(5.0)
                            # 重新检查
                            new_analysis = self._get_depth_analysis(
                                symbol, side, notional_usdt / (n_slices - i)
                            )
                            result.depth_checks += 1

                    # 更新前一次深度
                    if new_analysis:
                        prev_depth = (new_analysis.bid_depth_usdt
                                      if side == OrderSide.SELL
                                      else new_analysis.ask_depth_usdt)

                        # 如果流动性降级，切换到更保守策略
                        if new_analysis.liquidity_grade == 'D':
                            logger.warning(
                                f"[Adaptive] {symbol} 流动性降至 D，"
                                f"剩余量切换 Iceberg"
                            )
                            filled_so_far = sum(s.filled_amount for s in result.slices)
                            remaining = total_amount - filled_so_far
                            if remaining > 0:
                                result.algo_switches += 1
                                self._execute_iceberg(
                                    result, symbol, side, remaining,
                                    remaining * (self._get_mid_price(symbol, exchange_name) or 1),
                                    exchange_name, account_id
                                )
                            return

            # 最后一片用剩余量
            if i == n_slices - 1:
                filled_so_far = sum(s.filled_amount for s in result.slices)
                slice_amount = total_amount - filled_so_far
                if slice_amount <= 0:
                    break

            slice_result = self._place_single_order(
                symbol, side, slice_amount, exchange_name, account_id, slice_index=i
            )
            slice_result.algo_phase = f'adaptive_{initial_grade}_{i}'
            result.slices.append(slice_result)

            if slice_result.success:
                result.completed_slices += 1

                # 检查滑点异常
                if (cfg.cancel_on_excessive_slippage
                        and slice_result.slippage_bps > cfg.adaptive_max_slippage_bps):
                    logger.warning(
                        f"[Adaptive] {symbol} 滑点过大 "
                        f"({slice_result.slippage_bps:.0f} bps > "
                        f"{cfg.adaptive_max_slippage_bps} bps)，"
                        f"剩余切换 Iceberg"
                    )
                    filled_so_far = sum(s.filled_amount for s in result.slices)
                    remaining = total_amount - filled_so_far
                    if remaining > 0:
                        result.algo_switches += 1
                        self._execute_iceberg(
                            result, symbol, side, remaining,
                            remaining * (self._get_mid_price(symbol, exchange_name) or 1),
                            exchange_name, account_id
                        )
                    return
            else:
                result.failed_slices += 1
                if result.failed_slices >= 2:
                    result.status = SmartOrderStatus.FAILED
                    result.error = "Adaptive 连续失败"
                    break

            # 自适应间隔
            if i < n_slices - 1:
                time.sleep(max(1.0, cfg.twap_interval_sec))

        if result.status not in (SmartOrderStatus.CANCELLED, SmartOrderStatus.FAILED):
            if result.completed_slices >= n_slices:
                result.status = SmartOrderStatus.FILLED
            elif result.completed_slices > 0:
                result.status = SmartOrderStatus.PARTIAL
            else:
                result.status = SmartOrderStatus.FAILED



    # ══════════════════════════════════════════════════════════════
    #  底层下单方法
    # ══════════════════════════════════════════════════════════════

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

    def _place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        amount: float,
        price: float,
        exchange_name: str,
        account_id: str,
        slice_index: int,
        max_wait_sec: float = 8.0,
    ) -> SliceResult:
        """
        放置限价单并等待成交。

        逻辑:
          1. 挂限价单
          2. 每 0.5s 检查一次是否成交
          3. 超时后撤单，返回已成交部分
        """
        t0 = time.monotonic()
        result = SliceResult(slice_index=slice_index, amount=amount)

        try:
            from exchange_manager import get_binance
            exchange = get_binance(authenticated=True)

            # 挂限价单
            order_params = {'positionSide': 'SHORT' if side == OrderSide.SELL else 'LONG'}
            if side == OrderSide.BUY:
                order = exchange.create_limit_buy_order(
                    symbol, amount, price, params=order_params
                )
            else:
                order = exchange.create_limit_sell_order(
                    symbol, amount, price, params=order_params
                )

            order_id = str(order.get('id', ''))
            result.order_id = order_id

            # 轮询等待成交
            deadline = time.monotonic() + max_wait_sec
            while time.monotonic() < deadline:
                time.sleep(0.5)
                try:
                    status = exchange.fetch_order(order_id, symbol)
                    filled = float(status.get('filled', 0))
                    if filled >= amount * 0.95:  # 95% 以上视为全部成交
                        result.success = True
                        result.filled_amount = filled
                        result.avg_price = float(
                            status.get('average', status.get('price', price))
                        )
                        break
                    elif status.get('status') in ('closed', 'canceled', 'cancelled'):
                        result.filled_amount = filled
                        result.avg_price = float(
                            status.get('average', price)
                        ) if filled > 0 else 0
                        result.success = filled > 0
                        break
                except Exception:
                    continue

            # 超时：撤单并记录已成交部分
            if not result.success and result.filled_amount == 0:
                try:
                    exchange.cancel_order(order_id, symbol)
                    # 再查一次（撤单瞬间可能有成交）
                    time.sleep(0.3)
                    final = exchange.fetch_order(order_id, symbol)
                    filled = float(final.get('filled', 0))
                    if filled > 0:
                        result.filled_amount = filled
                        result.avg_price = float(
                            final.get('average', price)
                        )
                        result.success = True
                except Exception as e:
                    result.error = f"撤单异常: {e}"

        except ImportError:
            result.error = "exchange_manager 不可用"
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



    # ══════════════════════════════════════════════════════════════
    #  辅助方法
    # ══════════════════════════════════════════════════════════════

    def _select_algo(self, symbol: str, notional_usdt: float,
                     exchange_name: str, side: OrderSide = OrderSide.SELL) -> AlgoType:
        """
        根据仓位大小、深度和市场状况自动选择最优算法。

        决策树：
          notional ≤ 1000U → MARKET
          notional 1000~3000U:
            - 深度充足 → MARKET
            - 深度一般 → TWAP
            - 深度不足 → ADAPTIVE
          notional 3000~5000U:
            - 深度充足 → TWAP
            - 深度不足 → VWAP / ADAPTIVE
          notional > 5000U:
            - 高流动性 → VWAP
            - 低流动性 → PARTICIPATION
        """
        if notional_usdt <= self.config.auto_threshold_usdt:
            return AlgoType.MARKET

        # 获取深度分析
        depth_analysis = self._get_depth_analysis(symbol, side, notional_usdt)

        if depth_analysis:
            grade = depth_analysis.liquidity_grade
            est_slippage = depth_analysis.estimated_slippage_bps

            if notional_usdt <= 3000:
                if grade == 'A' and est_slippage <= 5:
                    return AlgoType.MARKET
                elif grade in ('A', 'B'):
                    return AlgoType.TWAP
                else:
                    return AlgoType.ADAPTIVE

            elif notional_usdt <= 5000:
                if grade in ('A', 'B') and est_slippage <= 10:
                    return AlgoType.TWAP
                elif grade == 'C':
                    return AlgoType.VWAP
                else:
                    return AlgoType.ADAPTIVE

            else:  # > 5000U
                if grade in ('A', 'B'):
                    return AlgoType.VWAP
                elif grade == 'C':
                    return AlgoType.PARTICIPATION
                else:
                    return AlgoType.PARTICIPATION

        # 无深度数据时的 fallback
        if notional_usdt >= 5000:
            return AlgoType.VWAP
        elif notional_usdt >= 3000:
            return AlgoType.TWAP
        return AlgoType.MARKET

    def _get_depth_analysis(self, symbol: str, side: OrderSide,
                            notional_usdt: float):
        """获取 DepthMonitor 的深度分析（优先实时 WS，fallback REST）"""
        try:
            from execution.orderbook_monitor import get_depth_monitor
            monitor = get_depth_monitor()
            analysis = monitor.analyze(
                symbol,
                side='sell' if side == OrderSide.SELL else 'buy',
                notional_usdt=notional_usdt
            )
            return analysis
        except Exception as e:
            logger.debug(f"DepthMonitor 分析失败: {e}")
            return None

    def _check_depth_before_slice(
        self,
        symbol: str,
        side: OrderSide,
        amount: float,
        notional_usdt: float,
        exchange_name: str,
        result: SmartOrderResult,
    ) -> bool:
        """
        每片下单前检查深度是否充足。

        返回 True 表示可以继续执行，False 表示应该暂停/等待。
        """
        analysis = self._get_depth_analysis(symbol, side, notional_usdt)
        result.depth_checks += 1

        if analysis is None:
            return True  # 无数据时乐观假设

        # 检查是否建议 abort
        if analysis.recommendation == 'abort':
            logger.warning(
                f"[DepthCheck] {symbol} 深度分析建议 abort "
                f"(slippage={analysis.estimated_slippage_bps:.0f}bps)"
            )
            return False

        # 检查流动性等级
        if analysis.liquidity_grade == 'D':
            return False

        return True

    def _calculate_iceberg_price(self, symbol: str, side: OrderSide,
                                 exchange_name: str) -> float:
        """
        计算 Iceberg 限价单的挂单价格。

        策略：
          - 做空开仓（sell）：挂在 mid_price + offset（稍高于中间价，等买方来吃）
          - 做多开仓（buy）：挂在 mid_price - offset（稍低于中间价，等卖方来吃）
        """
        mid = self._get_mid_price(symbol, exchange_name)
        if mid <= 0:
            return 0.0

        offset = mid * self.config.iceberg_price_offset_bps / 10000

        if side == OrderSide.SELL:
            # 做空：挂卖单稍高于 mid（让 taker 来吃我的单）
            return mid + offset
        else:
            # 做多：挂买单稍低于 mid
            return mid - offset

    def _get_market_flow_rate(self, symbol: str, exchange_name: str) -> float:
        """
        获取市场最近成交流速 (USDT/秒)。

        通过 Binance recent trades API 计算过去 N 秒内的成交金额。
        """
        try:
            import requests
            sym = symbol.replace('/USDT', 'USDT').replace('/', '')
            r = requests.get(
                "https://fapi.binance.com/fapi/v1/trades",
                params={"symbol": sym, "limit": 50},
                timeout=5,
            )
            if r.status_code != 200:
                return 0.0

            trades = r.json()
            if not trades:
                return 0.0

            # 计算时间跨度和总成交额
            now_ms = int(time.time() * 1000)
            total_notional = 0.0
            oldest_time = now_ms

            for t in trades:
                trade_time = int(t.get('time', now_ms))
                price = float(t.get('price', 0))
                qty = float(t.get('qty', 0))
                total_notional += price * qty
                oldest_time = min(oldest_time, trade_time)

            time_span_sec = max((now_ms - oldest_time) / 1000, 1.0)
            return total_notional / time_span_sec

        except Exception as e:
            logger.debug(f"获取市场流速失败 ({symbol}): {e}")
            return 0.0

    def _get_mid_price(self, symbol: str, exchange_name: str) -> float:
        """获取当前中间价"""
        # 优先从 DepthMonitor 获取（实时 WS 数据）
        try:
            from execution.orderbook_monitor import get_depth_monitor
            monitor = get_depth_monitor()
            snapshot = monitor.get_snapshot(symbol)
            if snapshot and snapshot.mid_price > 0:
                return snapshot.mid_price
        except Exception:
            pass

        # Fallback: REST orderbook
        try:
            orderbook = self._get_orderbook(symbol, exchange_name, depth=1)
            if orderbook:
                bids = orderbook.get('bids', [])
                asks = orderbook.get('asks', [])
                if bids and asks:
                    return (bids[0][0] + asks[0][0]) / 2
        except Exception:
            pass

        # Final fallback: ticker
        try:
            from exchange_manager import get_binance
            exchange = get_binance()
            ticker = exchange.fetch_ticker(symbol)
            return ticker.get('last', 0)
        except Exception:
            return 0.0

    def _get_orderbook(self, symbol: str, exchange_name: str,
                       depth: int = 10) -> Optional[Dict]:
        """获取订单簿（REST）"""
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
