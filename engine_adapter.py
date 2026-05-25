#!/usr/bin/env python3
"""
引擎适配器 — 桥接旧 scheduler 与新策略引擎 / 风控 / 执行层。

职责：
  - 初始化 StrategyEngine + DataFeed + OrderExecutor + PortfolioRiskManager
  - 提供 run_scan() / run_confirm() / run_exit() 三个顶层函数
  - scheduler.py 只需调用这三个函数即可切换到新架构
  - 内部做新旧兼容：成功走新路径，异常 fallback 到旧路径

设计原则：
  - 渐进式迁移：旧逻辑作为 fallback 保留，新引擎异常不影响生产
  - 零配置切换：通过 USE_NEW_ENGINE=true 环境变量控制
  - 日志透明：所有新旧切换都有明确日志
"""

import os
import logging
import time
from typing import List, Dict, Optional, Any

logger = logging.getLogger("engine_adapter")

# 全局开关：是否使用新引擎（可通过环境变量或 config 控制）
USE_NEW_ENGINE = os.environ.get('USE_NEW_ENGINE', 'true').lower() in ('1', 'true', 'yes')

# ══════════════════════════════════════════════════════════════════
#  懒加载单例（避免 import 时的循环依赖）
# ══════════════════════════════════════════════════════════════════

_engine = None
_data_feed = None
_executor = None
_portfolio_risk = None


def _get_engine():
    """懒加载 StrategyEngine 单例"""
    global _engine
    if _engine is None:
        from strategies.registry import StrategyRegistry, StrategyEngine
        registry = StrategyRegistry()
        registry.auto_discover()
        _engine = StrategyEngine(registry)
        logger.info(f"StrategyEngine 初始化完成，注册策略: {registry.list_names()}")
    return _engine


def _get_data_feed():
    """懒加载 DataFeed 单例"""
    global _data_feed
    if _data_feed is None:
        from data.feeds import ExchangeDataFeed
        _data_feed = ExchangeDataFeed(cache_ttl_sec=30)
        logger.info("ExchangeDataFeed 初始化完成")
    return _data_feed


def _get_executor():
    """懒加载 OrderExecutor 单例"""
    global _executor
    if _executor is None:
        from execution.executor import OrderExecutor, ExecutionConfig
        import config as cfg
        _executor = OrderExecutor(ExecutionConfig(
            dry_run=not getattr(cfg, 'LIVE_MODE', False),
        ))
        logger.info(f"OrderExecutor 初始化完成 (dry_run={not getattr(cfg, 'LIVE_MODE', False)})")
    return _executor


def _get_portfolio_risk():
    """懒加载 PortfolioRiskManager 单例"""
    global _portfolio_risk
    if _portfolio_risk is None:
        from risk.portfolio import PortfolioRiskManager, PortfolioRiskConfig
        import config as cfg
        balance = getattr(cfg, 'ACCOUNT_BALANCE', 100)
        _portfolio_risk = PortfolioRiskManager(
            config=PortfolioRiskConfig(),
            account_balance=balance,
        )
        logger.info(f"PortfolioRiskManager 初始化完成 (balance={balance}U)")
    return _portfolio_risk


# ══════════════════════════════════════════════════════════════════
#  顶层接口：run_scan
# ══════════════════════════════════════════════════════════════════

def run_scan():
    """
    日线扫描 — 新引擎版本。
    替代原 altcoin_scanner.scan_daily()。

    流程：
      1. 获取全市场 tickers
      2. StrategyEngine.run_scan_cycle() 驱动所有策略扫描
      3. 候选写入 DB (CandidateRepo) + 兼容写入 JSON
    """
    if not USE_NEW_ENGINE:
        _fallback_scan()
        return

    t0 = time.monotonic()
    try:
        engine = _get_engine()
        feed = _get_data_feed()

        # 获取市场快照
        from strategies.base import MarketSnapshot
        tickers = feed.get_tickers()
        if not tickers:
            logger.warning("获取 tickers 失败，fallback 到旧引擎")
            _fallback_scan()
            return

        market = MarketSnapshot(tickers=tickers)

        # 策略扫描
        candidates = engine.run_scan_cycle(feed, market)
        elapsed = time.monotonic() - t0

        logger.info(
            f"[新引擎] 扫描完成: {len(candidates)} 个候选 | "
            f"耗时 {elapsed:.2f}s | 策略: {[s.name for s in engine.registry.get_active()]}"
        )

        # 写入 DB
        _persist_candidates(candidates)

        # 兼容：同步写入旧 JSON 格式
        _sync_candidates_to_json(candidates)

    except Exception as e:
        logger.error(f"[新引擎] run_scan 异常，fallback: {e}", exc_info=True)
        _fallback_scan()


# ══════════════════════════════════════════════════════════════════
#  顶层接口：run_confirm
# ══════════════════════════════════════════════════════════════════

def run_confirm():
    """
    候选确认 — 新引擎版本。
    替代原 altcoin_scanner.check_candidates()。

    流程：
      1. 从 DB/JSON 加载候选池
      2. StrategyEngine.run_confirm_cycle() 确认信号
      3. 信号通过风控（单笔 + 组合）
      4. OrderExecutor 执行开仓
      5. 结果写入 DB + JSON
    """
    if not USE_NEW_ENGINE:
        _fallback_confirm()
        return

    t0 = time.monotonic()
    try:
        engine = _get_engine()
        feed = _get_data_feed()
        executor = _get_executor()
        portfolio_risk = _get_portfolio_risk()

        # 加载候选池
        from db.compat import load_candidates, load_open_trades
        candidates = load_candidates()
        if not candidates:
            logger.info("[新引擎] 候选池为空，跳过")
            return

        # BTC 趋势过滤
        btc_ticker = feed.get_ticker('BTC/USDT')
        btc_pct = btc_ticker.get('percentage', 0) or 0
        import config as cfg
        if getattr(cfg, 'BTC_FILTER_ENABLED', True):
            threshold = getattr(cfg, 'BTC_CRASH_THRESHOLD', -5.0)
            if btc_pct <= threshold:
                logger.warning(f"[新引擎] BTC 24h={btc_pct:.1f}% <= {threshold}%，暂停做空")
                return

        # 策略确认
        signals = engine.run_confirm_cycle(candidates, feed)
        if not signals:
            logger.info(f"[新引擎] 候选确认: 0 个信号触发 (检查了 {len(candidates)} 个候选)")
            return

        logger.info(f"[新引擎] 候选确认: {len(signals)} 个信号触发")

        # 对每个信号：风控 → 执行
        open_trades = load_open_trades()
        opened_count = 0

        for signal in signals:
            # 1. 单笔风控
            from risk_control import can_open_trade
            allowed, reason = can_open_trade(stake=signal.stake)
            if not allowed:
                logger.info(f"  🚫 单笔风控拒绝 {signal.symbol}: {reason}")
                continue

            # 2. 组合风控
            check = portfolio_risk.check_new_position(
                symbol=signal.symbol,
                stake=signal.stake,
                open_positions=open_trades,
            )
            if not check.approved:
                logger.info(f"  🚫 组合风控拒绝 {signal.symbol}: {check.reason}")
                continue

            # 3. Kelly 仓位调整
            if 'suggested_stake' in check.adjustments:
                suggested = check.adjustments['suggested_stake']
                if suggested < signal.stake:
                    logger.info(
                        f"  📊 Kelly 调整 {signal.symbol}: {signal.stake}U → {suggested}U"
                    )
                    signal.stake = suggested

            # 4. 冷却期检查
            from risk_control import is_in_cooldown
            in_cd, cd_reason = is_in_cooldown(signal.symbol)
            if in_cd:
                logger.info(f"  🚫 冷却期 {signal.symbol}: {cd_reason}")
                continue

            # 5. 执行开仓
            result = executor.execute_signal(signal)
            if result.success:
                opened_count += 1
                # 记录开仓到 DB + JSON
                _record_trade_opened(signal, result)
                # 更新风控
                from risk_control import record_trade_opened as risk_record
                risk_record(stake=signal.stake)
                logger.info(
                    f"  ✅ 开仓成功 {signal.symbol} | score={signal.score} | "
                    f"stake={signal.stake}U | {signal.reason}"
                )
            else:
                logger.error(
                    f"  ❌ 开仓失败 {signal.symbol}: {result.error} ({result.error_code})"
                )

        elapsed = time.monotonic() - t0
        logger.info(
            f"[新引擎] 候选确认完成: {opened_count}/{len(signals)} 开仓成功 | "
            f"耗时 {elapsed:.2f}s"
        )

    except Exception as e:
        logger.error(f"[新引擎] run_confirm 异常，fallback: {e}", exc_info=True)
        _fallback_confirm()


# ══════════════════════════════════════════════════════════════════
#  顶层接口：run_exit
# ══════════════════════════════════════════════════════════════════

def run_exit(check_only: bool = True):
    """
    持仓退出检查 — 新引擎版本。
    替代原 altcoin_tracker.run(check_only=True)。

    流程：
      1. 加载所有 open trades
      2. 获取当前价格
      3. StrategyEngine.run_exit_cycle() 评估退出
      4. 触发的退出信号执行平仓
    """
    if not USE_NEW_ENGINE:
        _fallback_exit(check_only)
        return

    t0 = time.monotonic()
    try:
        engine = _get_engine()
        feed = _get_data_feed()

        from db.compat import load_open_trades
        open_trades = load_open_trades()
        if not open_trades:
            return

        # 为每笔交易补充当前价格和浮动盈亏
        for trade in open_trades:
            symbol = trade.get('symbol', '')
            if not symbol:
                continue
            ticker = feed.get_ticker(symbol)
            current_price = ticker.get('last', 0) or 0
            trade['current_price'] = current_price

            entry = trade.get('entry_price', 0)
            if entry > 0 and current_price > 0:
                if trade.get('direction', 'SHORT') == 'SHORT':
                    trade['pnl_pct'] = (entry - current_price) / entry * 100
                else:
                    trade['pnl_pct'] = (current_price - entry) / entry * 100
            else:
                trade['pnl_pct'] = 0.0

            # 持仓时间
            from common import hold_hours as calc_hold_hours
            trade['hold_hours'] = calc_hold_hours(trade.get('opened_at', ''))

            # 更新 best_pnl_pct
            pnl_pct = trade['pnl_pct']
            if pnl_pct > trade.get('best_pnl_pct', 0):
                trade['best_pnl_pct'] = pnl_pct

        # 策略退出评估
        exit_signals = engine.run_exit_cycle(open_trades, feed)

        if not exit_signals:
            return

        logger.info(f"[新引擎] 退出检查: {len(exit_signals)} 个信号触发")

        # 执行平仓（走旧的 altcoin_tracker 逻辑，因为涉及 TP1 半仓等复杂状态管理）
        # 这里只通知旧模块"该平仓了"，具体执行由 tracker 负责
        for exit_sig in exit_signals:
            logger.info(
                f"  📤 退出信号: trade={exit_sig.trade_id} | "
                f"reason={exit_sig.reason.value} | ratio={exit_sig.close_ratio}"
            )
            # TODO: 后续完全切换到新执行层后，直接调 executor.execute_close()
            # 当前阶段仍委托给 altcoin_tracker 的 evaluate_trade 做最终平仓

        elapsed = time.monotonic() - t0
        logger.info(f"[新引擎] 退出检查完成 | 耗时 {elapsed:.2f}s")

    except Exception as e:
        logger.error(f"[新引擎] run_exit 异常，fallback: {e}", exc_info=True)
        _fallback_exit(check_only)


# ══════════════════════════════════════════════════════════════════
#  数据持久化辅助
# ══════════════════════════════════════════════════════════════════

def _persist_candidates(candidates: list):
    """将候选写入 DB"""
    try:
        from db.repositories import CandidateRepo
        from datetime import datetime, timezone, timedelta
        import config as cfg

        expire_hours = getattr(cfg, 'CANDIDATE_EXPIRE_HOURS', 12)

        for c in candidates:
            CandidateRepo.upsert({
                'symbol': c.symbol,
                'price': c.price,
                'vol24h': c.metadata.get('vol24h', 0),
                'pct24h': c.metadata.get('pct24h', 0),
                'rsi_1d': c.metadata.get('rsi_1d', 50),
                'oi_change': c.metadata.get('oi_change', 0),
                'funding_rate': c.metadata.get('funding_rate', 0),
                'yao_score': c.metadata.get('yao_score', 0),
                'added_at': datetime.now(timezone.utc),
                'expires_at': datetime.now(timezone.utc) + timedelta(hours=expire_hours),
            })
    except Exception as e:
        logger.warning(f"候选写入 DB 失败（非致命）: {e}")


def _sync_candidates_to_json(candidates: list):
    """兼容：将候选同步写入旧 JSON 格式"""
    try:
        from common import CANDIDATES_FILE, LockedJsonFile, utcnow_iso
        import config as cfg

        candidate_dicts = []
        for c in candidates:
            candidate_dicts.append({
                'symbol': c.symbol,
                'price': c.price,
                'vol24h': c.metadata.get('vol24h', 0),
                'pct24h': c.metadata.get('pct24h', 0),
                'rsi_1d': c.metadata.get('rsi_1d', 50),
                'oi_change': c.metadata.get('oi_change', 0),
                'funding_rate': c.metadata.get('funding_rate', 0),
                'yao_score': c.metadata.get('yao_score', 0),
                'added_at': utcnow_iso(),
                'triggered': False,
            })

        with LockedJsonFile(CANDIDATES_FILE, default=[]) as (existing, save):
            existing_syms = {c['symbol'] for c in existing}
            for cd in candidate_dicts:
                if cd['symbol'] not in existing_syms:
                    existing.append(cd)
                else:
                    # 更新已有候选的指标
                    for ex in existing:
                        if ex['symbol'] == cd['symbol']:
                            ex.update({k: v for k, v in cd.items() if k != 'added_at'})
                            break
            save(existing)
    except Exception as e:
        logger.warning(f"候选同步到 JSON 失败（非致命）: {e}")


def _record_trade_opened(signal, result):
    """记录开仓到 DB + JSON"""
    try:
        from db.repositories import TradeRepo
        from datetime import datetime, timezone
        from models import Trade

        # 写 DB
        trade_data = {
            'id': f"ENG-{signal.symbol.replace('/', '')}-{int(time.time()*1000)}",
            'symbol': signal.symbol,
            'direction': signal.direction.value,
            'strategy': signal.strategy_name,
            'status': 'open',
            'entry_price': result.fill_price or 0,
            'stake': signal.stake,
            'leverage': signal.leverage,
            'notional': signal.stake * signal.leverage,
            'shares': (signal.stake * signal.leverage / result.fill_price) if result.fill_price > 0 else 0,
            'exchange': result.exchange,
            'account_id': result.account_id,
            'client_order_id': result.client_order_id,
            'live_order_id': result.order_id,
            'reason': signal.reason,
            'opened_at': datetime.now(timezone.utc),
            'created_at': datetime.now(timezone.utc),
            'updated_at': datetime.now(timezone.utc),
        }
        TradeRepo.create(trade_data)
    except Exception as e:
        logger.warning(f"交易写入 DB 失败（非致命）: {e}")

    # 兼容：同步写旧 JSON
    try:
        from common import TRADES_FILE, LockedJsonFile, utcnow_iso
        from models import Trade
        import config as cfg

        trade = Trade.create_short(
            symbol=signal.symbol,
            price=result.fill_price or 0,
            reason=signal.reason,
            stake=signal.stake,
            leverage=signal.leverage,
            exchange=result.exchange or 'shadow',
            live_order_id=result.order_id,
            client_order_id=result.client_order_id,
        )
        with LockedJsonFile(TRADES_FILE, default=[]) as (trades, save):
            trades.append(trade.to_dict())
            save(trades)
    except Exception as e:
        logger.warning(f"交易写入 JSON 失败（非致命）: {e}")


# ══════════════════════════════════════════════════════════════════
#  Fallback 到旧引擎
# ══════════════════════════════════════════════════════════════════

def _fallback_scan():
    """回退到旧的 altcoin_scanner.scan_daily()"""
    logger.info("[旧引擎] 执行 scan_daily")
    from altcoin_scanner import scan_daily
    scan_daily()


def _fallback_confirm():
    """回退到旧的 altcoin_scanner.check_candidates()"""
    logger.info("[旧引擎] 执行 check_candidates")
    from altcoin_scanner import check_candidates
    check_candidates()


def _fallback_exit(check_only: bool = True):
    """回退到旧的 altcoin_tracker.run()"""
    logger.info("[旧引擎] 执行 tracker run")
    from altcoin_tracker import run as tracker_run
    tracker_run(check_only=check_only)
