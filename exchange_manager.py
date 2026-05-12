#!/usr/bin/env python3
"""
多交易所管理模块 v1.0
统一封装 Binance 和 OKX 的数据接口，提供：
  - K线/Ticker 数据获取
  - OI（持仓量）变化率
  - 资金费率
  - 合约品种列表
  - 费率交叉验证（两所费率都异常 = 信号更强）

设计原则：
  - Binance 为主交易所（下单 + 主数据源）
  - OKX 为辅助交易所（交叉验证 + 补充品种）
  - 单个交易所故障不影响系统运行（优雅降级）
"""

import time
from typing import Optional

import ccxt
import requests

import config
from common import setup_logger, to_binance_symbol

logger = setup_logger("exchange_manager")


# ══════════════════════════════════════════════════════════════════
#  交易所实例管理（单例复用，避免重复创建）
# ══════════════════════════════════════════════════════════════════

_binance_instance: Optional[ccxt.binance] = None
_okx_instance: Optional[ccxt.okx] = None


def get_binance(authenticated: bool = False) -> ccxt.binance:
    """获取 Binance 交易所实例（公共数据用无认证版本）"""
    global _binance_instance
    if not authenticated:
        if _binance_instance is None:
            _binance_instance = ccxt.binance({'enableRateLimit': True})
        return _binance_instance

    # 认证版本每次新建（避免共享状态）
    import os
    return ccxt.binance({
        'apiKey': os.environ.get('BINANCE_API_KEY', ''),
        'secret': os.environ.get('BINANCE_SECRET', ''),
        'enableRateLimit': True,
        'options': {'defaultType': 'future'},
    })


def get_okx(authenticated: bool = False) -> Optional[ccxt.okx]:
    """
    获取 OKX 交易所实例。
    如果 OKX 未启用或连接失败，返回 None（优雅降级）。
    """
    global _okx_instance
    if not config.OKX_ENABLED:
        return None

    if not authenticated:
        if _okx_instance is None:
            try:
                _okx_instance = ccxt.okx({'enableRateLimit': True})
            except Exception as e:
                logger.warning(f"OKX 初始化失败: {e}")
                return None
        return _okx_instance

    # 认证版本
    import os
    api_key = os.environ.get('OKX_API_KEY', '')
    secret = os.environ.get('OKX_SECRET', '')
    passphrase = os.environ.get('OKX_PASSPHRASE', '')
    if not api_key or not secret or not passphrase:
        logger.warning("OKX API 凭证未配置，无法使用认证接口")
        return None

    try:
        return ccxt.okx({
            'apiKey': api_key,
            'secret': secret,
            'password': passphrase,
            'enableRateLimit': True,
        })
    except Exception as e:
        logger.warning(f"OKX 认证实例创建失败: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
#  符号转换
# ══════════════════════════════════════════════════════════════════

def to_okx_inst_id(symbol: str) -> str:
    """
    ccxt 格式 (BTC/USDT) → OKX instId 格式 (BTC-USDT-SWAP)
    """
    base = symbol.replace('/USDT', '').replace('/', '')
    return f"{base}-USDT-SWAP"


def to_okx_spot_id(symbol: str) -> str:
    """
    ccxt 格式 (BTC/USDT) → OKX 现货 instId (BTC-USDT)
    """
    base = symbol.replace('/USDT', '').replace('/', '')
    return f"{base}-USDT"


# ══════════════════════════════════════════════════════════════════
#  OKX 数据获取
# ══════════════════════════════════════════════════════════════════

def get_okx_funding_rate(symbol: str) -> float:
    """
    获取 OKX 当前资金费率（%/8h）。
    返回 0.0 表示获取失败或无合约。
    """
    if not config.OKX_ENABLED:
        return 0.0
    try:
        inst_id = to_okx_inst_id(symbol)
        r = requests.get(
            "https://www.okx.com/api/v5/public/funding-rate",
            params={"instId": inst_id},
            timeout=5,
        )
        if r.status_code != 200:
            return 0.0
        data = r.json().get('data', [])
        if not data:
            return 0.0
        # OKX 返回的 fundingRate 已经是小数形式
        return float(data[0].get('fundingRate', 0)) * 100
    except Exception as e:
        logger.debug(f"获取 OKX 费率失败 ({symbol}): {e}")
        return 0.0


def get_okx_oi_change(symbol: str) -> float:
    """
    获取 OKX 合约 OI 24h 变化率。
    返回 0.0 表示获取失败。
    """
    if not config.OKX_ENABLED:
        return 0.0
    try:
        inst_id = to_okx_inst_id(symbol)
        r = requests.get(
            "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history",
            params={"instId": inst_id, "period": "1H"},
            timeout=5,
        )
        if r.status_code != 200:
            return 0.0
        data = r.json().get('data', [])
        if len(data) < 2:
            return 0.0
        # data[0] 是最新的，data[-1] 是最旧的
        # 取最近 24 个（1H × 24 = 24h）
        oi_now = float(data[0][1]) if len(data[0]) > 1 else 0
        oi_24h_idx = min(23, len(data) - 1)
        oi_24h_ago = float(data[oi_24h_idx][1]) if len(data[oi_24h_idx]) > 1 else 0
        if oi_24h_ago <= 0:
            return 0.0
        return (oi_now - oi_24h_ago) / oi_24h_ago
    except Exception as e:
        logger.debug(f"获取 OKX OI 变化失败 ({symbol}): {e}")
        return 0.0


def get_okx_all_funding_rates() -> list:
    """
    获取 OKX 所有永续合约的资金费率。
    返回: [{"symbol": "BTC/USDT", "rate": -0.01, "instId": "BTC-USDT-SWAP"}, ...]
    """
    if not config.OKX_ENABLED:
        return []
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/public/funding-rate",
            params={"instType": "SWAP"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        data = r.json().get('data', [])
        results = []
        for item in data:
            inst_id = item.get('instId', '')
            if not inst_id.endswith('-USDT-SWAP'):
                continue
            base = inst_id.replace('-USDT-SWAP', '')
            rate = float(item.get('fundingRate', 0)) * 100
            results.append({
                'symbol': f"{base}/USDT",
                'rate': rate,
                'instId': inst_id,
            })
        return results
    except Exception as e:
        logger.error(f"获取 OKX 全量费率失败: {e}")
        return []


def get_okx_ticker(symbol: str) -> dict:
    """
    获取 OKX 现货 ticker 数据。
    返回: {"last": float, "volume": float, "percentage": float} 或空字典
    """
    if not config.OKX_ENABLED:
        return {}
    try:
        okx = get_okx()
        if not okx:
            return {}
        ticker = okx.fetch_ticker(symbol)
        return {
            'last': ticker.get('last', 0),
            'volume': ticker.get('quoteVolume', 0),
            'percentage': ticker.get('percentage', 0),
        }
    except Exception as e:
        logger.debug(f"获取 OKX ticker 失败 ({symbol}): {e}")
        return {}


# ══════════════════════════════════════════════════════════════════
#  交叉验证
# ══════════════════════════════════════════════════════════════════

def cross_validate_funding(symbol: str, binance_rate: float) -> dict:
    """
    费率交叉验证：对比 Binance 和 OKX 的费率。

    返回:
      {
        "okx_rate": float,         # OKX 费率
        "binance_rate": float,     # Binance 费率
        "both_negative": bool,     # 两边都是负费率（做多信号更强）
        "both_positive": bool,     # 两边都是正费率（做空信号更强）
        "divergence": float,       # 两所费率差（绝对值）
        "signal_boost": bool,      # 是否应该加强信号
        "available": bool,         # OKX 数据是否可用
      }
    """
    okx_rate = get_okx_funding_rate(symbol)

    result = {
        "okx_rate": okx_rate,
        "binance_rate": binance_rate,
        "both_negative": False,
        "both_positive": False,
        "divergence": abs(binance_rate - okx_rate),
        "signal_boost": False,
        "available": okx_rate != 0.0,
    }

    if not result["available"]:
        return result

    result["both_negative"] = (binance_rate < 0 and okx_rate < 0)
    result["both_positive"] = (binance_rate > 0 and okx_rate > 0)

    # 信号加强条件：两所费率方向一致且都超过阈值
    if result["both_positive"] and binance_rate >= config.FUNDING_HOT and okx_rate >= config.OKX_FUNDING_HOT:
        result["signal_boost"] = True
    elif result["both_negative"] and binance_rate <= config.FUNDING_ARB_MIN_RATE and okx_rate <= config.OKX_FUNDING_ARB_MIN_RATE:
        result["signal_boost"] = True

    return result


def cross_validate_oi(symbol: str, binance_oi_change: float) -> dict:
    """
    OI 交叉验证：对比 Binance 和 OKX 的 OI 变化。

    返回:
      {
        "okx_oi_change": float,    # OKX OI 变化率
        "binance_oi_change": float,
        "both_increasing": bool,   # 两边 OI 都在涨
        "signal_boost": bool,      # 是否加强信号
        "available": bool,
      }
    """
    okx_oi = get_okx_oi_change(symbol)

    result = {
        "okx_oi_change": okx_oi,
        "binance_oi_change": binance_oi_change,
        "both_increasing": False,
        "signal_boost": False,
        "available": okx_oi != 0.0,
    }

    if not result["available"]:
        return result

    result["both_increasing"] = (binance_oi_change > 0 and okx_oi > 0)

    # 两所 OI 都涨超过阈值 → 信号加强
    if binance_oi_change >= config.OI_CHANGE_MIN and okx_oi >= config.OKX_OI_CHANGE_MIN:
        result["signal_boost"] = True

    return result


# ══════════════════════════════════════════════════════════════════
#  多交易所聚合数据
# ══════════════════════════════════════════════════════════════════

def get_aggregated_funding_rates() -> dict:
    """
    聚合 Binance + OKX 所有合约费率。
    返回: {
      "BTC/USDT": {"binance": 0.01, "okx": 0.008, "avg": 0.009},
      ...
    }
    """
    result = {}

    # Binance 费率
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            timeout=10,
        )
        if r.status_code == 200:
            for item in r.json():
                sym = item.get('symbol', '')
                if not sym.endswith('USDT'):
                    continue
                base = sym.replace('USDT', '')
                ccxt_sym = f"{base}/USDT"
                rate = float(item.get('lastFundingRate', 0)) * 100
                result[ccxt_sym] = {"binance": rate, "okx": None, "avg": rate}
    except Exception as e:
        logger.warning(f"获取 Binance 全量费率失败: {e}")

    # OKX 费率
    okx_rates = get_okx_all_funding_rates()
    for item in okx_rates:
        sym = item['symbol']
        rate = item['rate']
        if sym in result:
            result[sym]["okx"] = rate
            # 计算平均
            result[sym]["avg"] = (result[sym]["binance"] + rate) / 2
        else:
            result[sym] = {"binance": None, "okx": rate, "avg": rate}

    return result


def find_cross_exchange_arb_opportunities() -> list:
    """
    寻找跨交易所费率套利机会：
    - 两所费率方向相反 → 两边对冲吃费率
    - 两所都极度负费率 → 做多信号更强

    返回机会列表（按潜在收益排序）。
    """
    rates = get_aggregated_funding_rates()
    opportunities = []

    for symbol, data in rates.items():
        bn_rate = data.get("binance")
        okx_rate = data.get("okx")

        if bn_rate is None or okx_rate is None:
            continue

        divergence = abs(bn_rate - okx_rate)

        # 情况1：两边都极度负 → 强做多信号
        if bn_rate <= config.FUNDING_ARB_MIN_RATE and okx_rate <= config.OKX_FUNDING_ARB_MIN_RATE:
            avg_rate = (bn_rate + okx_rate) / 2
            opportunities.append({
                'symbol': symbol,
                'type': 'both_negative',
                'binance_rate': bn_rate,
                'okx_rate': okx_rate,
                'avg_rate': avg_rate,
                'divergence': divergence,
                'signal_strength': 'strong',
            })

        # 情况2：费率差异极大（>0.1%）→ 跨所对冲
        elif divergence >= config.OKX_CROSS_ARB_MIN_DIVERGENCE:
            opportunities.append({
                'symbol': symbol,
                'type': 'divergence',
                'binance_rate': bn_rate,
                'okx_rate': okx_rate,
                'avg_rate': (bn_rate + okx_rate) / 2,
                'divergence': divergence,
                'signal_strength': 'medium' if divergence >= 0.15 else 'weak',
            })

    # 按收益潜力排序
    opportunities.sort(key=lambda x: x['divergence'], reverse=True)
    return opportunities


# ══════════════════════════════════════════════════════════════════
#  品种覆盖检查
# ══════════════════════════════════════════════════════════════════

_okx_swap_symbols: Optional[set] = None


def okx_has_swap(symbol: str) -> bool:
    """检查 OKX 是否有该币种的永续合约"""
    global _okx_swap_symbols
    if not config.OKX_ENABLED:
        return False

    if _okx_swap_symbols is None:
        try:
            okx = get_okx()
            if okx:
                okx.load_markets()
                _okx_swap_symbols = {
                    m['symbol'] for m in okx.markets.values()
                    if m.get('swap') and m.get('quote') == 'USDT'
                }
            else:
                _okx_swap_symbols = set()
        except Exception as e:
            logger.warning(f"加载 OKX 市场列表失败: {e}")
            _okx_swap_symbols = set()

    # ccxt OKX swap 格式: BTC/USDT:USDT
    swap_symbol = symbol.replace('/USDT', '/USDT:USDT')
    return swap_symbol in _okx_swap_symbols


# ══════════════════════════════════════════════════════════════════
#  BTC 价格（多源）
# ══════════════════════════════════════════════════════════════════

def get_btc_24h_change_multi() -> float:
    """
    多源获取 BTC 24h 涨跌幅。
    优先 Binance，失败则尝试 OKX。
    """
    # 尝试 Binance
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": "BTCUSDT"},
            timeout=5,
        )
        if r.status_code == 200:
            return float(r.json().get('priceChangePercent', 0))
    except Exception:
        pass

    # 降级到 OKX
    if config.OKX_ENABLED:
        try:
            r = requests.get(
                "https://www.okx.com/api/v5/market/ticker",
                params={"instId": "BTC-USDT"},
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json().get('data', [])
                if data:
                    open_24h = float(data[0].get('open24h', 0))
                    last = float(data[0].get('last', 0))
                    if open_24h > 0:
                        return (last - open_24h) / open_24h * 100
        except Exception:
            pass

    logger.warning("获取 BTC 涨跌幅失败（所有数据源）")
    return 0.0
