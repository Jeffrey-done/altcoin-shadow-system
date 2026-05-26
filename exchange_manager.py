#!/usr/bin/env python3
"""
多交易所管理模块 v3.0 — 每交易所独立账户
统一封装 Binance / OKX / Gate.io 的数据接口，提供：
  - K线/Ticker 数据获取
  - OI（持仓量）变化率
  - 资金费率
  - 合约品种列表
  - 费率交叉验证（多所费率都异常 = 信号更强）

设计原则 (v3.0 变更)：
  - 每个交易所拥有独立的账户配置（资金池、杠杆、风控参数）
  - 公共数据实例：无认证单例，用于读取行情/费率
  - 认证实例：按账户创建，使用该账户对应的 API 凭证和配置
  - 通过 get_exchange_config() 获取该交易所的独立账户参数
  - 单个交易所故障不影响其他交易所运行（优雅降级）
"""

from typing import Optional, Dict

import threading

import ccxt
import requests

import config
from common import setup_logger

logger = setup_logger("exchange_manager")


# ══════════════════════════════════════════════════════════════════
#  交易所实例管理
# ══════════════════════════════════════════════════════════════════

# 公共数据实例（无认证，单例复用）
_binance_instance: Optional[ccxt.binance] = None
_okx_instance: Optional[ccxt.okx] = None
_gate_instance: Optional[ccxt.gate] = None

# 认证实例缓存：key = (exchange_name, account_id)
_authenticated_instances: Dict[tuple, ccxt.Exchange] = {}
_auth_cache_lock = threading.Lock()


# H10: ccxt HTTP 超时（毫秒）。所有走 ccxt 的 fetch_*/create_order 调用
# 都受这个限制，防止 OKX/Binance 抽风时把进程拖死到外层 600s 任务超时。
DEFAULT_CCXT_TIMEOUT_MS = 8000
# 下单操作用更长超时，防止高波动时延迟触发展开重试导致重复订单
ORDER_CREATE_TIMEOUT_MS = 15000


def make_exchange(
    name: str,
    *,
    api_key: str = '',
    secret: str = '',
    passphrase: str = '',
    default_type: str = '',
    enable_rate_limit: bool = True,
    timeout_ms: int = DEFAULT_CCXT_TIMEOUT_MS,
    extra_options: Optional[dict] = None,
):
    """
    统一的 ccxt 实例工厂。所有创建 ccxt 实例的地方都应该走这里，
    保证 `timeout` 必填，避免有人写出"无 timeout 的 ccxt 实例"。

    Args:
      name: 'binance' 或 'okx'
      api_key/secret/passphrase: 凭证（留空 = 公共数据实例）
      default_type: ccxt 的 options.defaultType
        - Binance: 'future' 表示 USDT-M 永续合约
        - OKX:    'swap'   表示 USDT 永续合约
        - 留空:   交易所默认（一般是现货）
      enable_rate_limit: 是否启用 ccxt 自带 rate limiter
      timeout_ms: HTTP 请求超时（毫秒），默认 8000
      extra_options: 合并到 ccxt.options 的额外配置

    Returns:
      ccxt.Exchange 实例
    """
    cfg: dict = {
        'enableRateLimit': enable_rate_limit,
        'timeout': timeout_ms,
    }
    if api_key:
        cfg['apiKey'] = api_key
    if secret:
        cfg['secret'] = secret
    if passphrase:
        # OKX 用 'password' 字段表示 passphrase
        cfg['password'] = passphrase

    options: dict = {}
    if default_type:
        options['defaultType'] = default_type
    if extra_options:
        options.update(extra_options)
    if options:
        cfg['options'] = options

    if name == 'binance':
        return ccxt.binance(cfg)
    if name == 'okx':
        return ccxt.okx(cfg)
    if name == 'gate':
        return ccxt.gate(cfg)
    raise ValueError(f"未知交易所: {name}（支持: 'binance' / 'okx' / 'gate'）")


def get_binance(authenticated: bool = False, account_id: str = None) -> ccxt.binance:
    """获取 Binance 交易所实例

    公共数据：无认证单例（节省资源）。
    认证版本：凭证优先级 admin_secrets 指定账户 > 活跃账户 > .env 环境变量。

    Args:
        authenticated: 是否需要认证实例（下单等操作需要）
        account_id: 指定账户 ID（多账户场景下指定用哪个账户的凭证）
    """
    global _binance_instance
    if not authenticated:
        if _binance_instance is None:
            _binance_instance = make_exchange('binance')
        return _binance_instance

    # 认证版本：按 account_id 缓存
    cache_key = ('binance', account_id or '_active')
    with _auth_cache_lock:
        if cache_key in _authenticated_instances:
            return _authenticated_instances[cache_key]

    try:
        from admin_secrets import get_exchange_credentials
        creds = get_exchange_credentials('binance', account_id=account_id)
        api_key = creds.get('api_key', '')
        secret = creds.get('secret', '')
    except Exception as e:
        logger.debug(f"admin_secrets 不可用，fallback 到环境变量: {e}")
        import os
        api_key = os.environ.get('BINANCE_API_KEY', '')
        secret = os.environ.get('BINANCE_SECRET', '')

    if not api_key or not secret:
        logger.warning("Binance API 凭证未配置（admin_secrets 和 .env 都没有）")

    instance = make_exchange(
        'binance',
        api_key=api_key,
        secret=secret,
        default_type='future',
    )
    with _auth_cache_lock:
        _authenticated_instances[cache_key] = instance
    return instance


def get_okx(authenticated: bool = False, account_id: str = None) -> Optional[ccxt.okx]:
    """
    获取 OKX 交易所实例。
    如果 OKX 未启用或连接失败，返回 None（优雅降级）。

    Args:
        authenticated: 是否需要认证实例
        account_id: 指定账户 ID
    """
    global _okx_instance
    if not config.OKX_ENABLED:
        return None

    if not authenticated:
        if _okx_instance is None:
            try:
                _okx_instance = make_exchange('okx')
            except Exception as e:
                logger.warning(f"OKX 初始化失败: {e}")
                return None
        return _okx_instance

    # 认证版本：按 account_id 缓存
    cache_key = ('okx', account_id or '_active')
    with _auth_cache_lock:
        if cache_key in _authenticated_instances:
            return _authenticated_instances[cache_key]

    try:
        from admin_secrets import get_exchange_credentials
        creds = get_exchange_credentials('okx', account_id=account_id)
        api_key = creds.get('api_key', '')
        secret = creds.get('secret', '')
        passphrase = creds.get('passphrase', '')
    except Exception as e:
        logger.debug(f"admin_secrets 不可用，fallback 到环境变量: {e}")
        import os
        api_key = os.environ.get('OKX_API_KEY', '')
        secret = os.environ.get('OKX_SECRET', '')
        passphrase = os.environ.get('OKX_PASSPHRASE', '')
    if not api_key or not secret or not passphrase:
        logger.warning("OKX API 凭证未配置，无法使用认证接口")
        return None

    try:
        instance = make_exchange(
            'okx',
            api_key=api_key,
            secret=secret,
            passphrase=passphrase,
        )
        with _auth_cache_lock:
            _authenticated_instances[cache_key] = instance
        return instance
    except Exception as e:
        logger.warning(f"OKX 认证实例创建失败: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
#  Gate.io 交易所实例
# ══════════════════════════════════════════════════════════════════

# Gate.io 配置 — 已禁用（2026-05 审计：路由层从未支持 gate 分支）
# 保留 get_gate() 函数签名以兼容可能的外部调用，但始终返回 None
GATE_ENABLED = False
GATE_LIVE_MODE = False
GATE_DEFAULT_LEVERAGE = 10


def get_gate(authenticated: bool = False, account_id: str = None) -> Optional[ccxt.gate]:
    """
    获取 Gate.io 交易所实例。

    如果 Gate.io 未启用或连接失败，返回 None（优雅降级）。

    Args:
        authenticated: 是否需要认证实例
        account_id: 指定账户 ID
    """
    global _gate_instance
    if not GATE_ENABLED:
        return None

    if not authenticated:
        if _gate_instance is None:
            try:
                _gate_instance = make_exchange(
                    'gate',
                    default_type='swap',
                    extra_options={'defaultSettle': 'usdt'},
                )
            except Exception as e:
                logger.warning(f"Gate.io 初始化失败: {e}")
                return None
        return _gate_instance

    # 认证版本：按 account_id 缓存
    cache_key = ('gate', account_id or '_active')
    with _auth_cache_lock:
        if cache_key in _authenticated_instances:
            return _authenticated_instances[cache_key]

    try:
        from admin_secrets import get_exchange_credentials
        creds = get_exchange_credentials('gate', account_id=account_id)
        api_key = creds.get('api_key', '')
        secret = creds.get('secret', '')
    except Exception as e:
        logger.debug(f"admin_secrets Gate.io 不可用，fallback 到环境变量: {e}")
        import os
        api_key = os.environ.get('GATE_API_KEY', '')
        secret = os.environ.get('GATE_SECRET', '')

    if not api_key or not secret:
        logger.warning("Gate.io API 凭证未配置")
        return None

    try:
        instance = make_exchange(
            'gate',
            api_key=api_key,
            secret=secret,
            default_type='swap',
            extra_options={'defaultSettle': 'usdt'},
        )
        with _auth_cache_lock:
            _authenticated_instances[cache_key] = instance
        return instance
    except Exception as e:
        logger.warning(f"Gate.io 认证实例创建失败: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
#  统一获取器（通用接口）
# ══════════════════════════════════════════════════════════════════

def get_exchange(name: str, authenticated: bool = False,
                 account_id: str = None) -> Optional[ccxt.Exchange]:
    """
    统一的交易所实例获取接口。

    Args:
        name: 交易所名称 ('binance', 'okx', 'gate')
        authenticated: 是否需要认证实例
        account_id: 指定账户 ID

    Returns:
        ccxt.Exchange 实例，或 None（交易所未启用/连接失败）
    """
    name = name.lower()
    if name == 'binance':
        return get_binance(authenticated=authenticated, account_id=account_id)
    elif name == 'okx':
        return get_okx(authenticated=authenticated, account_id=account_id)
    elif name == 'gate':
        return get_gate(authenticated=authenticated, account_id=account_id)
    else:
        logger.warning(f"未知交易所: {name}")
        return None


def get_exchange_config(exchange: str, account_id: str = None) -> dict:
    """
    获取指定交易所的独立账户配置（leverage, stake, risk 等）。

    这是获取交易所配置的推荐统一入口。
    优先级：runtime_config > admin_secrets > config/_defaults > 默认值

    Args:
        exchange: 交易所名称 ('binance', 'okx', 'gate')
        account_id: 账户 ID，None 使用活跃账户

    Returns:
        完整配置字典：{
            'account_balance': 100,
            'leverage': 10,
            'default_stake': 30,
            'live_mode': False,
            'slippage_alert_pct': 1.0,
            'risk': {...},
            'compound': {...},
            'tp_sl': {...},
        }
    """
    try:
        from runtime_config import get_effective_exchange_config
        return get_effective_exchange_config(exchange, account_id)
    except Exception as e:
        logger.debug(f"get_exchange_config fallback to config/_defaults: {e}")
        # Fallback：从 config/_defaults 读取
        try:
            exchange_accounts = getattr(config, 'EXCHANGE_ACCOUNTS', {})
            return exchange_accounts.get(exchange.lower(), {})
        except Exception:
            return {}


def invalidate_authenticated_cache(exchange: str = None, account_id: str = None):
    """
    清除认证实例缓存。当凭证被更新时调用，强制下次创建新实例。

    Args:
        exchange: 指定交易所（None = 全部清除）
        account_id: 指定账户（None = 全部清除）
    """
    global _authenticated_instances
    if exchange is None and account_id is None:
        with _auth_cache_lock:
            _authenticated_instances.clear()
        return

    keys_to_remove = []
    with _auth_cache_lock:
        for key in _authenticated_instances:
            exch, acc = key
            if exchange and exch != exchange.lower():
                continue
            if account_id and acc != account_id:
                continue
            keys_to_remove.append(key)

        for key in keys_to_remove:
            del _authenticated_instances[key]


def gate_has_swap(symbol: str) -> bool:
    """检查 Gate.io 是否有该币种的永续合约"""
    gate = get_gate()
    if not gate:
        return False
    try:
        gate.load_markets()
        # Gate.io swap symbol 格式: BTC/USDT:USDT
        gate_symbol = f"{symbol}:USDT" if ':' not in symbol else symbol
        return gate_symbol in gate.markets
    except Exception:
        return False


def get_gate_funding_rate(symbol: str) -> Optional[float]:
    """获取 Gate.io 当前资金费率（%/8h）"""
    gate = get_gate()
    if not gate:
        return None
    try:
        gate_symbol = f"{symbol}:USDT" if ':' not in symbol else symbol
        info = gate.fetch_funding_rate(gate_symbol)
        rate = float(info.get('fundingRate', 0)) * 100  # 转为百分比
        return rate
    except Exception as e:
        logger.debug(f"Gate.io 费率获取失败 ({symbol}): {e}")
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
_okx_swap_loaded_at: float = 0.0
# TTL: OKX 合约列表缓存有效期（秒）。4小时刷新一次，兼顾新币上线感知和 API 节约。
_OKX_SWAP_CACHE_TTL: float = 4 * 3600


def _refresh_okx_swap_symbols() -> None:
    """从 OKX 重新加载永续合约品种列表到内存缓存"""
    global _okx_swap_symbols, _okx_swap_loaded_at
    import time as _time
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
        if _okx_swap_symbols is None:
            _okx_swap_symbols = set()
        # 加载失败时保留旧缓存，但把 loaded_at 设为当前时间的一半 TTL，
        # 这样 2 小时后会重试（而不是每次调用都重试导致 API 洪水）
        _okx_swap_loaded_at = _time.time() - _OKX_SWAP_CACHE_TTL / 2
        return
    _okx_swap_loaded_at = _time.time()
    logger.info(f"OKX 合约列表已刷新: {len(_okx_swap_symbols)} 个永续品种")


def okx_has_swap(symbol: str) -> bool:
    """检查 OKX 是否有该币种的永续合约（带 TTL 缓存，每 4 小时刷新）"""
    global _okx_swap_symbols, _okx_swap_loaded_at
    import time as _time
    if not config.OKX_ENABLED:
        return False

    # 首次加载或缓存过期 → 刷新
    if _okx_swap_symbols is None or (_time.time() - _okx_swap_loaded_at > _OKX_SWAP_CACHE_TTL):
        _refresh_okx_swap_symbols()

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



def cross_validate_price(symbol: str, binance_price: float) -> dict:
    """
    对比 Binance 和 OKX 的价格，返回偏差信息。

    Returns:
        {
            'available': bool,      # OKX 是否有数据（异常 / OKX 未启用 / 无数据 → False）
            'okx_price': float,     # OKX 价格
            'divergence_pct': float, # 偏差百分比（绝对值）
            'pass': bool,           # 是否通过（偏差<阈值 或 数据不可用按保守方式放行）
            'reason': str,          # 描述：通过为 ''，不通过或异常写明原因
        }

    H9: 异常路径不再静默 fail-open。异常时 `available=False` 并写 reason 记录原因 +
    logger.warning，供上层风控门在"偏差超限"和"数据缺失"两种场景下做不同处理
    （目前策略保持"无数据时不阻止开仓"，但至少日志可见，避免 OKX 抽风时整个
    价格偏差风控悄悄失效却没人知道）。
    """
    result = {
        'available': False,
        'okx_price': 0,
        'divergence_pct': 0,
        'pass': True,
        'reason': '',
    }

    if not config.OKX_ENABLED:
        result['reason'] = 'OKX 未启用'
        return result

    # H10: 防御性门禁——OKX 没有该币种合约时直接返回，
    # 不浪费一次（可能卡死的）网络调用。调用方应当先用 okx_has_swap 过滤，
    # 这里是兜底，避免任何调用方漏掉门禁后又把整个子进程拖死。
    try:
        if not okx_has_swap(symbol):
            result['reason'] = 'OKX 无该合约'
            return result
    except Exception as e:
        result['reason'] = f'OKX 合约列表不可用: {type(e).__name__}: {e}'
        logger.warning(f"cross_validate_price 合约列表查询失败 ({symbol}): {e}")
        return result

    try:
        okx = get_okx()
        if okx is None:
            result['reason'] = 'OKX 实例不可用'
            return result

        ticker = okx.fetch_ticker(symbol)
        okx_price = ticker.get('last', 0)
        if okx_price <= 0:
            result['reason'] = f'OKX ticker 无效 (last={okx_price})'
            return result

        result['available'] = True
        result['okx_price'] = okx_price

        divergence = abs(binance_price - okx_price) / binance_price * 100
        result['divergence_pct'] = round(divergence, 2)

        if divergence > config.PRICE_DIVERGENCE_MAX_PCT:
            result['pass'] = False
            result['reason'] = (
                f"价格偏差{divergence:.1f}%（Binance={binance_price:.6f} "
                f"vs OKX={okx_price:.6f}）"
            )

        return result
    except Exception as e:
        # H9: 不再静默 fail-open；标记数据不可用，写清原因和日志
        result['available'] = False
        result['reason'] = f'OKX 查询异常: {type(e).__name__}: {e}'
        logger.warning(f"cross_validate_price 异常 ({symbol}): {e}")
        return result
