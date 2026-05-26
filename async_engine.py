#!/usr/bin/env python3
"""
异步策略引擎 v2.0 — 生产级调度器（完全替代 scheduler.py）

架构升级：
  - asyncio + aiohttp 并发 IO（50 币候选确认 ~30s vs 旧版 6 分钟）
  - 包含 scheduler.py 的全部初始化序列（journal 恢复、风控对账、配置校验）
  - 包含全部周期运维任务（持仓对账、交易所同步、健康审计、日报、归档）
  - 完整风控守卫（宏观过滤、冷却期、portfolio VaR、runtime_config 热加载）
  - fallback 机制：新引擎异常自动回退旧路径
  - 优雅退出：SIGINT/SIGTERM → 清理资源

启动：
  python3 async_engine.py
  # docker-compose 中作为默认 scheduler 服务运行

环境变量：
  USE_NEW_ENGINE=true    启用新策略引擎路径（默认 true）
  LOG_LEVEL=INFO         日志级别
  REDIS_URL=redis://...  事件总线 Redis 地址
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import setup_logger, log_execution_event

logger = setup_logger("async_engine")



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
    reconcile_interval_sec: float = 600.0   # 持仓对账（每 10 分钟）
    exchange_sync_interval_sec: float = 300.0  # 交易所状态同步（每 5 分钟）
    macro_interval_sec: float = 7200.0      # 宏观数据采集（每 2 小时）
    config_reload_interval_sec: float = 30.0  # 配置热加载（每 30 秒）

    # 并发
    max_concurrent_api_calls: int = 10
    strategy_thread_pool_size: int = 8      # 从 4 提升到 8（支持 50+ 币）
    api_semaphore_limit: int = 8

    # 超时
    scan_timeout_sec: float = 480.0
    confirm_timeout_sec: float = 120.0
    tracker_timeout_sec: float = 60.0

    # aiohttp
    aiohttp_timeout_sec: float = 10.0
    aiohttp_max_connections: int = 30       # 从 20 提升到 30

    # 缓存
    cache_max_entries: int = 500            # AsyncDataFeed 缓存最大条目数（LRU 淘汰）


# 全局开关
USE_NEW_ENGINE = os.environ.get('USE_NEW_ENGINE', 'true').lower() in ('1', 'true', 'yes')



# ══════════════════════════════════════════════════════════════════
#  异步数据馈送
# ══════════════════════════════════════════════════════════════════

class AsyncDataFeed:
    """
    异步数据馈送 — aiohttp 并发获取交易所数据。
    50 币的 K 线/OI/费率并发请求 ~3s（vs ccxt 串行 ~50s）。
    """

    def __init__(self, config: Optional[AsyncEngineConfig] = None):
        self._config = config or AsyncEngineConfig()
        self._session = None
        self._cache: Dict[str, tuple] = {}
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=self._config.aiohttp_timeout_sec)
            connector = aiohttp.TCPConnector(
                limit=self._config.aiohttp_max_connections,
                ttl_dns_cache=300,
            )
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self._session

    async def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._config.api_semaphore_limit)
        return self._semaphore

    async def fetch_tickers_batch(self, symbols: Optional[List[str]] = None) -> Dict[str, Dict]:
        """批量获取全市场 tickers（单次 API 调用）"""
        cache_key = "tickers:all"
        cached = self._get_cached(cache_key, ttl=30)
        if cached is not None:
            return {k: v for k, v in cached.items() if k in symbols} if symbols else cached

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
                        return {k: v for k, v in result.items() if k in symbols} if symbols else result
            except Exception as e:
                logger.debug(f"fetch_tickers_batch 失败: {e}")
            return {}


    async def fetch_funding_rates_batch(self, symbols: List[str]) -> Dict[str, float]:
        """批量获取资金费率（单次 API 调用拿全量）"""
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
                                    result[formatted] = float(item.get('lastFundingRate', 0)) * 100
                        return result
            except Exception as e:
                logger.debug(f"fetch_funding_rates_batch 失败: {e}")
            return {}

    async def fetch_klines(self, symbol: str, timeframe: str = '1h', limit: int = 50) -> List[List[float]]:
        """异步获取 K 线"""
        cache_key = f"klines:{symbol}:{timeframe}:{limit}"
        cached = self._get_cached(cache_key, ttl=60)
        if cached is not None:
            return cached

        sem = await self._get_semaphore()
        async with sem:
            session = await self._get_session()
            ws_symbol = symbol.replace('/USDT', 'USDT').replace('/', '')
            url = f"https://fapi.binance.com/fapi/v1/klines?symbol={ws_symbol}&interval={timeframe}&limit={limit}"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        raw = await resp.json()
                        result = [[k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in raw]
                        self._set_cache(cache_key, result, ttl=60)
                        return result
            except Exception as e:
                logger.debug(f"fetch_klines({symbol}) 失败: {e}")
            return []

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_cached(self, key: str, ttl: int = 60) -> Optional[Any]:
        if key in self._cache:
            data, expire = self._cache[key]
            if time.time() < expire:
                # LRU：命中时移到末尾（Python 3.7+ dict 保持插入序）
                del self._cache[key]
                self._cache[key] = (data, expire)
                return data
            del self._cache[key]
        return None

    def _set_cache(self, key: str, data: Any, ttl: int = 60):
        # 超过 maxsize 时淘汰最旧的条目（dict 头部 = 最久未访问）
        max_entries = self._config.cache_max_entries
        while len(self._cache) >= max_entries:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
        self._cache[key] = (data, time.time() + ttl)



# ══════════════════════════════════════════════════════════════════
#  启动初始化（从 scheduler.py main_loop 移植）
# ══════════════════════════════════════════════════════════════════

def _run_startup_sequence():
    """
    同步启动序列 — 在 asyncio loop 启动前执行。
    包含 scheduler.py 的全部初始化逻辑，保证与旧版行为完全一致。
    """
    logger.info("=== 异步策略引擎 v2.0 启动 ===")
    logger.info(f"  引擎模式: {'新引擎' if USE_NEW_ENGINE else '旧引擎(fallback)'}")

    # 1. 事件系统 + YAML 配置注入
    try:
        from event_integration import init_event_system
        init_event_system('async_engine')
    except Exception as e:
        logger.warning(f"事件系统初始化失败（不影响核心业务）: {e}")

    # 2. runtime_config 首次加载
    try:
        from runtime_config import apply_overrides
        apply_overrides(force=True)
    except Exception as e:
        logger.debug(f"启动 apply_overrides 失败（非致命）: {e}")

    # 3. Journal 恢复（幽灵仓位检测）
    try:
        from journal_recovery import recover_inflight
        stats = recover_inflight()
        if stats.get('ghost', 0) > 0:
            logger.critical(f"🚨 启动发现 {stats['ghost']} 个幽灵订单，需人工处理（详见 TG）")
    except Exception as e:
        logger.error(f"journal 恢复异常（非致命，跳过）: {e}")

    # 4. 风控对账（所有账户）
    try:
        from risk_control import reconcile_risk_state
        from common import get_all_trading_account_ids, get_current_account_id
        account_ids = set()
        active_id = get_current_account_id()
        if active_id:
            account_ids.add(active_id)
        for acc_id in get_all_trading_account_ids():
            account_ids.add(acc_id)

        if not account_ids:
            diff = reconcile_risk_state(notify=True)
            if diff:
                logger.warning(f"启动对账修正了 {len(diff)} 项风控字段")
            else:
                logger.info("启动对账：风控状态一致 ✅")
        else:
            total_diffs = 0
            for acc_id in account_ids:
                diff = reconcile_risk_state(account_id=acc_id, notify=True)
                if diff:
                    total_diffs += len(diff)
            if total_diffs == 0:
                logger.info(f"启动对账：所有 {len(account_ids)} 个账户风控状态一致 ✅")
            else:
                logger.warning(f"启动对账：共修正 {total_diffs} 项偏差")
    except Exception as e:
        logger.error(f"启动对账异常: {e}")


    # 5. 配置一致性校验（所有账号）— M3: ERROR 触发 SAFE_MODE
    try:
        from runtime_config import validate_cross_field_consistency, load_account_overrides, load_global_overrides
        from common import send_tg, tg_escape
        from admin_secrets import list_accounts
        from safe_mode import set_safe_mode, get_safe_mode_info, safe_mode_reason_text

        # 启动前若 SAFE_MODE 已激活，提醒运维（不自动清除）
        existing_sm = get_safe_mode_info()
        if existing_sm:
            logger.error(
                f"🚫 检测到 SAFE_MODE 已激活（{existing_sm.get('source', '')}），"
                f"开仓被全局拒绝直到手动清除。详情：\n{safe_mode_reason_text()}"
            )
            send_tg(
                "🚫 <b>启动检测到 SAFE_MODE 激活</b>\n\n"
                f"<pre>{tg_escape(safe_mode_reason_text())}</pre>\n\n"
                "处理：从 admin panel 修复配置后清除标记。"
            )

        global_over = load_global_overrides()
        accounts = list_accounts() or []
        all_errors, all_warnings = [], []

        if not accounts:
            errs, warns = validate_cross_field_consistency({})
            all_errors.extend([f"(默认) {e}" for e in errs])
            all_warnings.extend([f"(默认) {w}" for w in warns])
        else:
            for acc in accounts:
                acc_id = acc['id']
                acc_name = acc.get('name', acc_id)
                acc_over = load_account_overrides(acc_id) or {}
                merged = {**global_over, **acc_over}
                errs, warns = validate_cross_field_consistency(merged, account_id=acc_id)
                all_errors.extend([f"[{acc_name}] {e}" for e in errs])
                all_warnings.extend([f"[{acc_name}] {w}" for w in warns])

        if all_errors:
            logger.error(f"启动配置一致性 ERROR:\n" + "\n".join(f"• {e}" for e in all_errors))
            # M3: 激活 SAFE_MODE（写文件，跨进程生效）
            set_safe_mode(
                reason=f"启动配置一致性校验失败（{len(all_errors)} 项 ERROR）",
                errors=all_errors,
                source='startup',
            )
            send_tg(
                "🚫 <b>启动配置一致性致命错误 → SAFE_MODE 激活</b>\n\n"
                "开仓将被全局拒绝直到从 admin panel 修复并清除 SAFE_MODE。\n\n"
                + "\n".join(f"• {tg_escape(e)}" for e in all_errors[:10])
                + (f"\n\n... 还有 {len(all_errors) - 10} 项" if len(all_errors) > 10 else "")
            )
        if all_warnings:
            logger.warning(f"启动配置一致性 WARNING:\n" + "\n".join(f"• {w}" for w in all_warnings))
    except Exception as e:
        logger.debug(f"配置一致性校验异常（非致命）: {e}")

    # 6. 启动快速预筛 WebSocket
    try:
        from hot_scanner import start_hot_scanner_thread
        start_hot_scanner_thread()
    except Exception as e:
        logger.warning(f"快速预筛启动失败（非致命）: {e}")

    # 7. 启动 TG Bot
    try:
        from tg_bot import start_bot_thread
        start_bot_thread()
    except Exception as e:
        logger.warning(f"TG Bot 启动失败（非致命）: {e}")

    logger.info("=== 启动初始化完成 ===")



# ══════════════════════════════════════════════════════════════════
#  主引擎类
# ══════════════════════════════════════════════════════════════════

class AsyncStrategyEngine:
    """
    生产级异步策略引擎 v2.0 — 完全替代 scheduler.py。

    包含：
      - 全部启动初始化（journal 恢复、风控对账、配置校验）
      - 核心策略循环（scan / confirm / tracker）
      - 全部周期运维任务（对账 / 审计 / 日报 / 归档）
      - 风控守卫（宏观过滤、冷却期、VaR、config 热加载）
      - 异常 fallback（新引擎失败回退旧引擎）
    """

    def __init__(self, config: Optional[AsyncEngineConfig] = None):
        self.config = config or AsyncEngineConfig()
        self._data_feed = AsyncDataFeed(self.config)
        self._strategy_pool = ThreadPoolExecutor(
            max_workers=self.config.strategy_thread_pool_size,
            thread_name_prefix='strat',
        )
        self._running = False

    async def run_forever(self):
        """主运行循环"""
        self._running = True

        # 启动深度监控
        try:
            from execution.orderbook_monitor import get_depth_monitor
            monitor = get_depth_monitor()
            monitor.start()
            logger.info("📊 OrderBook 深度监控已启动（max_symbols=50）")
        except Exception as e:
            logger.warning(f"深度监控启动失败（非致命）: {e}")

        # 启动鲸鱼预警
        try:
            from signals.whale_alert import get_whale_engine
            whale = get_whale_engine()
            whale.start()
        except Exception as e:
            logger.warning(f"鲸鱼预警启动失败（非致命）: {e}")

        logger.info("🚀 异步策略引擎主循环启动")

        try:
            async with asyncio.TaskGroup() as tg:
                # 核心策略循环
                tg.create_task(self._scan_loop())
                tg.create_task(self._confirm_loop())
                tg.create_task(self._tracker_loop())
                # 运维任务
                tg.create_task(self._config_reload_loop())
                tg.create_task(self._reconcile_loop())
                tg.create_task(self._exchange_sync_loop())
                tg.create_task(self._health_audit_loop())
                tg.create_task(self._macro_collection_loop())
                tg.create_task(self._journal_recovery_loop())
                tg.create_task(self._close_retry_loop())
                tg.create_task(self._daily_tasks_loop())
        except* Exception as eg:
            for e in eg.exceptions:
                logger.error(f"Task 异常退出: {e}")
        finally:
            await self._shutdown()

    async def stop(self):
        self._running = False

    async def _shutdown(self):
        await self._data_feed.close()
        # H-3 修复：wait=True 给正在执行的策略任务（如开仓流程）一个优雅退出窗口，
        # 避免 wait=False 导致 worker 线程仍在运行时进程退出 → journal pending 永不 confirm。
        # cancel_futures=True (Python 3.9+) 取消尚未开始的排队任务，只等已在运行的。
        self._strategy_pool.shutdown(wait=True, cancel_futures=True)
        logger.info("🛑 异步策略引擎已停止")



    # ══════════════════════════════════════════════════════════════
    #  核心策略循环
    # ══════════════════════════════════════════════════════════════

    async def _scan_loop(self):
        """每小时全市场扫描"""
        while self._running:
            try:
                await asyncio.wait_for(self._run_scan(), timeout=self.config.scan_timeout_sec)
            except asyncio.TimeoutError:
                logger.warning("扫描超时")
            except Exception as e:
                logger.error(f"扫描异常: {e}", exc_info=True)
            await asyncio.sleep(self.config.scan_interval_sec)

    async def _confirm_loop(self):
        """候选确认（可配置间隔，默认 5 分钟）"""
        while self._running:
            try:
                await asyncio.wait_for(self._run_confirm(), timeout=self.config.confirm_timeout_sec)
            except asyncio.TimeoutError:
                logger.warning("确认超时")
            except Exception as e:
                logger.error(f"确认异常: {e}", exc_info=True)
            # 从 config 动态读取间隔
            import config as cfg
            interval = max(60, int(getattr(cfg, 'CHECK_CANDIDATES_INTERVAL_MINUTES', 5)) * 60)
            await asyncio.sleep(interval)

    async def _tracker_loop(self):
        """每分钟持仓追踪"""
        while self._running:
            try:
                await asyncio.wait_for(self._run_tracker(), timeout=self.config.tracker_timeout_sec)
            except asyncio.TimeoutError:
                logger.warning("追踪超时")
            except Exception as e:
                logger.error(f"追踪异常: {e}", exc_info=True)
            await asyncio.sleep(self.config.tracker_interval_sec)

    # ══════════════════════════════════════════════════════════════
    #  运维任务循环
    # ══════════════════════════════════════════════════════════════

    async def _config_reload_loop(self):
        """每 30s 热加载 runtime_config"""
        while self._running:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._strategy_pool, self._reload_config)
            except Exception as e:
                logger.debug(f"config 热加载异常: {e}")
            await asyncio.sleep(self.config.config_reload_interval_sec)

    async def _reconcile_loop(self):
        """每 10 分钟持仓对账 + 风控对账"""
        while self._running:
            await asyncio.sleep(self.config.reconcile_interval_sec)
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._strategy_pool, self._run_position_reconcile)
            except Exception as e:
                logger.warning(f"持仓对账异常: {e}")

    async def _exchange_sync_loop(self):
        """每 5 分钟同步交易所条件单状态"""
        while self._running:
            await asyncio.sleep(self.config.exchange_sync_interval_sec)
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._strategy_pool, self._run_exchange_sync)
            except Exception as e:
                logger.warning(f"交易所同步异常: {e}")

    async def _health_audit_loop(self):
        """每 15 分钟全账号健康审计"""
        while self._running:
            await asyncio.sleep(900)  # 15 分钟
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._strategy_pool, self._run_health_audit)
            except Exception as e:
                logger.warning(f"健康审计异常: {e}")

    async def _macro_collection_loop(self):
        """每 2 小时宏观数据采集"""
        while self._running:
            await asyncio.sleep(self.config.macro_interval_sec)
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._strategy_pool, self._run_macro_collection)
            except Exception as e:
                logger.warning(f"宏观采集异常: {e}")

    async def _journal_recovery_loop(self):
        """每 30 分钟扫描 in-flight journal，检测幽灵仓位

        补充启动时的一次性扫描：覆盖"运行中崩溃后自动恢复"的场景。
        如果发现 pending 条目超过 30 分钟仍未 confirm，说明下单进程可能
        已经崩溃但订单在交易所端成交了 → 触发告警。
        """
        while self._running:
            await asyncio.sleep(1800)  # 30 分钟
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._strategy_pool, self._run_journal_recovery)
            except Exception as e:
                logger.warning(f"Journal recovery 异常: {e}")

    async def _close_retry_loop(self):
        """每 5 分钟主动扫描 close_retry_pending 标记，重试平仓 (H4 修复)

        场景：_perform_exchange_close 重试 3 次后失败会标记 close_retry_pending=True;
        旧版只靠下一次 tracker.run() 兜底处理(60s 一次),但当 tracker 间隔较长或主循环
        异常时,这些悬仓会长时间没人管。专门的 worker 每 5 分钟主动扫描一次,直到成功或
        持续失败超过 12 次（1h）后升级 critical 告警让运维介入。
        """
        while self._running:
            await asyncio.sleep(300)  # 5 分钟
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._strategy_pool, self._run_close_retry)
            except Exception as e:
                logger.warning(f"close_retry 异常: {e}")

    async def _daily_tasks_loop(self):
        """每日任务：日报(08:00)、归档(00:01)、周优化(周一09:00)"""
        while self._running:
            await asyncio.sleep(60)  # 每分钟检查一次
            now = datetime.now(timezone.utc)
            loop = asyncio.get_running_loop()
            # 日报 08:00 UTC
            if now.hour == 8 and now.minute == 0:
                try:
                    await loop.run_in_executor(self._strategy_pool, self._run_daily_report)
                except Exception as e:
                    logger.warning(f"日报异常: {e}")
            # 归档 00:01 UTC
            if now.hour == 0 and now.minute == 1:
                try:
                    await loop.run_in_executor(self._strategy_pool, self._run_archive)
                except Exception as e:
                    logger.warning(f"归档异常: {e}")
            # 周优化 周一 09:00 UTC
            if now.weekday() == 0 and now.hour == 9 and now.minute == 0:
                try:
                    await loop.run_in_executor(self._strategy_pool, self._run_auto_optimize)
                except Exception as e:
                    logger.warning(f"自动优化异常: {e}")



    # ══════════════════════════════════════════════════════════════
    #  核心业务逻辑（含 fallback）
    # ══════════════════════════════════════════════════════════════

    async def _run_scan(self):
        """全市场扫描 — 新引擎 + fallback"""
        if not USE_NEW_ENGINE:
            await asyncio.get_running_loop().run_in_executor(
                self._strategy_pool, self._fallback_scan)
            return

        t0 = time.monotonic()
        try:
            tickers = await self._data_feed.fetch_tickers_batch()
            if not tickers:
                logger.warning("获取 tickers 失败，fallback 到旧引擎")
                await asyncio.get_running_loop().run_in_executor(
                    self._strategy_pool, self._fallback_scan)
                return

            loop = asyncio.get_running_loop()
            # 策略扫描在线程池中执行（策略逻辑是同步的）
            candidates = await loop.run_in_executor(
                self._strategy_pool, self._sync_scan_with_tickers, tickers)

            elapsed = time.monotonic() - t0
            logger.info(f"[新引擎] 扫描完成: {len(candidates)} 个候选 | 耗时 {elapsed:.2f}s")
        except Exception as e:
            logger.error(f"[新引擎] scan 异常，fallback: {e}", exc_info=True)
            await asyncio.get_running_loop().run_in_executor(
                self._strategy_pool, self._fallback_scan)

    async def _run_confirm(self):
        """候选确认 — 含宏观过滤 + 冷却期 + 风控 + fallback"""
        if not USE_NEW_ENGINE:
            await asyncio.get_running_loop().run_in_executor(
                self._strategy_pool, self._fallback_confirm)
            return

        t0 = time.monotonic()
        try:
            loop = asyncio.get_running_loop()

            # 宏观过滤（线程池内执行，因为可能调 API）
            macro_result = await loop.run_in_executor(
                self._strategy_pool, self._check_macro_filter)
            if macro_result and not macro_result.get('allowed', True):
                logger.warning(f"[新引擎] 宏观过滤暂停做空: {macro_result.get('reason', '')}")
                from common import send_tg
                send_tg(f"🚫 <b>宏观过滤：暂停做空</b>\n\n{macro_result.get('reason', '')}")
                return

            # 加载候选池
            from db.compat import load_candidates
            candidates = load_candidates()
            if not candidates:
                return

            logger.info(f"[新引擎] 确认 {len(candidates)} 个候选")

            # 并发获取费率（async 优势所在）
            symbols = [c.get('symbol', '') for c in candidates if c.get('symbol')]
            funding_rates = await self._data_feed.fetch_funding_rates_batch(symbols)

            # 策略确认 + 风控 + 开仓（线程池内串行执行，保留锁安全性）
            opened = await loop.run_in_executor(
                self._strategy_pool, self._sync_confirm_with_data, candidates, funding_rates, macro_result)

            elapsed = time.monotonic() - t0
            logger.info(f"[新引擎] 候选确认完成: {opened} 笔开仓 | 耗时 {elapsed:.2f}s")
        except Exception as e:
            logger.error(f"[新引擎] confirm 异常，fallback: {e}", exc_info=True)
            await asyncio.get_running_loop().run_in_executor(
                self._strategy_pool, self._fallback_confirm)

    async def _run_tracker(self):
        """持仓追踪 — 使用 engine_adapter.run_exit（已含完整平仓逻辑）"""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._strategy_pool, self._sync_tracker)
        except Exception as e:
            logger.error(f"追踪异常: {e}")



    # ══════════════════════════════════════════════════════════════
    #  同步桥接层（线程池内执行）
    # ══════════════════════════════════════════════════════════════

    def _sync_scan_with_tickers(self, tickers: dict) -> list:
        """在线程池中执行策略扫描"""
        from engine_adapter import _get_engine, _persist_candidates, _sync_candidates_to_json
        from strategies.base import MarketSnapshot
        from data.feeds import ExchangeDataFeed

        engine = _get_engine()
        feed = ExchangeDataFeed(cache_ttl_sec=30)
        market = MarketSnapshot(tickers=tickers)
        candidates = engine.run_scan_cycle(feed, market)
        if candidates:
            _persist_candidates(candidates)
            _sync_candidates_to_json(candidates)
        return candidates

    def _sync_confirm_with_data(self, candidates: list, funding_rates: dict, macro_result: dict) -> int:
        """在线程池中执行完整的确认 + 风控 + 开仓流程"""
        from engine_adapter import _get_engine, _get_executor, _get_portfolio_risk, _record_trade_opened
        from db.compat import load_open_trades
        from risk_control import can_open_trade, is_in_cooldown, record_trade_opened as risk_record
        from data.feeds import ExchangeDataFeed

        engine = _get_engine()
        executor = _get_executor()
        portfolio_risk = _get_portfolio_risk()
        feed = ExchangeDataFeed(cache_ttl_sec=30)

        signals = engine.run_confirm_cycle(candidates, feed)
        if not signals:
            return 0

        open_trades = load_open_trades()
        opened_count = 0
        _stake_mult = macro_result.get('stake_multiplier', 1.0) if macro_result else 1.0
        _score_bonus = macro_result.get('score_bonus', 0) if macro_result else 0

        for signal in signals:
            # 1. 冷却期
            in_cd, cd_reason = is_in_cooldown(signal.symbol)
            if in_cd:
                logger.info(f"  🚫 冷却期 {signal.symbol}: {cd_reason}")
                continue

            # 2. 单笔风控
            allowed, reason = can_open_trade(stake=signal.stake)
            if not allowed:
                logger.info(f"  🚫 风控拒绝 {signal.symbol}: {reason}")
                continue

            # 3. 组合风控
            check = portfolio_risk.check_new_position(
                symbol=signal.symbol, stake=signal.stake, open_positions=open_trades)
            if not check.approved:
                logger.info(f"  🚫 组合风控拒绝 {signal.symbol}: {check.reason}")
                continue

            # 4. Kelly 仓位调整
            if 'suggested_stake' in check.adjustments:
                suggested = check.adjustments['suggested_stake']
                if suggested < signal.stake:
                    signal.stake = suggested

            # 5. 宏观调节
            if _stake_mult != 1.0:
                signal.stake = round(signal.stake * _stake_mult)
            if _score_bonus != 0:
                signal.score = max(0, min(100, signal.score + _score_bonus))

            # 6. 执行开仓
            result = executor.execute_signal(signal)
            if result.success:
                opened_count += 1
                _record_trade_opened(signal, result)
                risk_record(stake=signal.stake)
                logger.info(f"  ✅ 开仓 {signal.symbol} | score={signal.score} | stake={signal.stake}U")
            else:
                logger.error(f"  ❌ 开仓失败 {signal.symbol}: {result.error}")

        return opened_count

    def _sync_tracker(self):
        """同步执行持仓追踪（engine_adapter 已含完整 fallback）"""
        from engine_adapter import run_exit
        run_exit(check_only=True)

    def _check_macro_filter(self) -> Optional[dict]:
        """检查宏观过滤"""
        try:
            from macro.filter import check_macro_filter
            result = check_macro_filter()
            return {'allowed': result.allowed, 'reason': result.reason,
                    'stake_multiplier': result.stake_multiplier, 'score_bonus': result.score_bonus}
        except Exception as e:
            logger.debug(f"宏观过滤检查跳过: {e}")
            return {'allowed': True, 'reason': '', 'stake_multiplier': 1.0, 'score_bonus': 0}

    def _reload_config(self):
        """热加载 runtime_config"""
        try:
            from runtime_config import apply_overrides
            apply_overrides()
        except Exception:
            pass



    # ══════════════════════════════════════════════════════════════
    #  运维任务实现（线程池内同步执行）
    # ══════════════════════════════════════════════════════════════

    def _run_position_reconcile(self):
        """持仓对账：Binance/OKX 真实持仓 vs 本地 open trades"""
        try:
            from common import load_json, TRADES_FILE
            from live_executor import get_binance_position_amount
            from live_executor import get_okx_live_exchange

            trades = load_json(TRADES_FILE, [])
            open_trades = [t for t in trades if t.get('status') == 'open' and t.get('exchange') in ('binance', 'okx')]
            if not open_trades:
                return

            by_key = {}
            for t in open_trades:
                ex = t.get('exchange')
                if ex not in ('binance', 'okx'):
                    continue
                key = (ex, t.get('symbol'), t.get('direction', 'SHORT'), t.get('account_id') or '')
                by_key[key] = by_key.get(key, 0.0) + float(t.get('amount') or 0)

            diffs = 0
            for (ex, symbol, direction, account_id), local_amount in by_key.items():
                if local_amount <= 0:
                    continue
                remote_amount = 0.0
                if ex == 'binance':
                    remote_amount = float(get_binance_position_amount(symbol, direction, account_id=account_id or None))
                else:
                    # M-2 修复：使用 get_okx_live_exchange 支持 account_id 路由，
                    # 否则多账户模式下所有 OKX 对账都走默认凭证 → 非默认账户永远报差异
                    okx = get_okx_live_exchange(account_id=account_id or None)
                    if okx:
                        try:
                            poss = okx.fetch_positions([symbol])
                            want = (direction or 'SHORT').upper()
                            for pos in poss:
                                side = str(pos.get('side') or '').lower()
                                contracts = float(pos.get('contracts') or 0)
                                if (want == 'SHORT' and side == 'short') or (want == 'LONG' and side == 'long'):
                                    remote_amount = max(remote_amount, contracts)
                        except Exception:
                            pass

                tol = max(1.0, local_amount * 0.02)
                if abs(local_amount - remote_amount) > tol:
                    diffs += 1
                    log_execution_event('position_reconcile_diff', exchange=ex, symbol=symbol,
                                        direction=direction, account_id=account_id,
                                        local_amount=round(local_amount, 8), remote_amount=round(remote_amount, 8))

            if diffs > 0:
                logger.warning(f"持仓对账发现 {diffs} 处差异")
                from common import send_tg
                send_tg(f"⚠️ <b>持仓对账差异</b>\n\n发现 {diffs} 处差异，请检查 execution_events.jsonl")
        except Exception as e:
            logger.warning(f"持仓对账异常（非致命）: {e}")

    def _run_exchange_sync(self):
        """交易所条件单状态同步"""
        try:
            from common import load_json, LockedJsonFile, TRADES_FILE
            from live_executor import get_live_exchange

            trades = load_json(TRADES_FILE, [])
            live = [t for t in trades if t.get('status') == 'open' and t.get('exchange') == 'binance']
            if not live:
                return

            by_acc = {}
            for t in live:
                aid = t.get('account_id') or ''
                by_acc.setdefault(aid, []).append(t)

            exchange_state = {}
            for aid in by_acc.keys():
                ex = get_live_exchange(aid or None)
                if not ex:
                    continue
                try:
                    ex.load_markets()
                    algo_all = ex.fapiPrivateGetOpenAlgoOrders({})
                    algo_ids = {str(a.get('algoId')) for a in algo_all}
                except Exception:
                    algo_ids = set()
                exchange_state[aid] = algo_ids

            with LockedJsonFile(TRADES_FILE, default=[]) as (raw, save):
                changed = False
                for t in raw:
                    if t.get('status') != 'open' or t.get('exchange') != 'binance':
                        continue
                    aid = t.get('account_id') or ''
                    algo_ids = exchange_state.get(aid, set())
                    if (t.get('protect_stage') == 'stage1' and t.get('protect_tp_algo_id')
                            and str(t.get('protect_tp_algo_id')) not in algo_ids):
                        t['protect_tp_algo_id'] = None
                        t['protect_stop_algo_id'] = None
                        changed = True
                if changed:
                    save(raw)
        except Exception as e:
            logger.warning(f"交易所同步异常（非致命）: {e}")

    def _run_health_audit(self):
        """全账号健康审计"""
        try:
            res = subprocess.run(['python3', 'health_audit.py', '--all', '--tg'],
                                check=False, timeout=300)
            if res.returncode >= 2:
                logger.warning(f"健康审计退出码 {res.returncode}")
        except Exception as e:
            logger.warning(f"健康审计异常: {e}")

    def _run_macro_collection(self):
        """宏观数据采集"""
        try:
            from macro.ms_runner import run_macro_collection
            run_macro_collection()
        except Exception as e:
            logger.warning(f"宏观采集异常: {e}")

    def _run_daily_report(self):
        """日报推送"""
        try:
            from altcoin_tracker import run as tracker_run
            tracker_run(check_only=False)
        except Exception as e:
            logger.warning(f"日报异常: {e}")

    def _run_archive(self):
        """清理过期交易 + journal"""
        try:
            from common import cleanup_old_trades, journal_cleanup_failed
            cleanup_old_trades()
            journal_cleanup_failed(retain_hours=72)
        except Exception as e:
            logger.warning(f"归档异常: {e}")

    def _run_journal_recovery(self):
        """定期扫描 in-flight journal 检测幽灵仓位（补充启动时的一次性扫描）"""
        try:
            from journal_recovery import recover_inflight
            stats = recover_inflight()
            if stats.get('ghost', 0) > 0:
                logger.critical(
                    f"🚨 Journal recovery 发现 {stats['ghost']} 个幽灵订单！"
                    f"（已推送 TG 告警，需人工处理）"
                )
        except Exception as e:
            logger.debug(f"Journal recovery 跳过: {e}")

    def _run_close_retry(self):
        """主动扫描 close_retry_pending,重试平仓 (H4 修复)

        逻辑：
          1. 读 trades.json 找出所有 close_retry_pending=True 的 trade
          2. 对每条 trade 调 _perform_exchange_close 重试（内部已有指数退避 3 次）
          3. 重试成功会清除 close_retry_pending 标记;持续失败累积 retry_attempts 计数
          4. 当某条 retry_attempts >= 12（≈1h）→ 升级 critical TG 告警
        """
        try:
            from common import load_json, TRADES_FILE, LockedJsonFile, send_tg, tg_escape, utcnow_iso
            from models import Trade
            from altcoin_tracker import _perform_exchange_close

            trades_raw = load_json(TRADES_FILE, [])
            stuck = [t for t in trades_raw
                     if t.get('close_retry_pending')
                     and t.get('exchange') != 'shadow'
                     and float(t.get('close_retry_amount', 0) or 0) > 0]

            if not stuck:
                return

            logger.warning(f"🔁 close_retry: {len(stuck)} 条悬仓待重试平仓")

            # 先把 retry_attempts 计数 + 1（在锁内）
            with LockedJsonFile(TRADES_FILE, default=[]) as (raw, save):
                changed = False
                for t in raw:
                    if not t.get('close_retry_pending') or t.get('exchange') == 'shadow':
                        continue
                    t['close_retry_attempts'] = int(t.get('close_retry_attempts', 0) or 0) + 1
                    t['close_retry_last_attempt'] = utcnow_iso()
                    changed = True
                if changed:
                    save(raw)

            # 出锁后重试（_perform_exchange_close 内部抢自己的锁）
            for t in stuck:
                try:
                    trade_obj = Trade.from_dict(t)
                    action = t.get('close_retry_action', 'full_close')
                    amount = float(t.get('close_retry_amount', 0) or 0)
                    _perform_exchange_close(trade_obj, action, amount)
                except Exception as _e:
                    logger.error(f"close_retry 单条异常 {t.get('symbol')}: {_e}")

            # 重试后再读一次,看哪些升级到了 critical 阈值
            try:
                trades_after = load_json(TRADES_FILE, [])
                critical = [
                    t for t in trades_after
                    if t.get('close_retry_pending')
                    and int(t.get('close_retry_attempts', 0) or 0) >= 12
                ]
                if critical:
                    lines = [
                        f"🚨🚨 <b>{len(critical)} 笔平仓持续失败 1h+</b>",
                        "",
                        "下列交易自动重试已超过 12 次（≈1 小时），需立即人工到交易所核对：",
                        "",
                    ]
                    for t in critical[:10]:
                        lines.append(
                            f"• {tg_escape(t.get('symbol', '?'))} "
                            f"({tg_escape(t.get('exchange', '?'))}) "
                            f"动作={tg_escape(t.get('close_retry_action', ''))} "
                            f"重试={t.get('close_retry_attempts', 0)} "
                            f"err={tg_escape(str(t.get('close_retry_last_error', ''))[:80])}"
                        )
                    if len(critical) > 10:
                        lines.append(f"... 还有 {len(critical) - 10} 笔")
                    send_tg("\n".join(lines))
            except Exception as _se:
                logger.debug(f"close_retry critical 告警失败: {_se}")
        except Exception as e:
            logger.warning(f"close_retry 整体异常: {e}", exc_info=True)

    def _run_auto_optimize(self):
        """自动回测优化建议"""
        try:
            from auto_optimize import run_auto_optimize
            run_auto_optimize()
        except Exception as e:
            logger.warning(f"自动优化异常: {e}")



    # ══════════════════════════════════════════════════════════════
    #  Fallback 到旧引擎
    # ══════════════════════════════════════════════════════════════

    def _fallback_scan(self):
        """回退到旧的 altcoin_scanner.scan_daily()"""
        logger.info("[旧引擎 fallback] 执行 scan_daily")
        from altcoin_scanner import scan_daily
        scan_daily()

    def _fallback_confirm(self):
        """回退到旧的 altcoin_scanner.check_candidates()"""
        logger.info("[旧引擎 fallback] 执行 check_candidates")
        from altcoin_scanner import check_candidates
        check_candidates()


# ══════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════

def main():
    """作为独立进程运行"""
    logging.basicConfig(
        level=os.environ.get('LOG_LEVEL', 'INFO'),
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    )

    # 同步启动初始化（journal 恢复、风控对账、配置校验等）
    _run_startup_sequence()

    # 创建引擎并运行
    engine = AsyncStrategyEngine()
    loop = asyncio.new_event_loop()

    def _signal_handler():
        logger.info("收到退出信号，优雅停止...")
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
