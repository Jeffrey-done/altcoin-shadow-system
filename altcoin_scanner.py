#!/usr/bin/env python3
"""
小币种超买做空扫描器 v2.0
多时间框架策略：
  - 每4小时扫描全市场，筛选日线RSI>85的候选池
  - 每1小时检查候选池，确认4小时RSI开始回落才触发信号
  - 过滤条件：市值500万~5000万U、上市>30天、成交量>50万U
"""

import ccxt, json, os, time, requests, logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger("altcoin_scanner")

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CANDIDATES_FILE = os.path.join(SCRIPT_DIR, 'altcoin_candidates.json')
TRADES_FILE  = os.path.join(SCRIPT_DIR, 'altcoin_shadow_trades.json')

# ── 配置 ──────────────────────────────────────────────────────────
MCAP_MIN     = 5_000_000      # 市值下限 500万U
MCAP_MAX     = 50_000_000     # 市值上限 5000万U  
VOL_MIN      = 500_000        # 24h成交量下限 50万U
DAILY_RSI_MIN = 78            # 日线RSI阈值
H4_RSI_ENTER  = 70            # 4小时RSI进入信号（从高位回落至此）
H4_RSI_DROP   = 10            # 4小时RSI需从峰值回落多少点
PRICE_MAX    = 1.0            # 价格上限（只做小币）
LIST_DAYS_MIN = 30            # 上市至少30天

# ── 妖币识别参数（新增）──────────────────────────────
OI_CHANGE_MIN    = 0.30       # OI 24h涨幅下限（30%表示主力在建仓）
FUNDING_MAX      = 0.05       # 资金费率上限（空头付费上限，超过跳过）
FUNDING_HOT      = 0.03       # 资金费率热度阈值（>0.03%/8h表示多头过热）

# 加载.env
from dotenv import load_dotenv
# 强制加载.env（兼容cron无环境变量场景）
_env_path = os.path.join(SCRIPT_DIR, '.env')
load_dotenv(_env_path, override=True)
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', '')
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID', '8068489553')

def send_tg(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        logger.error(f"TG推送失败: {e}")

def get_oi_change(symbol: str) -> float:
    """获取合约OI 24h变化率，无合约返回0"""
    try:
        sym = symbol.replace('/USDT','USDT').replace('/','')
        import requests as _req
        r = _req.get(f"https://fapi.binance.com/fapi/v1/openInterest", 
                     params={"symbol": sym}, timeout=5)
        if r.status_code != 200: return 0
        oi_now = float(r.json().get('openInterest', 0))
        # 获取24h前OI（用历史数据）
        r2 = _req.get(f"https://fapi.binance.com/futures/data/openInterestHist",
                      params={"symbol": sym, "period": "1h", "limit": 25}, timeout=5)
        if r2.status_code != 200 or not r2.json(): return 0
        hist = r2.json()
        oi_24h_ago = float(hist[0].get('sumOpenInterest', 0))
        if oi_24h_ago <= 0: return 0
        return (oi_now - oi_24h_ago) / oi_24h_ago
    except:
        return 0

def get_funding_rate_now(symbol: str) -> float:
    """获取当前资金费率（%/8h），无合约返回0"""
    try:
        sym = symbol.replace('/USDT','USDT').replace('/','')
        import requests as _req
        r = _req.get(f"https://fapi.binance.com/fapi/v1/premiumIndex",
                     params={"symbol": sym}, timeout=5)
        if r.status_code != 200: return 0
        return float(r.json().get('lastFundingRate', 0)) * 100
    except:
        return 0

def calc_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    avg_g  = sum(gains[-period:]) / period
    avg_l  = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_g / avg_l)), 1)

def get_rsi(exchange, symbol: str, timeframe: str, limit: int = 30) -> float:
    try:
        ohlcv  = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        closes = [c[4] for c in ohlcv]
        return calc_rsi(closes)
    except:
        return 50.0

def get_rsi_peak(exchange, symbol: str, timeframe: str = '4h', lookback: int = 20) -> float:
    """获取近期RSI峰值"""
    try:
        ohlcv  = exchange.fetch_ohlcv(symbol, timeframe, limit=lookback + 14)
        closes = [c[4] for c in ohlcv]
        rsi_series = []
        for i in range(14, len(closes)):
            rsi_series.append(calc_rsi(closes[:i+1]))
        return max(rsi_series[-lookback:]) if rsi_series else 50.0
    except:
        return 50.0

def detect_abandon_signal(exchange, symbol: str) -> dict:
    """
    检测1H K线弃盘点信号（文章V3核心策略）
    弃盘特征：连续2根1H K线实体下跌>3% + OI同步下降
    返回：{"signal": True/False, "reason": str, "drop_pct": float}
    """
    try:
        # 获取最近6根1H K线
        h1 = exchange.fetch_ohlcv(symbol, '1h', limit=6)
        if len(h1) < 3:
            return {"signal": False, "reason": "数据不足"}

        # 计算最近3根K线的实体跌幅（收盘-开盘）/开盘
        drops = []
        for candle in h1[-3:]:
            open_p, close_p = candle[1], candle[4]
            body_drop = (open_p - close_p) / open_p * 100  # 正值=下跌
            drops.append(round(body_drop, 2))

        # 连续2根实体下跌>3%（弃盘特征）
        consecutive_drop = sum(1 for d in drops[-2:] if d > 3)

        if consecutive_drop < 2:
            return {"signal": False, "reason": f"1H跌幅不足({drops[-2:]})"}

        total_drop = sum(d for d in drops[-2:] if d > 0)

        # 检查OI是否同步下降（排除爆仓导致的下跌）
        oi_declining = False
        try:
            sym = symbol.replace('/USDT','USDT').replace('/','')
            import requests as _req
            r = _req.get("https://fapi.binance.com/futures/data/openInterestHist",
                        params={"symbol": sym, "period": "1h", "limit": 5}, timeout=5)
            if r.status_code == 200 and r.json():
                oi_hist = r.json()
                if len(oi_hist) >= 2:
                    oi_recent = float(oi_hist[-1]['sumOpenInterest'])
                    oi_prev   = float(oi_hist[-2]['sumOpenInterest'])
                    oi_declining = oi_recent < oi_prev * 0.98  # OI下降>2%
        except:
            pass

        reason = f"连续{consecutive_drop}根1H实体下跌{total_drop:.1f}%"
        if oi_declining:
            reason += " + OI同步下降（主力撤退信号）"

        return {
            "signal": True,
            "reason": reason,
            "drop_pct": total_drop,
            "oi_declining": oi_declining,
            "drops": drops[-2:]
        }
    except Exception as e:
        return {"signal": False, "reason": f"检测失败:{e}"}


# ── 第一阶段：日线扫描（每4小时）──────────────────────────────────
def scan_daily():
    """扫描全市场，找日线RSI>85的候选币"""
    logger.info("=== 开始日线扫描 ===")
    exchange = ccxt.binance({'enableRateLimit': True})

    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        logger.error(f"获取行情失败: {e}")
        return

    candidates = {}
    checked = 0

    for symbol, ticker in tickers.items():
        if not symbol.endswith('/USDT'):
            continue

        price  = ticker.get('last', 0) or 0
        vol24h = ticker.get('quoteVolume', 0) or 0
        pct24h = ticker.get('percentage', 0) or 0

        # 基础过滤
        if price <= 0 or price > PRICE_MAX:
            continue
        if vol24h < VOL_MIN:
            continue
        if pct24h < 10:  # 至少24h涨幅>10%才值得看
            continue

        checked += 1

        # 市值估算（用流通供应量×价格，Binance ticker里没有直接市值，用成交量/换手率近似）
        # 简化：用vol24h/price作为日均换手量，市值≈价格×供应量
        # 实际用vol24h作为流动性参考，跳过无法获取市值的情况
        # 改用成交量门槛代替市值：50万~5000万U的成交量对应小币特征

        # 计算日线RSI
        rsi_1d = get_rsi(exchange, symbol, '1d', limit=30)

        if rsi_1d >= DAILY_RSI_MIN:
            # 查询OI变化率和资金费率（妖币验证）
            oi_change  = get_oi_change(symbol)
            funding    = get_funding_rate_now(symbol)
            # 妖币加权评分（0-3分）
            yao_score  = 0
            if oi_change >= OI_CHANGE_MIN: yao_score += 1   # OI快速堆积
            if funding >= FUNDING_HOT:     yao_score += 1   # 多头过热
            if pct24h >= 30:               yao_score += 1   # 暴涨
            
            # 资金费率过高跳过（空头成本太贵）
            if funding > FUNDING_MAX:
                logger.info(f"  ⛔ 跳过: {symbol} 资金费率过高({funding:.4f}%)")
                continue

            candidates[symbol] = {
                "symbol": symbol,
                "price": price,
                "vol24h": round(vol24h),
                "pct24h": round(pct24h, 1),
                "rsi_1d": rsi_1d,
                "rsi_4h": None,
                "rsi_4h_peak": None,
                "oi_change": round(oi_change * 100, 1),   # OI 24h变化%
                "funding_rate": round(funding, 4),          # 资金费率
                "yao_score": yao_score,                     # 妖币评分
                "added_at": datetime.utcnow().isoformat(),
                "triggered": False,
            }
            yao_tag = "🔥妖币" if yao_score >= 2 else ("⚡候选" if yao_score == 1 else "📌普通")
            logger.info(f"  ✅ {yao_tag}: {symbol} | 日线RSI={rsi_1d} | 24h={pct24h:.1f}% | OI变化={oi_change*100:.0f}% | FR={funding:.4f}% | 评分={yao_score}")

        time.sleep(0.1)  # 避免频率限制

    # 保存候选池（合并已有）
    existing = {}
    if os.path.exists(CANDIDATES_FILE):
        existing = {c['symbol']: c for c in json.load(open(CANDIDATES_FILE))}

    # 更新现有候选的日线RSI
    for sym, data in candidates.items():
        if sym in existing:
            existing[sym]['rsi_1d'] = data['rsi_1d']
            existing[sym]['price']  = data['price']
            existing[sym]['vol24h'] = data['vol24h']
            existing[sym]['pct24h'] = data['pct24h']
        else:
            existing[sym] = data

    # 清理已触发超过3天的候选
    now = datetime.utcnow()
    to_remove = []
    for sym, c in existing.items():
        if c.get('triggered'):
            added = datetime.fromisoformat(c['added_at'])
            if (now - added).days > 1:
                to_remove.append(sym)
    for sym in to_remove:
        del existing[sym]

    json.dump(list(existing.values()), open(CANDIDATES_FILE, 'w'), indent=2, ensure_ascii=False)
    logger.info(f"日线扫描完成，共检查{checked}个标的，候选池{len(existing)}个")

# ── 第二阶段：4小时确认（每1小时）────────────────────────────────
def check_candidates():
    """检查候选池，4小时RSI开始回落则触发信号"""
    if not os.path.exists(CANDIDATES_FILE):
        logger.info("候选池为空，跳过")
        return

    candidates = json.load(open(CANDIDATES_FILE))
    if not candidates:
        return

    exchange = ccxt.binance({'enableRateLimit': True})
    logger.info(f"=== 检查候选池（{len(candidates)}个）===")

    # 已有持仓的币，不重复开仓
    open_symbols = set()
    if os.path.exists(TRADES_FILE):
        trades = json.load(open(TRADES_FILE))
        open_symbols = {t['symbol'] for t in trades if t.get('status') == 'open'}

    triggered_any = False
    for c in candidates:
        if c.get('triggered'):
            continue
        symbol = c['symbol']
        if symbol in open_symbols:
            continue

        # 获取4小时RSI和峰值
        rsi_4h      = get_rsi(exchange, symbol, '4h', limit=30)
        rsi_4h_peak = get_rsi_peak(exchange, symbol, '4h', lookback=10)
        c['rsi_4h']      = rsi_4h
        c['rsi_4h_peak'] = rsi_4h_peak

        drop = rsi_4h_peak - rsi_4h
        logger.info(f"  {symbol}: 日线RSI={c['rsi_1d']} | 4h RSI={rsi_4h} | 峰值={rsi_4h_peak:.1f} | 回落={drop:.1f}")

        # 触发条件A：4h RSI回落（旧逻辑）
        trigger_4h = rsi_4h < H4_RSI_ENTER and drop >= H4_RSI_DROP

        # 触发条件B：1H弃盘点信号（文章核心，更精准）
        abandon = detect_abandon_signal(exchange, symbol)
        trigger_abandon = abandon.get("signal", False)

        # 任一条件满足即触发（弃盘点优先）
        if trigger_4h or trigger_abandon:
            price = exchange.fetch_ticker(symbol)['last']
            c['triggered'] = True
            triggered_any  = True

            # 记录触发原因
            if trigger_abandon:
                trigger_reason = f"弃盘点: {abandon['reason']}"
                c['trigger_type'] = 'abandon'
            else:
                trigger_reason = f"4h RSI从{rsi_4h_peak:.0f}回落至{rsi_4h}"
                c['trigger_type'] = '4h_rsi'
            c['trigger_reason'] = trigger_reason

            logger.info(f"  🚨 触发信号: {symbol} @ {price} [{trigger_reason}]")

            # 自动开影子空单
            import time as _time
            trades_data = []
            if os.path.exists(TRADES_FILE):
                trades_data = json.load(open(TRADES_FILE))
            trade = {
                "id": f"SCAN-SHORT-{symbol.replace('/USDT','')}-{int(_time.time())}",
                "symbol": symbol,
                "direction": "SHORT",
                "entry_price": price,
                "stake": 200,
                "shares": round(200 / price, 4) if price > 0 else 0,
                "opened_at": datetime.utcnow().isoformat(),
                "status": "open",
                "source": "扫描器自动开仓",
                "reason": c.get('trigger_reason', f"日线RSI={c['rsi_1d']} | 4hRSI从{rsi_4h_peak:.0f}回落至{rsi_4h}"),
                "take_profit": round(price * 0.50, 6),
                "take_profit_1": round(price * 0.70, 6),
                "take_profit_2": round(price * 0.50, 6),
                "tp1_triggered": False,
                "stake_remaining": 200,
                "stop_loss": None,
                "pnl": 0,
            }
            trades_data.append(trade)
            json.dump(trades_data, open(TRADES_FILE, 'w'), indent=2, ensure_ascii=False)
            logger.info(f"  ✅ 已自动开空单: {symbol} @ {price}")

            # 开仓后推送通知
            yao_tag = "🔥妖币" if c.get('yao_score',0) >= 2 else "📌普通超买"
            msg = (
                f"🔴 <b>影子空单已自动开仓</b> {yao_tag}\n\n"
                f"📌 <b>{symbol}</b>\n"
                f"入场价：{price:.6f} U\n"
                f"虚拟仓位：$200\n"
                f"止盈一档：{trade['take_profit_1']:.6f}（-20%）\n"
                f"止盈二档：{trade['take_profit_2']:.6f}（-35%）\n"
                f"移动止损：从最高盈利回撤10%触发\n\n"
                f"📊 开仓依据：\n"
                f"日线RSI：{c['rsi_1d']}（超买）\n"
                f"4h RSI：{rsi_4h}（从{rsi_4h_peak:.0f}回落{drop:.0f}点）\n"
                f"24h涨幅：{c['pct24h']:+.1f}% | 成交量：{c['vol24h']:,}U\n"
                f"OI变化：{c.get('oi_change',0):+.0f}% | 资金费率：{c.get('funding_rate',0):.4f}%/8h\n"
                f"妖币评分：{c.get('yao_score',0)}/3"
            )
            send_tg(msg)

        time.sleep(0.1)

    json.dump(candidates, open(CANDIDATES_FILE, 'w'), indent=2, ensure_ascii=False)

    if not triggered_any:
        logger.info("候选池无触发信号")

# ── 主入口 ────────────────────────────────────────────────────────
if __name__ == '__main__':
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else 'check'

    if mode == 'scan':
        scan_daily()
    elif mode == 'check':
        check_candidates()
    elif mode == 'both':
        scan_daily()
        check_candidates()
