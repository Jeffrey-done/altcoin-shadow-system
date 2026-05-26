#!/usr/bin/env python3
"""
实时 Order Book 深度监控 v1.0

通过 Binance WebSocket Depth Stream 实时监控交易对的订单簿深度。
为 SmartOrderEngine 和风控系统提供以下信号：

核心功能：
  1. 深度评估：开仓前检查是否有足够流动性
  2. 冲击预估：根据当前深度预估 N 个 notional 的滑点
  3. 不平衡检测：买卖压力失衡 → 短期方向预判
  4. 流动性枯竭告警：深度骤降时发出预警

数据流：
  Binance WS → depth@100ms → 本地 Order Book 重建 → 分析指标 → 事件总线

使用方式：
  from execution.orderbook_monitor import get_depth_monitor

  monitor = get_depth_monitor()
  monitor.subscribe('PEPE/USDT')

  # 获取实时深度分析
  analysis = monitor.analyze('PEPE/USDT', side='sell', notional_usdt=3000)
  print(f"预估滑点: {analysis.estimated_slippage_bps:.1f} bps")
  print(f"买卖不平衡: {analysis.imbalance_ratio:.2f}")
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("execution.orderbook_monitor")


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class DepthMonitorConfig:
    """深度监控配置"""
    enabled: bool = True

    # WebSocket
    ws_url: str = 'wss://fstream.binance.com/ws'
    depth_levels: int = 20              # 订阅深度档位
    update_speed: str = '100ms'         # 更新频率（100ms / 500ms）

    # 分析参数
    imbalance_threshold: float = 0.3    # 不平衡阈值（>0.3 视为显著）
    liquidity_alert_threshold: float = 0.5  # 流动性骤降告警阈值（减少 50%+）
    depth_snapshot_interval_sec: float = 5.0  # 深度快照间隔（统计用）

    # 冲击模型
    impact_model: str = 'linear'        # 'linear' | 'sqrt' (Almgren-Chriss 简化)
    impact_coefficient: float = 0.5     # 冲击系数

    # 监控的最大品种数（WebSocket 连接数限制）
    max_symbols: int = 10

    # 重连
    reconnect_delay_sec: float = 3.0
    max_reconnect_attempts: int = 10


# ══════════════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class OrderBookLevel:
    """单档盘口"""
    price: float
    quantity: float

    @property
    def notional(self) -> float:
        return self.price * self.quantity


@dataclass
class OrderBookSnapshot:
    """订单簿快照"""
    symbol: str
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)
    timestamp: float = 0.0
    last_update_id: int = 0

    @property
    def mid_price(self) -> float:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2
        return 0.0

    @property
    def spread_bps(self) -> float:
        if self.bids and self.asks:
            return (self.asks[0].price - self.bids[0].price) / self.mid_price * 10000
        return 0.0

    @property
    def bid_depth_usdt(self) -> float:
        """买方前 N 档总深度 (USDT)"""
        return sum(l.notional for l in self.bids)

    @property
    def ask_depth_usdt(self) -> float:
        """卖方前 N 档总深度 (USDT)"""
        return sum(l.notional for l in self.asks)

    def bid_depth_at_pct(self, pct: float) -> float:
        """距 mid 价 N% 以内的买方深度"""
        if not self.mid_price:
            return 0
        limit = self.mid_price * (1 - pct / 100)
        return sum(l.notional for l in self.bids if l.price >= limit)

    def ask_depth_at_pct(self, pct: float) -> float:
        """距 mid 价 N% 以内的卖方深度"""
        if not self.mid_price:
            return 0
        limit = self.mid_price * (1 + pct / 100)
        return sum(l.notional for l in self.asks if l.price <= limit)


@dataclass
class DepthAnalysis:
    """深度分析结果"""
    symbol: str
    timestamp: float = 0.0

    # 基础指标
    mid_price: float = 0.0
    spread_bps: float = 0.0
    bid_depth_usdt: float = 0.0         # 买方总深度
    ask_depth_usdt: float = 0.0         # 卖方总深度

    # 不平衡度 (-1~1): 正数=买方强势，负数=卖方强势
    imbalance_ratio: float = 0.0

    # 冲击预估
    estimated_slippage_bps: float = 0.0  # 给定 notional 的预估滑点
    estimated_fill_price: float = 0.0    # 预估成交均价
    depth_sufficient: bool = True        # 深度是否充足
    recommended_slices: int = 1          # 建议拆分片数

    # 流动性评估
    liquidity_score: float = 0.0        # 0~100 流动性评分
    liquidity_grade: str = 'A'          # A/B/C/D 等级

    # 建议
    recommendation: str = ''             # 'proceed' / 'reduce_size' / 'use_twap' / 'abort'


# ══════════════════════════════════════════════════════════════════
#  深度监控器
# ══════════════════════════════════════════════════════════════════

class DepthMonitor:
    """
    实时 Order Book 深度监控器。

    用法:
      monitor = DepthMonitor()
      monitor.start()
      monitor.subscribe('PEPE/USDT')

      # 分析深度
      analysis = monitor.analyze('PEPE/USDT', side='sell', notional_usdt=3000)
    """

    def __init__(self, config: Optional[DepthMonitorConfig] = None):
        self.config = config or DepthMonitorConfig()
        self._books: Dict[str, OrderBookSnapshot] = {}
        self._lock = threading.RLock()
        self._ws_thread: Optional[threading.Thread] = None
        self._running = False
        self._subscribed_symbols: set = set()
        self._history: Dict[str, List[Tuple[float, float, float]]] = defaultdict(list)
        # history: symbol → [(timestamp, bid_depth, ask_depth), ...]

    def start(self) -> None:
        """启动深度监控"""
        if not self.config.enabled:
            logger.info("📊 OrderBook Monitor 已禁用")
            return

        self._running = True
        self._ws_thread = threading.Thread(
            target=self._ws_loop,
            name='depth_monitor_ws',
            daemon=True,
        )
        self._ws_thread.start()
        logger.info("📊 OrderBook Depth Monitor 已启动")

    def stop(self) -> None:
        """停止监控"""
        self._running = False
        logger.info("📊 OrderBook Depth Monitor 已停止")

    def subscribe(self, symbol: str) -> bool:
        """订阅品种深度数据"""
        if len(self._subscribed_symbols) >= self.config.max_symbols:
            logger.warning(
                f"深度监控品种数已达上限 ({self.config.max_symbols})，"
                f"无法订阅 {symbol}"
            )
            return False

        self._subscribed_symbols.add(symbol)
        logger.info(f"📊 深度订阅: {symbol}")
        return True

    def unsubscribe(self, symbol: str):
        """取消订阅"""
        self._subscribed_symbols.discard(symbol)
        with self._lock:
            self._books.pop(symbol, None)

    def get_snapshot(self, symbol: str) -> Optional[OrderBookSnapshot]:
        """获取当前订单簿快照"""
        with self._lock:
            return self._books.get(symbol)

    def analyze(self, symbol: str, side: str = 'sell',
                notional_usdt: float = 3000) -> DepthAnalysis:
        """
        分析指定品种的深度，预估滑点。

        参数:
          symbol: 交易对
          side: 'buy' 或 'sell'（做空开仓 = sell）
          notional_usdt: 计划执行的名义金额

        返回:
          DepthAnalysis 包含完整分析结果
        """
        analysis = DepthAnalysis(symbol=symbol, timestamp=time.time())

        with self._lock:
            book = self._books.get(symbol)

        if not book or not book.bids or not book.asks:
            # 无实时数据，尝试 REST fallback
            book = self._fetch_rest_orderbook(symbol)
            if not book:
                analysis.recommendation = 'proceed'  # 无数据时不阻塞
                analysis.depth_sufficient = True  # 乐观假设
                return analysis

        # 基础指标
        analysis.mid_price = book.mid_price
        analysis.spread_bps = book.spread_bps
        analysis.bid_depth_usdt = book.bid_depth_usdt
        analysis.ask_depth_usdt = book.ask_depth_usdt

        # 不平衡度
        total_depth = analysis.bid_depth_usdt + analysis.ask_depth_usdt
        if total_depth > 0:
            analysis.imbalance_ratio = (
                (analysis.bid_depth_usdt - analysis.ask_depth_usdt) / total_depth
            )

        # 冲击预估
        if side == 'sell':
            levels = book.bids  # 做空开仓吃 bid 方深度
        else:
            levels = book.asks

        slippage, fill_price, slices = self._estimate_impact(
            levels, notional_usdt, book.mid_price
        )
        analysis.estimated_slippage_bps = slippage
        analysis.estimated_fill_price = fill_price
        analysis.recommended_slices = slices

        # 深度充足性判断
        opposite_depth = analysis.bid_depth_usdt if side == 'sell' else analysis.ask_depth_usdt
        analysis.depth_sufficient = opposite_depth >= notional_usdt * 3

        # 流动性评分
        analysis.liquidity_score = self._calculate_liquidity_score(
            book, notional_usdt, analysis.spread_bps
        )
        if analysis.liquidity_score >= 80:
            analysis.liquidity_grade = 'A'
        elif analysis.liquidity_score >= 60:
            analysis.liquidity_grade = 'B'
        elif analysis.liquidity_score >= 40:
            analysis.liquidity_grade = 'C'
        else:
            analysis.liquidity_grade = 'D'

        # 建议
        if analysis.estimated_slippage_bps > 30:
            analysis.recommendation = 'abort'
        elif analysis.estimated_slippage_bps > 15:
            analysis.recommendation = 'use_twap'
        elif analysis.estimated_slippage_bps > 8:
            analysis.recommendation = 'reduce_size'
        else:
            analysis.recommendation = 'proceed'

        return analysis

    def get_imbalance(self, symbol: str) -> float:
        """
        快速获取买卖不平衡度 (-1~1)。
        正数 = 买方强势（看涨）
        负数 = 卖方强势（看跌，做空有利）
        """
        with self._lock:
            book = self._books.get(symbol)
        if not book:
            return 0.0

        total = book.bid_depth_usdt + book.ask_depth_usdt
        if total == 0:
            return 0.0
        return (book.bid_depth_usdt - book.ask_depth_usdt) / total

    # ── WebSocket 管理 ────────────────────────────────────────────

    def _ws_loop(self):
        """WebSocket 连接循环（带重连）"""
        attempts = 0
        while self._running:
            if not self._subscribed_symbols:
                time.sleep(1)
                continue

            try:
                self._run_ws_connection()
                attempts = 0
            except Exception as e:
                if self._running:
                    attempts += 1
                    delay = min(self.config.reconnect_delay_sec * attempts, 30)
                    logger.warning(
                        f"Depth WS 断线 (attempt {attempts}): {e}, "
                        f"{delay:.0f}s 后重连"
                    )
                    time.sleep(delay)

    def _run_ws_connection(self):
        """单次 WebSocket 连接"""
        try:
            import websocket
        except ImportError:
            logger.error("websocket-client 未安装")
            time.sleep(60)
            return

        # 构建 stream 列表
        streams = []
        for symbol in list(self._subscribed_symbols):
            ws_symbol = symbol.replace('/USDT', 'usdt').replace('/', '').lower()
            streams.append(f"{ws_symbol}@depth{self.config.depth_levels}@{self.config.update_speed}")

        if not streams:
            time.sleep(1)
            return

        url = f"{self.config.ws_url}/{'/'.join(streams)}"

        ws = websocket.WebSocketApp(
            url,
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_ws_message(self, ws, message: str):
        """处理 WebSocket 消息"""
        try:
            data = json.loads(message)

            # 解析 stream 数据
            if 'stream' in data:
                data = data.get('data', data)

            symbol = self._parse_symbol_from_ws(data)
            if not symbol:
                return

            bids = [
                OrderBookLevel(price=float(b[0]), quantity=float(b[1]))
                for b in data.get('b', data.get('bids', []))
            ]
            asks = [
                OrderBookLevel(price=float(a[0]), quantity=float(a[1]))
                for a in data.get('a', data.get('asks', []))
            ]

            if not bids and not asks:
                return

            with self._lock:
                self._books[symbol] = OrderBookSnapshot(
                    symbol=symbol,
                    bids=sorted(bids, key=lambda l: l.price, reverse=True),
                    asks=sorted(asks, key=lambda l: l.price),
                    timestamp=time.time(),
                    last_update_id=data.get('u', data.get('lastUpdateId', 0)),
                )

                # 记录历史
                book = self._books[symbol]
                self._history[symbol].append(
                    (time.time(), book.bid_depth_usdt, book.ask_depth_usdt)
                )
                # 保留最近 1000 条
                if len(self._history[symbol]) > 1000:
                    self._history[symbol] = self._history[symbol][-500:]

        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    def _on_ws_error(self, ws, error):
        if self._running:
            logger.debug(f"Depth WS error: {error}")

    def _on_ws_close(self, ws, close_status_code, close_msg):
        if self._running:
            logger.debug(f"Depth WS closed: {close_status_code}")

    def _parse_symbol_from_ws(self, data: dict) -> Optional[str]:
        """从 WS 数据中解析 symbol"""
        stream = data.get('stream', '') or data.get('s', '')
        if not stream:
            # 尝试从 data 中提取
            s = data.get('s', '')
            if s:
                # 'PEPEUSDT' → 'PEPE/USDT'
                if s.endswith('USDT'):
                    return s[:-4] + '/USDT'
            return None

        # 'pepeusdt@depth20@100ms' → 'PEPE/USDT'
        parts = stream.split('@')
        if parts:
            raw = parts[0].upper()
            if raw.endswith('USDT'):
                return raw[:-4] + '/USDT'
        return None

    # ── 分析辅助 ─────────────────────────────────────────────────

    def _estimate_impact(
        self,
        levels: List[OrderBookLevel],
        notional_usdt: float,
        mid_price: float,
    ) -> Tuple[float, float, int]:
        """
        估算市场冲击。

        返回: (slippage_bps, estimated_fill_price, recommended_slices)
        """
        if not levels or mid_price <= 0:
            return 0.0, mid_price, 1

        # 模拟逐档吃单
        remaining = notional_usdt
        total_filled_notional = 0.0
        total_filled_qty = 0.0

        for level in levels:
            level_notional = level.notional
            if level_notional <= 0:
                continue

            if remaining <= level_notional:
                # 当前档可以全部满足
                qty = remaining / level.price
                total_filled_notional += remaining
                total_filled_qty += qty
                remaining = 0
                break
            else:
                # 吃完当前档
                total_filled_notional += level_notional
                total_filled_qty += level.quantity
                remaining -= level_notional

        if total_filled_qty <= 0:
            return 50.0, mid_price, 5  # 深度严重不足

        # 加权平均成交价
        fill_price = total_filled_notional / total_filled_qty

        # 滑点
        slippage_bps = abs(fill_price - mid_price) / mid_price * 10000

        # 未能全部满足的部分
        if remaining > 0:
            # 深度不够，需要等待补充
            slippage_bps += remaining / notional_usdt * 20  # 惩罚

        # 建议拆分片数
        if slippage_bps <= 5:
            slices = 1
        elif slippage_bps <= 15:
            slices = 2
        elif slippage_bps <= 30:
            slices = 3
        else:
            slices = max(4, int(notional_usdt / 1000))

        return round(slippage_bps, 2), round(fill_price, 8), slices

    def _calculate_liquidity_score(self, book: OrderBookSnapshot,
                                   notional_usdt: float,
                                   spread_bps: float) -> float:
        """计算流动性评分 (0~100)"""
        score = 0.0

        # 价差评分 (0~30): 越小越好
        if spread_bps <= 3:
            score += 30
        elif spread_bps <= 8:
            score += 20
        elif spread_bps <= 15:
            score += 10
        elif spread_bps <= 30:
            score += 5

        # 深度评分 (0~40): 对手方深度 / 我的仓位
        min_depth = min(book.bid_depth_usdt, book.ask_depth_usdt)
        depth_ratio = min_depth / max(notional_usdt, 1)
        if depth_ratio >= 10:
            score += 40
        elif depth_ratio >= 5:
            score += 30
        elif depth_ratio >= 3:
            score += 20
        elif depth_ratio >= 1:
            score += 10

        # 对称性评分 (0~15): 买卖深度越平衡越好
        total = book.bid_depth_usdt + book.ask_depth_usdt
        if total > 0:
            symmetry = 1 - abs(book.bid_depth_usdt - book.ask_depth_usdt) / total
            score += symmetry * 15

        # 档位密度评分 (0~15): 前10档价格间距越密越好
        if len(book.asks) >= 5:
            price_range = book.asks[4].price - book.asks[0].price
            if book.asks[0].price > 0:
                density_pct = price_range / book.asks[0].price * 100
                if density_pct <= 0.5:
                    score += 15
                elif density_pct <= 1.0:
                    score += 10
                elif density_pct <= 2.0:
                    score += 5

        return min(100, round(score, 1))

    def _fetch_rest_orderbook(self, symbol: str) -> Optional[OrderBookSnapshot]:
        """REST API fallback 获取订单簿"""
        try:
            from exchange_manager import get_binance
            exchange = get_binance()
            raw = exchange.fetch_order_book(symbol, limit=self.config.depth_levels)

            bids = [OrderBookLevel(price=b[0], quantity=b[1]) for b in raw.get('bids', [])]
            asks = [OrderBookLevel(price=a[0], quantity=a[1]) for a in raw.get('asks', [])]

            return OrderBookSnapshot(
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp=time.time(),
            )
        except Exception as e:
            logger.debug(f"REST orderbook 获取失败 ({symbol}): {e}")
            return None


# ══════════════════════════════════════════════════════════════════
#  v2.0 增强方法
# ══════════════════════════════════════════════════════════════════

    def get_depth_change_rate(self, symbol: str, lookback_sec: float = 30.0) -> float:
        """
        计算深度变化率（过去 N 秒内的深度变化百分比）。

        返回:
          正数 = 深度增加（流动性改善）
          负数 = 深度减少（流动性恶化）
          0 = 无数据或无变化

        用途：SmartOrderEngine Adaptive 模式检测"流动性枯竭"
        """
        history = self._history.get(symbol, [])
        if len(history) < 2:
            return 0.0

        now = time.time()
        cutoff = now - lookback_sec

        # 找到 lookback_sec 前的快照
        old_entry = None
        for entry in history:
            if entry[0] >= cutoff:
                old_entry = entry
                break

        if old_entry is None:
            old_entry = history[0]

        latest = history[-1]

        old_depth = old_entry[1] + old_entry[2]  # bid + ask
        new_depth = latest[1] + latest[2]

        if old_depth <= 0:
            return 0.0

        return (new_depth - old_depth) / old_depth

    def get_spread_history(self, symbol: str, lookback_sec: float = 60.0) -> List[float]:
        """
        获取过去 N 秒的 spread 历史（bps）。

        用途：Adaptive 模式检测 spread 扩大 → 流动性枯竭预警
        """
        with self._lock:
            book = self._books.get(symbol)
        if not book:
            return []

        # 当前只能返回当前快照的 spread（历史 spread 需要额外存储）
        # v2.0: 返回单值列表供接口兼容
        return [book.spread_bps] if book.spread_bps > 0 else []

    def pre_execution_check(self, symbol: str, side: str,
                            notional_usdt: float) -> Dict[str, Any]:
        """
        开仓前综合检查（供 SmartOrderEngine 调用）。

        返回一个字典包含：
          - ok: bool — 是否可以执行
          - analysis: DepthAnalysis — 深度分析结果
          - warnings: List[str] — 警告信息
          - recommended_algo: str — 建议算法
          - max_single_order_usdt: float — 建议单笔最大金额
        """
        result = {
            'ok': True,
            'analysis': None,
            'warnings': [],
            'recommended_algo': 'market',
            'max_single_order_usdt': notional_usdt,
        }

        analysis = self.analyze(symbol, side, notional_usdt)
        result['analysis'] = analysis

        # 检查 1: 流动性等级
        if analysis.liquidity_grade == 'D':
            result['warnings'].append(
                f"流动性等级 D (score={analysis.liquidity_score:.0f})"
            )
            if analysis.recommendation == 'abort':
                result['ok'] = False
                result['warnings'].append("建议放弃本次交易")

        # 检查 2: 预估滑点
        if analysis.estimated_slippage_bps > 30:
            result['ok'] = False
            result['warnings'].append(
                f"预估滑点过高: {analysis.estimated_slippage_bps:.0f} bps"
            )
        elif analysis.estimated_slippage_bps > 15:
            result['warnings'].append(
                f"滑点较高: {analysis.estimated_slippage_bps:.0f} bps, 建议拆单"
            )

        # 检查 3: 买卖不平衡
        if side == 'sell' and analysis.imbalance_ratio > 0.4:
            result['warnings'].append(
                f"买方压力强 (imbalance={analysis.imbalance_ratio:.2f}), "
                f"做空可能面临反向冲击"
            )
        elif side == 'buy' and analysis.imbalance_ratio < -0.4:
            result['warnings'].append(
                f"卖方压力强 (imbalance={analysis.imbalance_ratio:.2f}), "
                f"做多可能面临反向冲击"
            )

        # 检查 4: 深度变化趋势
        depth_change = self.get_depth_change_rate(symbol, lookback_sec=30)
        if depth_change < -0.3:
            result['warnings'].append(
                f"深度快速下降 ({depth_change*100:.0f}%)，流动性可能枯竭"
            )

        # 推荐算法
        if analysis.estimated_slippage_bps <= 5 and analysis.liquidity_grade == 'A':
            result['recommended_algo'] = 'market'
        elif analysis.estimated_slippage_bps <= 10:
            result['recommended_algo'] = 'twap'
        elif analysis.estimated_slippage_bps <= 20:
            result['recommended_algo'] = 'vwap'
        else:
            result['recommended_algo'] = 'iceberg'

        # 建议单笔最大金额
        if analysis.depth_sufficient:
            result['max_single_order_usdt'] = notional_usdt
        else:
            # 不超过对手方深度的 20%
            opposite_depth = (analysis.bid_depth_usdt if side == 'sell'
                              else analysis.ask_depth_usdt)
            result['max_single_order_usdt'] = min(
                notional_usdt, opposite_depth * 0.2
            )

        return result


# ══════════════════════════════════════════════════════════════════
#  全局单例
# ══════════════════════════════════════════════════════════════════

_monitor: Optional[DepthMonitor] = None


def get_depth_monitor(config: Optional[DepthMonitorConfig] = None) -> DepthMonitor:
    """获取深度监控器单例"""
    global _monitor
    if _monitor is None:
        _monitor = DepthMonitor(config)
    return _monitor
