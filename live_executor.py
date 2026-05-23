#!/usr/bin/env python3
"""
实盘执行模块 v3.0
支持 Binance + OKX 双交易所实盘执行 + 统一路由接口。

工作模式：
  - LIVE_MODE=False 且 OKX_LIVE_MODE=False：纸上模拟（execute_* 返回 SHADOW）
  - LIVE_MODE=True：通过 Binance API 真实下单（推荐为主交易所）
  - OKX_LIVE_MODE=True：通过 OKX API 真实下单
  - 两个同时 True：由 execute_open(exchange_name=...) 路由决定走哪家

关键特性（v3.0，与 Binance 对齐）：
  - OKX 补齐 client_order_id（clOrdId）防止网络重试重复下单
  - OKX 补齐滑点告警（> SLIPPAGE_ALERT_PCT 写 WARN，配合 TG 推送）
  - 统一 execute_open / execute_close 返回格式（含 amount / 订单号）
  - 未配置 API 凭证时优雅降级，不让扫描器崩溃

⚠️ 警告：开启实盘前务必：
  1. 纸上交易验证至少2周
  2. 确认胜率>50%、盈亏比>1.5
  3. 从最小仓位开始（DEFAULT_STAKE=20）
  4. 配置好对应交易所的 API 凭证
  5. Binance 合约账户设为"对冲模式(Hedge Mode)"
  6. OKX 合约账户设为"双向持仓模式"
"""

import os
import threading
from typing import Optional

import ccxt

import config
from common import setup_logger, log_execution_event, make_idempotency_key, now_ms
from exchange_manager import get_okx

logger = setup_logger("live_executor")

def _classify_exec_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if 'insufficient' in msg or 'margin' in msg or 'balance' in msg:
        return 'INSUFFICIENT_MARGIN'
    if 'invalid' in msg and ('precision' in msg or 'quantity' in msg or 'amount' in msg):
        return 'INVALID_QUANTITY'
    if 'minnotional' in msg or 'lot_size' in msg or 'precision' in msg or 'tick size' in msg:
        return 'EXCHANGE_FILTER_REJECTED'
    if 'timeout' in msg or 'timed out' in msg:
        return 'NETWORK_TIMEOUT'
    if 'rate limit' in msg or 'too many requests' in msg:
        return 'RATE_LIMITED'
    if 'auth' in msg or 'api-key' in msg or 'signature' in msg or 'permission' in msg:
        return 'AUTH_FAILED'
    return 'EXCHANGE_ERROR'




def _ensure_client_order_id(client_order_id: Optional[str], prefix: str, symbol: str, exchange_name: str) -> str:
    """若上层未传入幂等键，则自动生成稳定可追踪的 client_order_id。"""
    if client_order_id:
        return client_order_id
    ts = now_ms()
    raw = make_idempotency_key('altcoin_shadow', prefix, symbol, ts)
    
    if exchange_name == 'okx':
        # OKX 只允许字母数字，长度 <= 32
        return (prefix[:3] + raw + str(ts)[-4:])[:32]
    base = symbol.replace('/USDT', '').replace('/', '').replace('-', '')
    return f"{prefix}-{base}-{str(ts)[-6:]}-{raw[:10]}"[:36]


def _event_base(exchange_name: str, symbol: str, direction: str, client_order_id: str, account_id: Optional[str]) -> dict:
    return {
        'exchange': exchange_name,
        'symbol': symbol,
        'direction': direction,
        'client_order_id': client_order_id,
        'account_id': account_id or '',
    }

def _fail_result(msg: str, code: str, **kwargs) -> dict:
    d = {'success': False, 'error': msg, 'error_code': code}
    d.update(kwargs)
    return d



# ══════════════════════════════════════════════════════════════════
#  Binance 实盘
# ══════════════════════════════════════════════════════════════════

_exchange_cache: dict = {}
_exchange_cache_lock = threading.Lock()


def get_live_exchange(account_id: Optional[str] = None):
    """创建已认证的 Binance 合约交易所实例（缓存结果）

    凭证优先级：
      - 指定 account_id 时：使用该账户的独立凭证（多账户并行模式）
      - 未指定时：admin_secrets.json 活跃账户 > .env 环境变量
    （admin panel 修改后立即生效，无需重启进程）

    缓存：每 (exchange_name, account_id) 组合缓存一个实例，避免重复
    创建导致 rate limiter 和 HTTP 会话丢失。
    """
    cache_key = ('binance', account_id or '')
    with _exchange_cache_lock:
        cached = _exchange_cache.get(cache_key)
        if cached is not None:
            return cached

    try:
        if account_id:
            from admin_secrets import get_account_exchange_credentials
            creds = get_account_exchange_credentials('binance', account_id)
        else:
            from admin_secrets import get_exchange_credentials
            creds = get_exchange_credentials('binance')
        api_key = creds.get('api_key', '')
        secret = creds.get('secret', '')
    except Exception as e:
        logger.debug(f"admin_secrets 不可用，fallback 到环境变量: {e}")
        api_key = os.environ.get('BINANCE_API_KEY', '')
        secret = os.environ.get('BINANCE_SECRET', '')

    if not api_key or not secret:
        logger.error("BINANCE_API_KEY 或 BINANCE_SECRET 未设置！无法实盘交易")
        return None

    from exchange_manager import make_exchange
    exchange = make_exchange(
        'binance',
        api_key=api_key,
        secret=secret,
        default_type='future',
    )

    with _exchange_cache_lock:
        _exchange_cache[cache_key] = exchange
    return exchange


def _check_slippage(symbol: str, exchange_name: str,
                    ref_price: float, fill_price: float) -> Optional[str]:
    """
    滑点校验工具：返回告警消息（若超阈值），否则 None。
    ref_price 一般是下单前 ticker 的 last；fill_price 是成交均价。
    """
    if ref_price <= 0 or fill_price <= 0:
        return None
    slippage_pct = abs(fill_price - ref_price) / ref_price * 100
    if slippage_pct > config.SLIPPAGE_ALERT_PCT:
        msg = (
            f"[{exchange_name}] 滑点异常 {symbol}: "
            f"ticker={ref_price:.6f} 成交={fill_price:.6f} ({slippage_pct:.2f}%)"
        )
        logger.warning(f"⚠️ {msg}")
        return msg
    return None


def _amount_to_precision(exchange, symbol: str, amount: float) -> float:
    """
    用 ccxt 的 amount_to_precision 裁剪下单数量到交易所 stepSize。
    裁剪后为 0 的返回 0（上层应拒单）。失败时返回原值（fallback）。

    Fix: 如果首次调用失败（通常因为 exchange.markets 尚未加载），
    自动调 load_markets() 后重试一次。这确保即使 get_live_exchange()
    返回的实例还没加载过市场列表，精度裁剪仍能正常工作。
    """
    try:
        precise = float(exchange.amount_to_precision(symbol, amount))
        return precise if precise > 0 else 0.0
    except Exception as e:
        logger.debug(f"amount_to_precision 首次失败 ({symbol}, {amount}): {e}，尝试 load_markets")
        # 可能是 markets 未加载导致的 KeyError / NoneType，尝试加载后重试
        try:
            exchange.load_markets()
            precise = float(exchange.amount_to_precision(symbol, amount))
            return precise if precise > 0 else 0.0
        except Exception as e2:
            logger.debug(f"amount_to_precision 重试仍失败 ({symbol}, {amount}): {e2}，返回原值")
            return amount




def _round_price(exchange, symbol: str, price: float) -> float:
    """Round a trigger/order price to exchange precision."""
    try:
        precise = float(exchange.price_to_precision(symbol, price))
        return precise if precise > 0 else 0.0
    except Exception as e:
        logger.debug(f"price_to_precision 失败 ({symbol}, {price}): {e}")
        return round(price, 6)


def place_binance_short_protection(symbol: str, amount: float,
                                   hard_stop_price: float,
                                   tp1_price: float,
                                   account_id: Optional[str] = None,
                                   stop_client_order_id: Optional[str] = None,
                                   tp1_client_order_id: Optional[str] = None) -> dict:
    """Place Binance protective exit orders for a live short position."""
    if not config.LIVE_MODE:
        return {"success": True, "stop_order_id": "SHADOW", "tp1_order_id": "SHADOW", "error": ""}

    exchange = get_live_exchange(account_id)
    if not exchange:
        return {"success": False, "stop_order_id": "", "tp1_order_id": "", "error": "交易所连接失败"}

    try:
        amount = _amount_to_precision(exchange, symbol, amount)
        if amount <= 0:
            return {"success": False, "stop_order_id": "", "tp1_order_id": "", "error": "保护单数量裁剪后为 0"}

        stop_price = _round_price(exchange, symbol, hard_stop_price)
        tp1_trigger_price = _round_price(exchange, symbol, tp1_price)
        if stop_price <= 0 or tp1_trigger_price <= 0:
            return {"success": False, "stop_order_id": "", "tp1_order_id": "", "error": "保护单价格无效"}

        stop_params = {
            'positionSide': 'SHORT',
                        'stopPrice': stop_price,
            'workingType': 'MARK_PRICE',
        }
        tp1_params = {
            'positionSide': 'SHORT',
                        'stopPrice': tp1_trigger_price,
            'workingType': 'MARK_PRICE',
        }
        if stop_client_order_id:
            stop_params['newClientOrderId'] = stop_client_order_id
        if tp1_client_order_id:
            tp1_params['newClientOrderId'] = tp1_client_order_id

        stop_order = exchange.create_order(symbol=symbol, type='STOP_MARKET', side='buy', amount=amount, params=stop_params)
        tp1_order = exchange.create_order(symbol=symbol, type='TAKE_PROFIT_MARKET', side='buy', amount=amount, params=tp1_params)

        logger.info(f"✅ Binance 保护单已挂: {symbol} | STOP={stop_price:.6f} | TP1={tp1_trigger_price:.6f} | amount={amount:.4f}")
        return {
            "success": True,
            "stop_order_id": stop_order.get('id', ''),
            "tp1_order_id": tp1_order.get('id', ''),
            "error": "",
        }
    except Exception as e:
        logger.error(f"❌ Binance 保护单挂单失败 ({symbol}): {e}")
        return {"success": False, "stop_order_id": "", "tp1_order_id": "", "error": str(e)}


def cancel_binance_open_orders(symbol: str, account_id: Optional[str] = None) -> dict:
    """Cancel all open Binance orders for the symbol (normal + algo)."""
    if not config.LIVE_MODE:
        return {"success": True, "cancelled": 0, "error": ""}

    exchange = get_live_exchange(account_id)
    if not exchange:
        return {"success": False, "cancelled": 0, "error": "交易所连接失败"}

    cancelled = 0
    try:
        orders = exchange.fetch_open_orders(symbol)
        for order in orders:
            try:
                exchange.cancel_order(order.get('id'), symbol)
                cancelled += 1
            except Exception as e:
                logger.warning(f"撤单失败 {symbol} {order.get('id')}: {e}")
    except Exception as e:
        logger.debug(f"普通挂单撤单路径失败（非致命）: {e}")

    algo = cancel_binance_open_algo_orders(symbol, account_id=account_id)
    cancelled += int(algo.get('cancelled') or 0)
    if not algo.get('success'):
        return {"success": False, "cancelled": cancelled, "error": algo.get('error', '')}

    return {"success": True, "cancelled": cancelled, "error": ""}


def execute_open_short(symbol: str, stake: float, leverage: int = config.LEVERAGE,
                       client_order_id: Optional[str] = None,
                       account_id: Optional[str] = None) -> dict:
    """
    实盘开空单（Binance）。

    参数:
      symbol: ccxt 格式 (如 'PEPE/USDT')
      stake: 保证金 (USDT)
      leverage: 杠杆倍数
      client_order_id: 幂等键（newClientOrderId），网络重试时避免重复下单

    返回:
      {"success": True/False, "order_id": str, "price": float, "amount": float, "error": str}

    影子模式（LIVE_MODE=False）:
      直接返回 success=True, price=0，上层会用 ticker 价记账。
    """
    if not config.LIVE_MODE:
        return {"success": True, "order_id": "SHADOW", "price": 0, "amount": 0, "error": ""}

    exchange = get_live_exchange(account_id)
    if not exchange:
        return _fail_result("交易所连接失败", "EXCHANGE_UNAVAILABLE", order_id="", price=0, amount=0)

    try:
        # 设置杠杆（失败时仅告警，由交易所返回错误中断流程）
        try:
            exchange.set_leverage(leverage, symbol)
        except Exception as e:
            logger.warning(f"set_leverage 失败 ({symbol}): {e}")

        # 获取下单前 ticker（作为滑点校验基准，不作为成交价）
        ticker = exchange.fetch_ticker(symbol)
        ref_price = ticker['last']
        notional = stake * leverage
        amount = notional / ref_price

        # 裁剪到交易所数量精度（stepSize），避免 -4005 LOT_SIZE 报错
        amount = _amount_to_precision(exchange, symbol, amount)
        if amount <= 0:
            return _fail_result(f"数量裁剪后为 0（名义仓位 {notional}U 可能低于 minNotional）", "INVALID_QUANTITY", order_id="", price=0, amount=0)

        params = {'positionSide': 'SHORT'}
        if client_order_id:
            # Binance 合约使用 newClientOrderId 幂等键
            params['newClientOrderId'] = client_order_id

        order = exchange.create_order(
            symbol=symbol, type='market', side='sell',
            amount=amount, params=params,
        )

        # 必须用成交均价作为 entry_price，而不是 ref_price
        fill_price = float(order.get('average') or order.get('price') or ref_price)
        filled_amount = float(order.get('filled') or amount)

        _check_slippage(symbol, 'Binance', ref_price, fill_price)

        logger.info(
            f"✅ Binance 开空: {symbol} | 成交={fill_price:.6f} | "
            f"数量={filled_amount:.4f} | 杠杆={leverage}x | 订单={order['id']}"
        )

        return {
            "success": True,
            "order_id": order.get('id', ''),
            "price": fill_price,
            "amount": filled_amount,
            "error": "",
        }

    except Exception as e:
        logger.error(f"❌ Binance 开空失败 ({symbol}): {e}")
        return _fail_result(str(e), _classify_exec_error(e), order_id="", price=0, amount=0)


def execute_open_long(symbol: str, stake: float, leverage: int = config.LEVERAGE,
                      client_order_id: Optional[str] = None,
                      account_id: Optional[str] = None) -> dict:
    """
    实盘开多单（当前做空系统未使用，保留给未来扩展）。
    """
    if not config.LIVE_MODE:
        return {"success": True, "order_id": "SHADOW", "price": 0, "amount": 0, "error": ""}

    exchange = get_live_exchange(account_id)
    if not exchange:
        return _fail_result("交易所连接失败", "EXCHANGE_UNAVAILABLE", order_id="", price=0, amount=0)

    try:
        try:
            exchange.set_leverage(leverage, symbol)
        except Exception as e:
            logger.warning(f"set_leverage 失败 ({symbol}): {e}")

        ticker = exchange.fetch_ticker(symbol)
        ref_price = ticker['last']
        notional = stake * leverage
        amount = notional / ref_price

        # 裁剪到交易所数量精度
        amount = _amount_to_precision(exchange, symbol, amount)
        if amount <= 0:
            return _fail_result(f"数量裁剪后为 0（名义仓位 {notional}U 可能低于 minNotional）", "INVALID_QUANTITY", order_id="", price=0, amount=0)

        params = {'positionSide': 'LONG'}
        if client_order_id:
            params['newClientOrderId'] = client_order_id

        order = exchange.create_order(
            symbol=symbol, type='market', side='buy',
            amount=amount, params=params,
        )

        fill_price = float(order.get('average') or order.get('price') or ref_price)
        filled_amount = float(order.get('filled') or amount)

        _check_slippage(symbol, 'Binance', ref_price, fill_price)

        logger.info(
            f"✅ Binance 开多: {symbol} | 成交={fill_price:.6f} | "
            f"数量={filled_amount:.4f} | 杠杆={leverage}x | 订单={order['id']}"
        )

        return {
            "success": True,
            "order_id": order.get('id', ''),
            "price": fill_price,
            "amount": filled_amount,
            "error": "",
        }

    except Exception as e:
        logger.error(f"❌ Binance 开多失败 ({symbol}): {e}")
        return _fail_result(str(e), _classify_exec_error(e), order_id="", price=0, amount=0)




def get_binance_position_amount(symbol: str, direction: str = 'SHORT',
                                account_id: Optional[str] = None) -> float:
    """Return current Binance position contracts for given side.

    SHORT returns absolute contracts of SHORT leg, LONG likewise.
    """
    if not config.LIVE_MODE:
        return 0.0
    exchange = get_live_exchange(account_id)
    if not exchange:
        return 0.0
    try:
        market = exchange.market(symbol)
        ex_symbol = market.get('symbol') or market.get('id') or symbol
        positions = exchange.fetch_positions([ex_symbol])
        want = (direction or 'SHORT').upper()
        for p in positions:
            side = str(p.get('side') or '').lower()
            contracts = float(p.get('contracts') or 0)
            if want == 'SHORT' and side == 'short':
                return max(0.0, contracts)
            if want == 'LONG' and side == 'long':
                return max(0.0, contracts)
        return 0.0
    except Exception as e:
        logger.warning(f"读取 Binance 持仓失败 ({symbol}): {e}")
        return -1.0

def execute_close_position(symbol: str, direction: str, amount: float,
                           client_order_id: Optional[str] = None,
                           account_id: Optional[str] = None) -> dict:
    """
    Binance 实盘平仓。

    参数:
      symbol: ccxt 格式
      direction: 'SHORT' 或 'LONG'
      amount: 平仓数量
      client_order_id: 平仓幂等键（防止 evaluate 在同一秒被触发多次重复平仓）
      account_id: 指定账户 ID（多账户模式）；None 使用活跃账户
    """
    if not config.LIVE_MODE:
        return {"success": True, "order_id": "SHADOW", "price": 0, "amount": amount, "error": ""}

    exchange = get_live_exchange(account_id)
    if not exchange:
        return _fail_result("交易所连接失败", "EXCHANGE_UNAVAILABLE", order_id="", price=0, amount=0)

    try:
        if direction == 'SHORT':
            side = 'buy'       # 平空 = 买入
            position_side = 'SHORT'
        else:
            side = 'sell'      # 平多 = 卖出
            position_side = 'LONG'

        params = {'positionSide': position_side, 'reduceOnly': True}
        if client_order_id:
            params['newClientOrderId'] = client_order_id

        # 裁剪到交易所数量精度
        amount = _amount_to_precision(exchange, symbol, amount)
        if amount <= 0:
            return _fail_result("平仓数量裁剪后为 0（精度不足）", "INVALID_QUANTITY", order_id="", price=0, amount=0)

        order = exchange.create_order(
            symbol=symbol, type='market', side=side,
            amount=amount, params=params,
        )

        fill_price = float(order.get('average') or order.get('price') or 0)
        filled_amount = float(order.get('filled') or amount)

        logger.info(
            f"✅ Binance 平仓: {symbol} {direction} | 数量={filled_amount:.4f} | "
            f"成交={fill_price:.6f} | 订单={order['id']}"
        )

        return {
            "success": True,
            "order_id": order.get('id', ''),
            "price": fill_price,
            "amount": filled_amount,
            "error": "",
        }

    except Exception as e:
        logger.error(f"❌ Binance 平仓失败 ({symbol}): {e}")
        return _fail_result(str(e), _classify_exec_error(e), order_id="", price=0, amount=0)


def check_live_balance(account_id: Optional[str] = None) -> dict:
    """查询合约账户余额（Binance）

    参数:
      account_id: 指定账户 ID（多账户模式）；None 使用活跃账户
    """
    if not config.LIVE_MODE:
        return {"available": config.ACCOUNT_BALANCE, "total": config.ACCOUNT_BALANCE}

    exchange = get_live_exchange(account_id)
    if not exchange:
        return {"available": 0, "total": 0}

    try:
        balance = exchange.fetch_balance({'type': 'future'})
        usdt = balance.get('USDT', {})
        return {
            "available": float(usdt.get('free', 0)),
            "total": float(usdt.get('total', 0)),
        }
    except Exception as e:
        logger.error(f"查询余额失败: {e}")
        return {"available": 0, "total": 0}


# ══════════════════════════════════════════════════════════════════
#  OKX 实盘执行
# ══════════════════════════════════════════════════════════════════

def get_okx_live_exchange(account_id: Optional[str] = None):
    """创建已认证的 OKX 合约交易所实例（支持多账户）"""
    if account_id:
        # 多账户模式：使用指定账户的凭证
        try:
            from admin_secrets import get_account_exchange_credentials
            creds = get_account_exchange_credentials('okx', account_id)
            api_key = creds.get('api_key', '')
            secret = creds.get('secret', '')
            passphrase = creds.get('passphrase', '')
        except Exception as e:
            logger.warning(f"获取账户 {account_id} OKX 凭证失败: {e}")
            return None
        if not api_key or not secret or not passphrase:
            return None
        try:
            # H10: 走 exchange_manager 工厂，强制带 timeout
            from exchange_manager import make_exchange
            return make_exchange(
                'okx',
                api_key=api_key,
                secret=secret,
                passphrase=passphrase,
            )
        except Exception as e:
            logger.warning(f"OKX 认证实例创建失败 (account={account_id}): {e}")
            return None
    # 默认模式：使用活跃账户
    return get_okx(authenticated=True)


def _okx_cloid(prefix: str, symbol: str, ts_ms: int) -> str:
    """
    生成 OKX clOrdId：只保留字母数字，长度 ≤32（OKX 限制）。
    prefix 建议 'sho'/'lng'/'cls' 三字母，便于风控/审计回溯。
    """
    base = symbol.replace('/USDT', '').replace('/', '').replace('-', '')
    coid = f"{prefix}{base}{ts_ms}"
    # OKX clOrdId 只能 alphanumeric，且长度 1-32
    coid = ''.join(c for c in coid if c.isalnum())
    return coid[:32]


def execute_okx_open_short(symbol: str, stake: float, leverage: int = config.OKX_DEFAULT_LEVERAGE,
                            client_order_id: Optional[str] = None,
                            account_id: Optional[str] = None) -> dict:
    """
    OKX 实盘开空单（v3.0：补齐幂等键 + 滑点告警 + 成交均价回填）。
    """
    if not config.OKX_LIVE_MODE:
        return {"success": True, "order_id": "SHADOW_OKX", "price": 0, "amount": 0, "error": ""}

    exchange = get_okx_live_exchange(account_id)
    if not exchange:
        return _fail_result("OKX 交易所连接失败", "EXCHANGE_UNAVAILABLE", order_id="", price=0, amount=0)

    try:
        # H6: 与 Binance 对齐，set_leverage 失败仅告警不中断
        # OKX 在账户模式不匹配或已设置时会抛异常，不应阻塞开仓主流程
        try:
            exchange.set_leverage(leverage, symbol, params={'mgnMode': 'cross'})
        except Exception as e:
            logger.warning(f"OKX set_leverage 失败 ({symbol}): {e}")

        ticker = exchange.fetch_ticker(symbol)
        ref_price = ticker['last']
        notional = stake * leverage
        amount = notional / ref_price

        # 裁剪到交易所数量精度（OKX 也有 lotSize）
        amount = _amount_to_precision(exchange, symbol, amount)
        if amount <= 0:
            return _fail_result(f"数量裁剪后为 0（名义仓位 {notional}U 可能低于 minNotional）", "INVALID_QUANTITY", order_id="", price=0, amount=0)

        params = {'tdMode': 'cross', 'posSide': 'short'}
        if client_order_id:
            # OKX 用 clOrdId；ccxt 已支持透传
            params['clOrdId'] = client_order_id

        order = exchange.create_order(
            symbol=symbol, type='market', side='sell',
            amount=amount, params=params,
        )

        fill_price = float(order.get('average') or order.get('price') or ref_price)
        filled_amount = float(order.get('filled') or amount)

        _check_slippage(symbol, 'OKX', ref_price, fill_price)

        logger.info(
            f"✅ OKX 开空: {symbol} | 成交={fill_price:.6f} | "
            f"数量={filled_amount:.4f} | 杠杆={leverage}x | 订单={order['id']}"
        )

        return {
            "success": True,
            "order_id": order.get('id', ''),
            "price": fill_price,
            "amount": filled_amount,
            "error": "",
        }

    except Exception as e:
        logger.error(f"❌ OKX 开空失败 ({symbol}): {e}")
        return _fail_result(str(e), _classify_exec_error(e), order_id="", price=0, amount=0)


def execute_okx_open_long(symbol: str, stake: float, leverage: int = config.OKX_DEFAULT_LEVERAGE,
                           client_order_id: Optional[str] = None,
                           account_id: Optional[str] = None) -> dict:
    """OKX 实盘开多单"""
    if not config.OKX_LIVE_MODE:
        return {"success": True, "order_id": "SHADOW_OKX", "price": 0, "amount": 0, "error": ""}

    exchange = get_okx_live_exchange(account_id)
    if not exchange:
        return _fail_result("OKX 交易所连接失败", "EXCHANGE_UNAVAILABLE", order_id="", price=0, amount=0)

    try:
        # H6: set_leverage 失败仅告警不中断
        try:
            exchange.set_leverage(leverage, symbol, params={'mgnMode': 'cross'})
        except Exception as e:
            logger.warning(f"OKX set_leverage 失败 ({symbol}): {e}")

        ticker = exchange.fetch_ticker(symbol)
        ref_price = ticker['last']
        notional = stake * leverage
        amount = notional / ref_price

        # 裁剪到交易所数量精度
        amount = _amount_to_precision(exchange, symbol, amount)
        if amount <= 0:
            return _fail_result(f"数量裁剪后为 0（名义仓位 {notional}U 可能低于 minNotional）", "INVALID_QUANTITY", order_id="", price=0, amount=0)

        params = {'tdMode': 'cross', 'posSide': 'long'}
        if client_order_id:
            params['clOrdId'] = client_order_id

        order = exchange.create_order(
            symbol=symbol, type='market', side='buy',
            amount=amount, params=params,
        )

        fill_price = float(order.get('average') or order.get('price') or ref_price)
        filled_amount = float(order.get('filled') or amount)

        _check_slippage(symbol, 'OKX', ref_price, fill_price)

        logger.info(
            f"✅ OKX 开多: {symbol} | 成交={fill_price:.6f} | "
            f"数量={filled_amount:.4f} | 杠杆={leverage}x | 订单={order['id']}"
        )

        return {
            "success": True,
            "order_id": order.get('id', ''),
            "price": fill_price,
            "amount": filled_amount,
            "error": "",
        }

    except Exception as e:
        logger.error(f"❌ OKX 开多失败 ({symbol}): {e}")
        return _fail_result(str(e), _classify_exec_error(e), order_id="", price=0, amount=0)


def execute_okx_close_position(symbol: str, direction: str, amount: float,
                                client_order_id: Optional[str] = None,
                                account_id: Optional[str] = None) -> dict:
    """OKX 实盘平仓"""
    if not config.OKX_LIVE_MODE:
        return {"success": True, "order_id": "SHADOW_OKX", "price": 0, "amount": amount, "error": ""}

    exchange = get_okx_live_exchange(account_id)
    if not exchange:
        return _fail_result("OKX 交易所连接失败", "EXCHANGE_UNAVAILABLE", order_id="", price=0, amount=0)

    try:
        if direction == 'SHORT':
            side = 'buy'
            pos_side = 'short'
        else:
            side = 'sell'
            pos_side = 'long'

        params = {'tdMode': 'cross', 'posSide': pos_side, 'reduceOnly': True}
        if client_order_id:
            params['clOrdId'] = client_order_id

        # 裁剪到交易所数量精度
        amount = _amount_to_precision(exchange, symbol, amount)
        if amount <= 0:
            return _fail_result("平仓数量裁剪后为 0（精度不足）", "INVALID_QUANTITY", order_id="", price=0, amount=0)

        order = exchange.create_order(
            symbol=symbol, type='market', side=side,
            amount=amount, params=params,
        )

        fill_price = float(order.get('average') or order.get('price') or 0)
        filled_amount = float(order.get('filled') or amount)

        logger.info(
            f"✅ OKX 平仓: {symbol} {direction} | 数量={filled_amount:.4f} | "
            f"成交={fill_price:.6f} | 订单={order['id']}"
        )

        return {
            "success": True,
            "order_id": order.get('id', ''),
            "price": fill_price,
            "amount": filled_amount,
            "error": "",
        }

    except Exception as e:
        logger.error(f"❌ OKX 平仓失败 ({symbol}): {e}")
        return _fail_result(str(e), _classify_exec_error(e), order_id="", price=0, amount=0)


def check_okx_balance(account_id: Optional[str] = None) -> dict:
    """查询 OKX 合约账户余额

    参数:
      account_id: 指定账户 ID（多账户模式）；None 使用活跃账户
    """
    if not config.OKX_LIVE_MODE:
        return {"available": 0, "total": 0}

    exchange = get_okx_live_exchange(account_id)
    if not exchange:
        return {"available": 0, "total": 0}

    try:
        balance = exchange.fetch_balance({'type': 'swap'})
        usdt = balance.get('USDT', {})
        return {
            "available": float(usdt.get('free', 0)),
            "total": float(usdt.get('total', 0)),
        }
    except Exception as e:
        logger.error(f"查询 OKX 余额失败: {e}")
        return {"available": 0, "total": 0}


# ══════════════════════════════════════════════════════════════════
#  统一执行接口（根据 exchange 参数自动路由）
# ══════════════════════════════════════════════════════════════════

def _is_exchange_live(exchange_name: str) -> bool:
    """判断某交易所是否处于实盘模式（用于上层决定是否需要 fallback 为影子）"""
    if exchange_name == 'binance':
        return config.LIVE_MODE
    if exchange_name == 'okx':
        return config.OKX_LIVE_MODE
    return False


def execute_open(symbol: str, direction: str, stake: float,
                 exchange_name: str = 'binance', leverage: Optional[int] = None,
                 client_order_id: Optional[str] = None,
                 account_id: Optional[str] = None) -> dict:
    """
    统一开仓接口，根据交易所名称路由到对应执行函数。

    参数:
      exchange_name: 'binance' | 'okx'
      direction: 'SHORT' | 'LONG'
      client_order_id: 幂等键；若为 None，系统自动生成
      account_id: 指定账户 ID（多账户并行模式）；None 使用活跃账户
    """
    prefix = 'osh' if direction == 'SHORT' else 'oln'
    effective_coid = _ensure_client_order_id(client_order_id, prefix, symbol, exchange_name)
    base = _event_base(exchange_name, symbol, direction, effective_coid, account_id)
    log_execution_event('order_created', stake=stake, leverage=leverage or 0, **base)

    if exchange_name == 'okx':
        lev = leverage or config.OKX_DEFAULT_LEVERAGE
        if direction == 'SHORT':
            result = execute_okx_open_short(symbol, stake, lev, client_order_id=effective_coid, account_id=account_id)
        else:
            result = execute_okx_open_long(symbol, stake, lev, client_order_id=effective_coid, account_id=account_id)
    else:
        lev = leverage or config.LEVERAGE
        if direction == 'SHORT':
            result = execute_open_short(symbol, stake, lev, client_order_id=effective_coid, account_id=account_id)
        else:
            result = execute_open_long(symbol, stake, lev, client_order_id=effective_coid, account_id=account_id)

    if result.get('success'):
        log_execution_event(
            'order_filled',
            order_id=result.get('order_id', ''),
            fill_price=result.get('price', 0),
            fill_amount=result.get('amount', 0),
            **base,
        )
    else:
        log_execution_event(
            'order_failed',
            error=result.get('error', ''),
            error_code=result.get('error_code', ''),
            **base,
        )
    return result


def execute_close(symbol: str, direction: str, amount: float,
                  exchange_name: str = 'binance',
                  client_order_id: Optional[str] = None,
                  account_id: Optional[str] = None) -> dict:
    """
    统一平仓接口，根据交易所名称路由。
    影子交易（exchange_name='shadow'）直接返回成功，不发真实订单。

    参数:
      account_id: 指定账户 ID（多账户模式）；None 使用活跃账户
    """
    prefix = 'csh' if direction == 'SHORT' else 'cln'
    effective_coid = _ensure_client_order_id(client_order_id, prefix, symbol, exchange_name)
    base = _event_base(exchange_name, symbol, direction, effective_coid, account_id)
    log_execution_event('close_created', amount=amount, **base)

    if exchange_name == 'shadow':
        result = {"success": True, "order_id": "SHADOW", "price": 0, "amount": amount, "error": ""}
    elif exchange_name == 'okx':
        result = execute_okx_close_position(symbol, direction, amount,
                                            client_order_id=effective_coid,
                                            account_id=account_id)
    else:
        result = execute_close_position(symbol, direction, amount,
                                        client_order_id=effective_coid,
                                        account_id=account_id)

    if result.get('success'):
        log_execution_event(
            'close_filled',
            order_id=result.get('order_id', ''),
            fill_price=result.get('price', 0),
            fill_amount=result.get('amount', 0),
            **base,
        )
    else:
        log_execution_event(
            'close_failed',
            error=result.get('error', ''),
            error_code=result.get('error_code', ''),
            **base,
        )
    return result


def make_client_order_id(prefix: str, symbol: str, exchange_name: str = 'binance',
                         ts_ms: Optional[int] = None) -> str:
    """
    生成幂等键（跨所统一接口）。
      - Binance: 允许 - 和字母数字，长度 ≤ 36
      - OKX:    只允许字母数字，长度 ≤ 32
    上层直接调用此函数，不用关心交易所差异。
    """
    import time as _time
    if ts_ms is None:
        ts_ms = int(_time.time() * 1000)

    base = symbol.replace('/USDT', '').replace('/', '').replace('-', '')

    if exchange_name == 'okx':
        return _okx_cloid(prefix, symbol, ts_ms)
    # Binance：保留连字符便于肉眼阅读
    coid = f"{prefix}-{base}-{ts_ms}"
    return coid[:36]


def place_binance_short_protection_split(symbol: str, stop_amount: float, tp_amount: float,
                                         hard_stop_price: float, tp_trigger_price: float,
                                         account_id: Optional[str] = None,
                                         stop_client_order_id: Optional[str] = None,
                                         tp_client_order_id: Optional[str] = None) -> dict:
    if not config.LIVE_MODE:
        return {"success": True, "stop_order_id": "SHADOW", "tp_order_id": "SHADOW", "error": ""}
    exchange = get_live_exchange(account_id)
    if not exchange:
        return {"success": False, "stop_order_id": "", "tp_order_id": "", "error": "交易所连接失败"}
    import time as _time
    stop_amount = _amount_to_precision(exchange, symbol, stop_amount)
    tp_amount = _amount_to_precision(exchange, symbol, tp_amount)
    if stop_amount <= 0 or tp_amount <= 0:
        return {"success": False, "stop_order_id": "", "tp_order_id": "", "error": "保护单数量裁剪后为 0"}
    stop_price = _round_price(exchange, symbol, hard_stop_price)
    tp_price = _round_price(exchange, symbol, tp_trigger_price)
    # 两阶段下单：先下 STOP_MARKET，确认后再下 TAKE_PROFIT_MARKET
    # 防止部分成功（STOP 成功 + TP 失败）时重试创建重复 STOP
    stop_params={'positionSide':'SHORT','stopPrice':stop_price,'workingType':'MARK_PRICE'}
    tp_params={'positionSide':'SHORT','stopPrice':tp_price,'workingType':'MARK_PRICE'}
    if stop_client_order_id:
        stop_params['newClientOrderId'] = stop_client_order_id
    else:
        stop_params['newClientOrderId'] = _ensure_client_order_id(None, 'st1', symbol, 'binance')
    if tp_client_order_id:
        tp_params['newClientOrderId'] = tp_client_order_id
    else:
        tp_params['newClientOrderId'] = _ensure_client_order_id(None, 'tp1', symbol, 'binance')
    stop_order_id = None
    max_retries = 3
    last_error = ''
    for attempt in range(max_retries):
        try:
            # 阶段1：下 STOP_MARKET
            if stop_order_id is None:
                stop_params['newClientOrderId'] = _ensure_client_order_id(None, 'st1', symbol, 'binance') + f'_a{attempt}'
                stop_order = exchange.create_order(symbol=symbol, type='STOP_MARKET', side='buy', amount=stop_amount, params=stop_params)
                stop_order_id = stop_order.get('id', '')
            # 阶段2：下 TAKE_PROFIT_MARKET
            tp_params['newClientOrderId'] = _ensure_client_order_id(None, 'tp1', symbol, 'binance') + f'_a{attempt}'
            tp_order = exchange.create_order(symbol=symbol, type='TAKE_PROFIT_MARKET', side='buy', amount=tp_amount, params=tp_params)
            tp_order_id = tp_order.get('id', '')
            logger.info(f"✅ Binance 分离保护单: {symbol} STOP({stop_amount:.4f}@{stop_price}) TP({tp_amount:.4f}@{tp_price})")
            return {"success": True, "stop_order_id": stop_order_id, "tp_order_id": tp_order_id, "error": ""}
        except Exception as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                delay = 1.5 ** attempt
                logger.warning(f"⏳ Binance 分离保护单重试({attempt+1}/{max_retries}): {symbol} {last_error}，{delay:.1f}s 后重试")
                # 如果 STOP 已成功但 TP 失败，清理已下的 STOP（防止重试累积）
                if stop_order_id is not None:
                    try:
                        exchange.cancel_order(stop_order_id, symbol)
                        logger.info(f"🧹 清理部分成功 STOP: {stop_order_id}")
                    except Exception:
                        pass
                    stop_order_id = None
                _time.sleep(delay)
    logger.error(f"❌ Binance 分离保护单失败（{max_retries}次重试后）({symbol}): {last_error}")
    return {"success": False, "stop_order_id": "", "tp_order_id": "", "error": last_error}


def place_binance_stage2_after_tp1(symbol: str, remain_amount: float, stop_price: float, tp2_price: float,
                                   account_id: Optional[str] = None,
                                   stop_client_order_id: Optional[str] = None,
                                   tp2_client_order_id: Optional[str] = None) -> dict:
    return place_binance_short_protection_split(symbol, remain_amount, remain_amount, stop_price, tp2_price,
                                                account_id=account_id, stop_client_order_id=stop_client_order_id,
                                                tp_client_order_id=tp2_client_order_id)


def get_binance_open_algo_orders(symbol: str, account_id: Optional[str] = None) -> list:
    """Return open Binance conditional(algo) orders for symbol."""
    exchange = get_live_exchange(account_id)
    if not exchange:
        return []
    try:
        exchange.load_markets()
        market = exchange.market(symbol)
        sid = market.get('id') or symbol.replace('/','').replace(':USDT','').replace('USDT:USDT','USDT')
        return exchange.fapiPrivateGetOpenAlgoOrders({'symbol': sid})
    except Exception as e:
        logger.error(f"获取 Binance algo 挂单失败 ({symbol}): {e}")
        return []


def cancel_binance_open_algo_orders(symbol: str, account_id: Optional[str] = None) -> dict:
    """Cancel all open Binance conditional(algo) orders for symbol."""
    if not config.LIVE_MODE:
        return {"success": True, "cancelled": 0, "error": ""}
    exchange = get_live_exchange(account_id)
    if not exchange:
        return {"success": False, "cancelled": 0, "error": "交易所连接失败"}
    try:
        exchange.load_markets()
        market = exchange.market(symbol)
        sid = market.get('id') or symbol.replace('/','').replace(':USDT','').replace('USDT:USDT','USDT')
        orders = exchange.fapiPrivateGetOpenAlgoOrders({'symbol': sid})
        cancelled = 0
        for o in orders:
            algo_id = o.get('algoId')
            if not algo_id:
                continue
            try:
                exchange.fapiPrivateDeleteAlgoOrder({'algoId': algo_id, 'symbol': sid})
                cancelled += 1
            except Exception as e:
                logger.warning(f"撤 Binance algo 单失败 {symbol} {algo_id}: {e}")
        return {"success": True, "cancelled": cancelled, "error": ""}
    except Exception as e:
        logger.error(f"撤 Binance algo 挂单失败 ({symbol}): {e}")
        return {"success": False, "cancelled": 0, "error": str(e)}
