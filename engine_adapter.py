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

        # ── 宏观信号过滤（替代原 BTC 趋势过滤）──
        from macro.filter import check_macro_filter, get_macro_summary
        macro_result = check_macro_filter()

        if not macro_result.allowed:
            logger.warning(f"[新引擎] 宏观过滤暂停做空: {macro_result.reason}；LONG 信号仍继续评估")

        # 宏观信号的仓位乘数和评分 bonus 仅应用于 SHORT 信号
        _macro_stake_mult = macro_result.stake_multiplier
        _macro_score_bonus = macro_result.score_bonus
        if _macro_stake_mult != 1.0 or _macro_score_bonus != 0:
            logger.info(
                f"[新引擎] 宏观调节生效: stake×{_macro_stake_mult:.1f} "
                f"score{_macro_score_bonus:+d} | {macro_result.reason}"
            )

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
            signal_direction = getattr(signal.direction, 'value', signal.direction)
            is_short_signal = signal_direction == 'SHORT'
            if is_short_signal and not macro_result.allowed:
                logger.info(f"  🚫 宏观过滤拒绝做空 {signal.symbol}: {macro_result.reason}")
                continue

            # 1. 宏观信号调节（仓位乘数 + 评分加减分，仅 SHORT）。
            # 必须在风控前执行，否则 bearish 环境放大后的 stake 可能绕过准入检查。
            if is_short_signal and _macro_stake_mult != 1.0:
                old_stake = signal.stake
                signal.stake = round(signal.stake * _macro_stake_mult)
                if signal.stake != old_stake:
                    logger.info(
                        f"  📡 宏观仓位调节 {signal.symbol}: "
                        f"{old_stake}U → {signal.stake}U (×{_macro_stake_mult:.1f})"
                    )
            if is_short_signal and _macro_score_bonus != 0:
                signal.score = max(0, min(100, signal.score + _macro_score_bonus))

            # 2. 单笔风控
            from risk_control import can_open_trade
            allowed, reason = can_open_trade(
                stake=signal.stake,
                direction=signal_direction,
            )
            if not allowed:
                logger.info(f"  🚫 单笔风控拒绝 {signal.symbol}: {reason}")
                continue

            # 3. 组合风控
            check = portfolio_risk.check_new_position(
                symbol=signal.symbol,
                stake=signal.stake,
                open_positions=open_trades,
            )
            if not check.approved:
                logger.info(f"  🚫 组合风控拒绝 {signal.symbol}: {check.reason}")
                continue

            # 4. Kelly 仓位调整
            if 'suggested_stake' in check.adjustments:
                suggested = check.adjustments['suggested_stake']
                if suggested < signal.stake:
                    logger.info(
                        f"  📊 Kelly 调整 {signal.symbol}: {signal.stake}U → {suggested}U"
                    )
                    signal.stake = suggested

            # 5. 冷却期检查
            from risk_control import is_in_cooldown
            in_cd, cd_reason = is_in_cooldown(signal.symbol)
            if in_cd:
                logger.info(f"  🚫 冷却期 {signal.symbol}: {cd_reason}")
                continue

            # 6. 执行开仓
            result = executor.execute_signal(signal)
            if result.success:
                opened_count += 1
                # 记录开仓到 DB + JSON
                _record_trade_opened(signal, result)
                # 更新风控
                from risk_control import record_trade_opened as risk_record
                risk_record(stake=signal.stake, direction=signal_direction)
                open_trades.append({
                    'symbol': signal.symbol,
                    'stake': signal.stake,
                    'stake_remaining': signal.stake,
                    'direction': signal_direction,
                })
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

        # 执行平仓：调用 altcoin_tracker 的 evaluate_trade 完成完整状态管理
        # （含 TP1 半仓、保本止损更新、JSON 落盘、交易所真实平仓）
        for exit_sig in exit_signals:
            logger.info(
                f"  📤 退出信号: trade={exit_sig.trade_id} | "
                f"reason={exit_sig.reason.value} | ratio={exit_sig.close_ratio}"
            )
            # 通过 altcoin_tracker 的标准流程执行平仓
            try:
                from altcoin_tracker import evaluate_trade
                from common import TRADES_FILE, LockedJsonFile
                from models import Trade

                with LockedJsonFile(TRADES_FILE, default=[]) as (trades_raw, save):
                    trades = [Trade.from_dict(t) for t in trades_raw]
                    target = next((t for t in trades if t.id == exit_sig.trade_id), None)
                    if target and target.status == 'open':
                        # 用当前价格触发 evaluate_trade
                        ticker = _get_data_feed().get_ticker(target.symbol)
                        current_price = ticker.get('last', 0)
                        if current_price > 0:
                            result = evaluate_trade(target, current_price)
                            if result.closed or result.updated:
                                save([t.to_dict() for t in trades])
                                # 发布事件
                                if result.closed:
                                    try:
                                        from event_integration import on_trade_closed
                                        on_trade_closed(
                                            trade_id=target.id,
                                            symbol=target.symbol,
                                            pnl=result.pnl_usd,
                                            close_type=exit_sig.reason.value,
                                            exchange=target.exchange,
                                        )
                                    except Exception:
                                        pass
            except Exception as ex:
                logger.warning(f"  退出执行异常 ({exit_sig.trade_id}): {ex}")

        elapsed = time.monotonic() - t0
        logger.info(f"[新引擎] 退出检查完成 | 耗时 {elapsed:.2f}s")

    except Exception as e:
        logger.error(f"[新引擎] run_exit 异常，fallback: {e}", exc_info=True)
        _fallback_exit(check_only)


# ══════════════════════════════════════════════════════════════════
#  数据持久化辅助
# ══════════════════════════════════════════════════════════════════

def _persist_candidates(candidates: list):
    """将候选写入 DB — 多策略支持，按 (symbol, strategy) 复合键 upsert"""
    try:
        from db.repositories import CandidateRepo
        from datetime import datetime, timezone, timedelta
        import config as cfg
        import json as _json

        expire_hours = getattr(cfg, 'CANDIDATE_EXPIRE_HOURS', 12)

        for c in candidates:
            # 从策略注入的字段读取
            strategy_name = getattr(c, 'strategy_name', '') or 'short_overbought'
            direction = getattr(c, 'direction', '') or 'SHORT'
            metadata = getattr(c, 'metadata', {}) or {}

            candidate_data = {
                'symbol': c.symbol,
                'strategy': strategy_name,
                'direction': direction,
                'price': c.price,
                'score': getattr(c, 'score', 0),
                'vol24h': metadata.get('vol24h', metadata.get('vol_24h', 0)),
                'pct24h': metadata.get('pct24h', metadata.get('pct_24h', 0)),
                'rsi_1d': metadata.get('rsi_1d', 50),
                'oi_change': metadata.get('oi_change', 0),
                'funding_rate': metadata.get('funding_rate', 0),
                'added_at': datetime.now(timezone.utc),
                'expires_at': datetime.now(timezone.utc) + timedelta(hours=expire_hours),
            }

            # short_overbought 专用字段
            if strategy_name == 'short_overbought':
                candidate_data['yao_score'] = metadata.get('yao_score', 0)

            # 其余 metadata 序列化存入 metadata_json
            # 排除已经存入独立列的字段
            _known_keys = {'vol24h', 'pct24h', 'rsi_1d', 'oi_change',
                           'funding_rate', 'yao_score', 'direction'}
            extra_meta = {k: v for k, v in metadata.items() if k not in _known_keys}
            if extra_meta:
                candidate_data['metadata_json'] = _json.dumps(
                    extra_meta, ensure_ascii=False)

            CandidateRepo.upsert(candidate_data)
    except Exception as e:
        logger.warning(f"候选写入 DB 失败（非致命）: {e}")


def _sync_candidates_to_json(candidates: list):
    """兼容：将候选同步写入旧 JSON 格式（含 strategy/direction 标签）"""
    try:
        from common import CANDIDATES_FILE, LockedJsonFile, utcnow_iso
        import config as cfg

        candidate_dicts = []
        for c in candidates:
            strategy_name = getattr(c, 'strategy_name', '') or 'short_overbought'
            direction = getattr(c, 'direction', '') or 'SHORT'
            metadata = getattr(c, 'metadata', {}) or {}

            candidate_dicts.append({
                'symbol': c.symbol,
                'strategy': strategy_name,
                'direction': direction,
                'price': c.price,
                'score': getattr(c, 'score', 0),
                'vol24h': metadata.get('vol24h', metadata.get('vol_24h', 0)),
                'pct24h': metadata.get('pct24h', metadata.get('pct_24h', 0)),
                'rsi_1d': metadata.get('rsi_1d', 50),
                'oi_change': metadata.get('oi_change', 0),
                'funding_rate': metadata.get('funding_rate', 0),
                'yao_score': metadata.get('yao_score', 0),
                'added_at': utcnow_iso(),
                'triggered': False,
            })

        with LockedJsonFile(CANDIDATES_FILE, default=[]) as (existing, save):
            # 按 (symbol, strategy) 复合键去重
            existing_keys = {(c['symbol'], c.get('strategy', 'short_overbought'))
                            for c in existing}
            for cd in candidate_dicts:
                key = (cd['symbol'], cd['strategy'])
                if key not in existing_keys:
                    existing.append(cd)
                else:
                    # 更新已有候选的指标
                    for ex in existing:
                        if ex['symbol'] == cd['symbol'] and \
                           ex.get('strategy', 'short_overbought') == cd['strategy']:
                            ex.update({k: v for k, v in cd.items()
                                      if k not in ('added_at', 'triggered')})
                            break
            save(existing)
    except Exception as e:
        logger.warning(f"候选同步到 JSON 失败（非致命）: {e}")


def _record_trade_opened(signal, result):
    """记录开仓到 DB + JSON，确保两边使用同一个 trade.id。"""
    from models import Trade

    trade = Trade.create_directional(
        symbol=signal.symbol,
        direction=getattr(signal.direction, 'value', signal.direction),
        price=result.fill_price or 0,
        reason=signal.reason,
        stake=signal.stake,
        leverage=signal.leverage,
        exchange=result.exchange or 'shadow',
        live_order_id=result.order_id,
        client_order_id=result.client_order_id,
        account_id=result.account_id or '',
        strategy=signal.strategy_name,
        hard_stop_loss_pct=signal.hard_stop_pct,
        tp1_pct=signal.tp1_pct,
        tp2_pct=signal.tp2_pct,
        max_hold_hours=signal.max_hold_hours,
    )

    # 写 DB：只传 DB schema 支持的字段，避免 dataclass 兼容字段导致 ORM 构造失败。
    try:
        from db.repositories import TradeRepo
        from db.models import TradeModel

        db_fields = {c.name for c in TradeModel.__table__.columns}
        trade_data = {k: v for k, v in trade.to_dict().items() if k in db_fields}
        TradeRepo.create(trade_data)
    except Exception as e:
        logger.warning(f"交易写入 DB 失败（非致命）: {e}")

    # 兼容：同步写旧 JSON
    try:
        from common import TRADES_FILE, LockedJsonFile

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
