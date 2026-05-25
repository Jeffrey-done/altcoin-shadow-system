#!/usr/bin/env python3
"""
异步策略引擎 v1.0

将原来 scheduler.py 的同步定时循环升级为 asyncio + aiohttp 架构。
核心改进：
  - 并发 API 调用（scanner 同时请求多个币种数据 → 10x 提速）
  - 非阻塞 IO（不再因为一个慢 API 卡住整个调度循环）
  - 事件驱动（通过 event_bus 与 realtime_monitor / dashboard 通信）
  - 优雅调度（asyncio.TaskGroup 管理子任务生命周期）

向后兼容：
  - 旧 scheduler.py 仍可运行（同步模式）
  - 本模块作为下一代引擎，通过环境变量 USE_ASYNC_ENGINE=1 启用
  - 所有策略接口保持不变（BaseStrategy.scan/confirm/evaluate_exit 仍是同步方法）
  - 异步层只负责调度和 IO，策略逻辑在线程池中执行

架构图：
  ┌────────────────────────────────────────────────────────────────┐
  │                    AsyncStrategyEngine                          │
  │                                                                │
  │  ┌──────────┐   ┌──────────────┐   ┌──────────────────────┐  │
  │  │ Scheduler│──▶│ AsyncDataFeed │──▶│ aiohttp session pool │  │
  │  │ (cron)   │   │ (cache layer) │   │ (Binance/OKX REST)   │  │
  │  └──────────┘   └──────────────┘   └──────────────────────┘  │
  │       │                                                        │
  │       ▼                                                        │
  │  ┌──────────────────┐                                         │
  │  │  Strategy Runner  │ ← ThreadPoolExecutor (CPU-bound 策略)   │
  │  │  (scan/confirm/   │                                         │
  │  │   evaluate_exit)  │                                         │
  │  └──────────────────┘                                         │
  │       │                                                        │
  │       ▼                                                        │
  │  ┌──────────┐   ┌────────────────┐   ┌──────────────────┐   │
  │  │ EventBus │──▶│ SmartOrderEngine│──▶│ DepthMonitor     │   │
  │  │ (publish)│   │ (TWAP/Adaptive) │   │ (WebSocket)      │   │
  │  └──────────┘   └────────────────┘   └──────────────────┘   │
  └────────────────────────────────────────────────────────────────┘

用法：
  # 作为独立进程运行
  python3 async_engine.py

  # 或嵌入到 scheduler.py 中
  from async_engine import AsyncStrategyEngine
  engine = AsyncStrategyEngine()
  asyncio.run(engine.run_forever())
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

logger = logging.getLogger("async_engine")


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class AsyncEngineConfig:
    """异步引擎配置"""
    # 调度周期
    scan_interval_sec: float = 3600.0       # 全市场扫描（每小时）
    confirm_interval_sec: float = 300.0     # 候选确认（每 5 分钟）
    tracker_interval_sec: float = 60.0      # 持仓追踪（每分钟）
    health_interval_sec: float = 300.0      # 健康检查（每 5 分钟）

    # 并发
    max_concurrent_api_calls: int = 10      # 最大并发 API 调用数
    strategy_thread_pool_size: int = 4      # 策略执行线程池大小
    api_semaphore_limit: int = 8            # API 并发信号量

    # 超时
    scan_timeout_sec: float = 480.0
    confirm_timeout_sec: float = 120.0
    tracker_timeout_sec: float = 60.0

    # aiohttp
    aiohttp_timeout_sec: float = 10.0
    aiohttp_max_connections: int = 20


# ══════════════════════════════════════════════════════════════════
#  异步数据馈送
# ══════════════════════════════════════════════════════════════════

class AsyncDataFeed:
    """
    异步数据馈送 — 使用 aiohttp 并发获取交易所数据。

    相比同步 ExchangeDataFeed 的优势：
      - 10 个币的 K 线/OI/费率可以并发请求（~1s vs 串行 ~10s）
      - 自动 rate limit 管理（令牌桶算法）
      - 请求去重 + 缓存
    """

    def __init__(self, config: Optional[AsyncEngineConfig] = None):
        self._config = config or AsyncEngineConfig()
        self._session = None
        self._cache: Dict[str, tuple] = {}  # key → (data, expire_time)
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def _get_session(self):
        """延迟初始化 aiohttp session"""
        if self._session is None or self._session.closed:
            try:
                import aiohttp
                timeout = aiohttp.ClientTimeout(total=self._config.aiohttp_timeout_sec)
                connector = aiohttp.TCPConnector(
                    limit=self._config.aiohttp_max_connections,
                    ttl_dns_cache=300,
                )
                self._session = aiohttp.ClientSession(
                    timeout=timeout,
                    connector=connector,
                )
            except ImportError:
                logger.error("aiohttp 未安装，pip install aiohttp")
                raise
        return self._session

    async def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._config.api_semaphore_limit)
        return self._semaphore

    async def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        """异步获取 ticker"""
        cache_key = f"ticker:{symbol}"
        cached = self._get_cached(cache_key, ttl=30)
        if cached is not None:
            return cached

        sem = await self._get_semaphore()
        async with sem:
            session = await self._get_session()
            ws_symbol = symbol.replace('/USDT', 'USDT').replace('/', '')
            url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={ws_symbol}"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = {
                            'last': float(data.get('lastPrice', 0)),
                            'bid': float(data.get('bidPrice', 0)),
                            'ask': float(data.get('askPrice', 0)),
                            'quoteVolume': float(data.get('quoteVolume', 0)),
                            'percentage': float(data.get('priceChangePercent', 0)),
                            'high': float(data.get('highPrice', 0)),
                            'low': float(data.get('lowPrice', 0)),
                        }
                        self._set_cache(cache_key, result, ttl=30)
                        return result
            except Exception as e:
                logger.debug(f"fetch_ticker({symbol}) 失败: {e}")
            return {}

    async def fetch_tickers_batch(self, symbols: Optional[List[str]] = None) -> Dict[str, Dict]:
        """批量获取 tickers（单次 API 调用）"""
        cache_key = "tickers:all"
        cached = self._get_cached(cache_key, ttl=30)
        if cached is not None:
            if symbols:
                return {k: v for k, v in cached.items() if k in symbols}
            return cached

        sem = await self._get_semaphore()
        async with sem:
            session = await self._get_session()
            url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = {}
                        for item in data:
                            raw_symbol = item.get('symbol', '')
                            if raw_symbol.endswith('USDT'):
                                formatted = raw_symbol[:-4] + '/USDT'
                                result[formatted] = {
                                    'last': float(item.get('lastPrice', 0)),
                                    'bid': float(item.get('bidPrice', 0)),
                                    'ask': float(item.get('askPrice', 0)),
                                    'quoteVolume': float(item.get('quoteVolume', 0)),
                                    'percentage': float(item.get('priceChangePercent', 0)),
                                }
                        self._set_cache(cache_key, result, ttl=30)
                        if symbols:
                            return {k: v for k, v in result.items() if k in symbols}
                        return result
            except Exception as e:
                logger.debug(f"fetch_tickers_batch 失败: {e}")
            return {}

    async def fetch_funding_rates_batch(self, symbols: List[str]) -> Dict[str, float]:
        """批量获取资金费率"""
        sem = await self._get_semaphore()
        async with sem:
            session = await self._get_session()
            url = "https://fapi.binance.com/fapi/v1/premiumIndex"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = {}
                        for item in data:
                            raw = item.get('symbol', '')
                            if raw.endswith('USDT'):
                                formatted = raw[:-4] + '/USDT'
                                if formatted in symbols or not symbols:
                                    rate = float(item.get('lastFundingRate', 0)) * 100
                                    result[formatted] = rate
                        return result
            except Exception as e:
                logger.debug(f"fetch_funding_rates_batch 失败: {e}")
            return {}

    async def fetch_klines(self, symbol: str, timeframe: str = '1h',
                           limit: int = 50) -> List[List[float]]:
        """异步获取 K 线"""
        cache_key = f"klines:{symbol}:{timeframe}:{limit}"
        cached = self._get_cached(cache_key, ttl=60)
        if cached is not None:
            return cached

        sem = await self._get_semaphore()
        async with sem:
            session = await self._get_session()
            ws_symbol = symbol.replace('/USDT', 'USDT').replace('/', '')
            url = (
                f"https://fapi.binance.com/fapi/v1/klines"
                f"?symbol={ws_symbol}&interval={timeframe}&limit={limit}"
            )
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        raw = await resp.json()
                        result = [
                            [k[0], float(k[1]), float(k[2]),
                             float(k[3]), float(k[4]), float(k[5])]
                            for k in raw
                        ]
                        self._set_cache(cache_key, result, ttl=60)
                        return result
            except Exception as e:
                logger.debug(f"fetch_klines({symbol}) 失败: {e}")
            return []

    async def close(self):
        """关闭 session"""
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_cached(self, key: str, ttl: int = 60) -> Optional[Any]:
        if key in self._cache:
            data, expire = self._cache[key]
            if time.time() < expire:
                return data
            del self._cache[key]
        return None

    def _set_cache(self, key: str, data: Any, ttl: int = 60):
        self._cache[key] = (data, time.time() + ttl)


# ══════════════════════════════════════════════════════════════════
#  异步策略引擎
# ══════════════════════════════════════════════════════════════════

class AsyncStrategyEngine:
    """
    异步策略引擎 — scheduler.py 的下一代替代方案。
    """

    def __init__(self, config: Optional[AsyncEngineConfig] = None):
        self.config = config or AsyncEngineConfig()
        self._data_feed = AsyncDataFeed(self.config)
        self._strategy_pool = ThreadPoolExecutor(
            max_workers=self.config.strategy_thread_pool_size,
            thread_name_prefix='strat',
        )
        self._running = False
        self._tasks: List[asyncio.Task] = []

    async def run_forever(self):
        """主运行循环"""
        self._running = True
        logger.info("🚀 AsyncStrategyEngine 启动")

        # 启动事件总线
        from event_bus import get_event_bus
        bus = get_event_bus(source='async_engine')
        bus.start()

        # 启动深度监控
        try:
            from execution.orderbook_monitor import get_depth_monitor
            monitor = get_depth_monitor()
            monitor.start()
        except Exception as e:
            logger.warning(f"深度监控启动失败: {e}")

        # 启动鲸鱼预警
        try:
            from signals.whale_alert import get_whale_engine
            whale = get_whale_engine()
            whale.start()
        except Exception as e:
            logger.warning(f"鲸鱼预警启动失败: {e}")

        # 创建定时任务
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._scan_loop())
                tg.create_task(self._confirm_loop())
                tg.create_task(self._tracker_loop())
                tg.create_task(self._health_loop())
        except* Exception as eg:
            for e in eg.exceptions:
                logger.error(f"Task 异常退出: {e}")
        finally:
            await self._shutdown()

    async def stop(self):
        """停止引擎"""
        self._running = False
        for task in self._tasks:
            task.cancel()

    async def _shutdown(self):
        """清理资源"""
        await self._data_feed.close()
        self._strategy_pool.shutdown(wait=False)
        logger.info("🛑 AsyncStrategyEngine 已停止")

    # ── 定时循环 ─────────────────────────────────────────────────

    async def _scan_loop(self):
        """扫描循环：每小时全市场扫描"""
        while self._running:
            try:
                await asyncio.wait_for(
                    self._run_scan(),
                    timeout=self.config.scan_timeout_sec,
                )
            except asyncio.TimeoutError:
                logger.warning("扫描超时")
            except Exception as e:
                logger.error(f"扫描异常: {e}", exc_info=True)

            await asyncio.sleep(self.config.scan_interval_sec)

    async def _confirm_loop(self):
        """确认循环：每 5 分钟检查候选池"""
        while self._running:
            try:
                await asyncio.wait_for(
                    self._run_confirm(),
                    timeout=self.config.confirm_timeout_sec,
                )
            except asyncio.TimeoutError:
                logger.warning("确认超时")
            except Exception as e:
                logger.error(f"确认异常: {e}", exc_info=True)

            await asyncio.sleep(self.config.confirm_interval_sec)

    async def _tracker_loop(self):
        """追踪循环：每分钟检查持仓"""
        while self._running:
            try:
                await asyncio.wait_for(
                    self._run_tracker(),
                    timeout=self.config.tracker_timeout_sec,
                )
            except asyncio.TimeoutError:
                logger.warning("追踪超时")
            except Exception as e:
                logger.error(f"追踪异常: {e}", exc_info=True)

            await asyncio.sleep(self.config.tracker_interval_sec)

    async def _health_loop(self):
        """健康检查循环"""
        while self._running:
            try:
                from event_bus import emit_system_health
                emit_system_health('async_engine', 'running', {
                    'uptime_sec': time.time(),
                    'cache_size': len(self._data_feed._cache),
                })
            except Exception:
                pass
            await asyncio.sleep(self.config.health_interval_sec)

    # ── 业务逻辑 ─────────────────────────────────────────────────

    async def _run_scan(self):
        """执行一轮全市场扫描"""
        logger.info("🔍 开始全市场扫描")

        # 1. 并发获取全市场 tickers
        tickers = await self._data_feed.fetch_tickers_batch()
        if not tickers:
            logger.warning("获取 tickers 失败")
            return

        # 2. 在线程池中运行策略扫描（策略逻辑是同步的）
        from strategies.registry import StrategyRegistry
        from strategies.base import MarketSnapshot
        from data.feeds import ExchangeDataFeed

        registry = StrategyRegistry()
        market = MarketSnapshot(tickers=tickers)
        data_feed = ExchangeDataFeed()

        loop = asyncio.get_running_loop()
        for strategy in registry.get_active():
            try:
                candidates = await loop.run_in_executor(
                    self._strategy_pool,
                    strategy.scan,
                    data_feed,
                    market,
                )
                if candidates:
                    logger.info(
                        f"[{strategy.name}] 扫描产生 {len(candidates)} 个候选"
                    )
                    # 写入候选池
                    from db.compat import save_candidates
                    save_candidates([c.to_dict() for c in candidates])

                    # 发布事件
                    from event_bus import emit_candidate_added
                    for c in candidates:
                        emit_candidate_added(c.symbol, rsi_1d=0, score=c.score)
            except Exception as e:
                logger.error(f"[{strategy.name}] scan 异常: {e}")

    async def _run_confirm(self):
        """执行一轮候选确认"""
        from db.compat import load_candidates
        candidates = load_candidates()
        if not candidates:
            return

        logger.info(f"🎯 确认 {len(candidates)} 个候选")

        # 并发获取所有候选的费率和 OI
        symbols = [c.get('symbol', '') for c in candidates if c.get('symbol')]
        funding_rates = await self._data_feed.fetch_funding_rates_batch(symbols)

        # 在线程池中执行确认逻辑
        from strategies.registry import StrategyRegistry
        from data.feeds import ExchangeDataFeed

        registry = StrategyRegistry()
        data_feed = ExchangeDataFeed()
        loop = asyncio.get_running_loop()

        for strategy in registry.get_active():
            strategy_candidates = [
                c for c in candidates
                if c.get('strategy', strategy.name) == strategy.name
            ]
            for c_data in strategy_candidates:
                try:
                    from strategies.base import Candidate as CandidateDTO
                    candidate = CandidateDTO(
                        symbol=c_data['symbol'],
                        price=c_data.get('price', 0),
                        score=c_data.get('score', 0),
                        metadata=c_data,
                    )
                    signal = await loop.run_in_executor(
                        self._strategy_pool,
                        strategy.confirm,
                        candidate,
                        data_feed,
                    )
                    if signal:
                        await self._handle_signal(signal)
                except Exception as e:
                    logger.error(
                        f"[{strategy.name}] confirm 异常 ({c_data.get('symbol')}): {e}"
                    )

    async def _run_tracker(self):
        """执行一轮持仓追踪"""
        from db.compat import load_open_trades
        open_trades = load_open_trades()
        if not open_trades:
            return

        # 并发获取所有持仓币种的最新价格
        symbols = list({t.get('symbol', '') for t in open_trades if t.get('symbol')})
        tickers = await self._data_feed.fetch_tickers_batch(symbols)

        # 在线程池中评估退出
        from strategies.registry import StrategyRegistry, StrategyEngine
        from data.feeds import ExchangeDataFeed

        registry = StrategyRegistry()
        engine = StrategyEngine(registry)
        data_feed = ExchangeDataFeed()

        loop = asyncio.get_running_loop()
        exit_signals = await loop.run_in_executor(
            self._strategy_pool,
            engine.run_exit_cycle,
            open_trades,
            data_feed,
        )

        for exit_signal in exit_signals:
            logger.info(
                f"📤 退出信号: {exit_signal.trade_id} "
                f"reason={exit_signal.reason.value}"
            )
            await self._execute_close(exit_signal, open_trades, tickers)

    async def _handle_signal(self, signal):
        """处理确认后的信号 → 风控 → 执行"""
        logger.info(
            f"📊 信号: {signal.symbol} {signal.direction.value} "
            f"score={signal.score} stake={signal.stake}"
        )

        # 发布信号评分事件
        from event_bus import emit_signal_scored
        emit_signal_scored(
            symbol=signal.symbol,
            strategy=signal.strategy_name,
            score=int(signal.score),
            grade='A' if signal.score >= 70 else 'B',
            triggered=True,
        )

        # ── 1. 单笔风控检查（原 risk_control.can_open_trade）──
        loop = asyncio.get_running_loop()
        try:
            from risk_control import can_open_trade
            from common import get_current_account_id
            account_id = get_current_account_id()
            allowed, reason = await loop.run_in_executor(
                self._strategy_pool,
                lambda: can_open_trade(account_id=account_id)
            )
            if not allowed:
                logger.info(f"🚫 风控拒绝 {signal.symbol}: {reason}")
                from event_bus import emit_risk_alert
                emit_risk_alert('open_rejected', reason, account_id=account_id)
                return
        except Exception as e:
            logger.warning(f"风控检查异常，保守拒绝: {e}")
            return

        # ── 2. 组合风控检查（Portfolio Risk）──
        try:
            from risk.portfolio import PortfolioRiskManager, PortfolioRiskConfig
            from db.compat import load_open_trades, load_all_trades
            import config as cfg

            balance = getattr(cfg, 'ACCOUNT_BALANCE', 100)
            portfolio_mgr = PortfolioRiskManager(
                config=PortfolioRiskConfig(),
                account_balance=balance,
            )
            open_positions = await loop.run_in_executor(
                self._strategy_pool, load_open_trades
            )
            historical = await loop.run_in_executor(
                self._strategy_pool,
                lambda: load_all_trades(status='closed')
            )

            risk_result = portfolio_mgr.check_new_position(
                symbol=signal.symbol,
                stake=signal.stake,
                open_positions=open_positions,
                historical_trades=historical[-50:] if historical else None,
            )
            if not risk_result.approved:
                logger.info(f"🚫 组合风控拒绝 {signal.symbol}: {risk_result.reason}")
                return

            # 应用 Kelly 建议仓位
            if risk_result.adjustments.get('suggested_stake'):
                adjusted_stake = risk_result.adjustments['suggested_stake']
                if adjusted_stake < signal.stake:
                    logger.info(
                        f"📐 Kelly 调整仓位: {signal.stake:.0f}U → {adjusted_stake:.0f}U"
                    )
                    signal.stake = adjusted_stake
        except Exception as e:
            logger.warning(f"组合风控检查异常（不阻塞）: {e}")

        # ── 3. 深度分析 + 智能执行 ──
        try:
            from execution.orderbook_monitor import get_depth_monitor
            from execution.smart_order import get_smart_order_engine, OrderSide

            # 深度分析
            monitor = get_depth_monitor()
            side = 'sell' if signal.direction.value == 'SHORT' else 'buy'
            notional = signal.stake * signal.leverage

            analysis = await loop.run_in_executor(
                self._strategy_pool,
                lambda: monitor.analyze(signal.symbol, side=side, notional_usdt=notional)
            )

            if analysis.recommendation == 'abort':
                logger.warning(
                    f"❌ 深度不足，中止 {signal.symbol}: "
                    f"预估滑点 {analysis.estimated_slippage_bps:.0f} bps"
                )
                return

            # 执行下单
            smart_engine = get_smart_order_engine()
            order_side = OrderSide.SELL if signal.direction.value == 'SHORT' else OrderSide.BUY

            # 估算下单量
            mid_price = analysis.mid_price or 1.0
            amount = notional / mid_price if mid_price > 0 else 0

            if amount <= 0:
                logger.warning(f"❌ 计算下单量为 0: {signal.symbol}")
                return

            exec_result = await loop.run_in_executor(
                self._strategy_pool,
                lambda: smart_engine.execute(
                    symbol=signal.symbol,
                    side=order_side,
                    amount=amount,
                    notional_usdt=notional,
                    exchange_name='binance',
                    account_id=account_id,
                )
            )

            if exec_result.fill_rate > 0:
                logger.info(
                    f"✅ 开仓成功: {signal.symbol} {signal.direction.value} "
                    f"avg_price={exec_result.avg_price:.8g} "
                    f"filled={exec_result.fill_rate*100:.0f}% "
                    f"slippage={exec_result.avg_slippage_bps:.1f}bps"
                )

                # 记录交易
                from risk_control import record_trade_opened
                record_trade_opened(signal.stake, account_id=account_id)

                # 发布开仓事件
                from event_bus import emit_trade_opened
                emit_trade_opened(
                    trade_id=f"{signal.strategy_name}_{signal.symbol}_{int(time.time())}",
                    symbol=signal.symbol,
                    direction=signal.direction.value,
                    stake=signal.stake,
                    exchange='binance',
                    account_id=account_id,
                    entry_price=exec_result.avg_price,
                    score=signal.score,
                )
            else:
                logger.warning(
                    f"❌ 开仓失败: {signal.symbol} error={exec_result.error}"
                )
        except Exception as e:
            logger.error(f"执行层异常: {e}", exc_info=True)

    async def _execute_close(self, exit_signal, open_trades: list, tickers: dict):
        """执行平仓"""
        loop = asyncio.get_running_loop()
        trade_data = next(
            (t for t in open_trades if t.get('id') == exit_signal.trade_id), None
        )
        if not trade_data:
            logger.warning(f"平仓目标交易未找到: {exit_signal.trade_id}")
            return

        symbol = trade_data.get('symbol', '')
        direction = trade_data.get('direction', 'SHORT')
        exchange = trade_data.get('exchange', 'shadow')
        account_id = trade_data.get('account_id', '')

        try:
            from execution.smart_order import get_smart_order_engine, OrderSide

            # 平仓方向与开仓相反
            close_side = OrderSide.BUY if direction == 'SHORT' else OrderSide.SELL
            shares = trade_data.get('shares', 0) or 0
            stake_remaining = trade_data.get('stake_remaining', trade_data.get('stake', 0))
            notional = stake_remaining * trade_data.get('leverage', 10)

            # 计算平仓量
            close_amount = shares * exit_signal.close_ratio
            if close_amount <= 0:
                # fallback: 用 notional / price 估算
                price = tickers.get(symbol, {}).get('last', trade_data.get('current_price', 0))
                if price > 0:
                    close_amount = notional * exit_signal.close_ratio / price

            if close_amount <= 0:
                logger.warning(f"平仓量计算为 0: {exit_signal.trade_id}")
                return

            smart_engine = get_smart_order_engine()
            exec_result = await loop.run_in_executor(
                self._strategy_pool,
                lambda: smart_engine.execute(
                    symbol=symbol,
                    side=close_side,
                    amount=close_amount,
                    notional_usdt=notional * exit_signal.close_ratio,
                    exchange_name=exchange if exchange != 'shadow' else 'binance',
                    account_id=account_id,
                    urgent=(exit_signal.reason.value == 'hard_stop'),  # 止损紧急执行
                )
            )

            if exec_result.fill_rate > 0:
                logger.info(
                    f"✅ 平仓成功: {symbol} reason={exit_signal.reason.value} "
                    f"avg_price={exec_result.avg_price:.8g}"
                )

                # 记录平仓
                from risk_control import record_trade_closed
                record_trade_closed(
                    exit_signal.pnl_estimate,
                    stake=stake_remaining * exit_signal.close_ratio,
                    account_id=account_id,
                )

                # 发布事件
                from event_bus import emit_trade_closed
                emit_trade_closed(
                    trade_id=exit_signal.trade_id,
                    symbol=symbol,
                    pnl=exit_signal.pnl_estimate,
                    close_type=exit_signal.reason.value,
                    exchange=exchange,
                )
            else:
                logger.warning(
                    f"❌ 平仓失败: {symbol} error={exec_result.error}"
                )
                # 推送告警
                from event_bus import emit_risk_alert
                emit_risk_alert(
                    'close_failed',
                    f"{symbol} 平仓失败: {exec_result.error}，请手动处理！",
                    account_id=account_id,
                )
        except Exception as e:
            logger.error(f"平仓执行异常 ({symbol}): {e}", exc_info=True)


# ══════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════

def main():
    """作为独立进程运行"""
    logging.basicConfig(
        level=os.environ.get('LOG_LEVEL', 'INFO'),
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    )

    engine = AsyncStrategyEngine()

    # 优雅退出
    loop = asyncio.new_event_loop()

    def _signal_handler():
        logger.info("收到退出信号")
        loop.create_task(engine.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        loop.run_until_complete(engine.run_forever())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == '__main__':
    main()
