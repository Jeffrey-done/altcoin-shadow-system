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
from typing import Optional

import ccxt

import config
from common import setup_logger
from exchange_manager import get_binance, get_okx, to_okx_inst_id

logger = setup_logger("live_executor")


# ══════════════════════════════════════════════════════════════════
#  Binance 实盘
# ══════════════════════════════════════════════════════════════════

def get_live_exchange(account_id: Optional[str] = None):
    """创建已认证的 Binance 合约交易所实例

    凭证优先级：
      - 指定 account_id 时：使用该账户的独立凭证（多账户并行模式）
      - 未指定时：admin_secrets.json 活跃账户 > .env 环境变量
    （admin panel 修改后立即生效，无需重启进程）
    """
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
        # admin_secrets 导入/读取失败时的 fallback
        logger.debug(f"admin_secrets 不可用，fallback 到环境变量: {e}")
        api_key = os.environ.get('BINANCE_API_KEY', '')
        secret = os.environ.get('BINANCE_SECRET', '')

    if not api_key or not secret:
        logger.error("BINANCE_API_KEY 或 BINANCE_SECRET 未设置！无法实盘交易")
        return None

    exchange = ccxt.binance({
        'apiKey': api_key,
        'secret': secret,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future',  # 使用合约账户
        },
    })

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
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": "交易所连接失败"}

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
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": str(e)}


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
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": "交易所连接失败"}

    try:
        try:
            exchange.set_leverage(leverage, symbol)
        except Exception as e:
            logger.warning(f"set_leverage 失败 ({symbol}): {e}")

        ticker = exchange.fetch_ticker(symbol)
        ref_price = ticker['last']
        notional = stake * leverage
        amount = notional / ref_price

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
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": str(e)}


def execute_close_position(symbol: str, direction: str, amount: float,
                           client_order_id: Optional[str] = None) -> dict:
    """
    Binance 实盘平仓。

    参数:
      symbol: ccxt 格式
      direction: 'SHORT' 或 'LONG'
      amount: 平仓数量
      client_order_id: 平仓幂等键（防止 evaluate 在同一秒被触发多次重复平仓）
    """
    if not config.LIVE_MODE:
        return {"success": True, "order_id": "SHADOW", "price": 0, "amount": amount, "error": ""}

    exchange = get_live_exchange()
    if not exchange:
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": "交易所连接失败"}

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
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": str(e)}


def check_live_balance() -> dict:
    """查询合约账户余额（Binance）"""
    if not config.LIVE_MODE:
        return {"available": config.ACCOUNT_BALANCE, "total": config.ACCOUNT_BALANCE}

    exchange = get_live_exchange()
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
            return ccxt.okx({
                'apiKey': api_key,
                'secret': secret,
                'password': passphrase,
                'enableRateLimit': True,
            })
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
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": "OKX 交易所连接失败"}

    try:
        exchange.set_leverage(leverage, symbol, params={'mgnMode': 'cross'})

        ticker = exchange.fetch_ticker(symbol)
        ref_price = ticker['last']
        notional = stake * leverage
        amount = notional / ref_price

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
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": str(e)}


def execute_okx_open_long(symbol: str, stake: float, leverage: int = config.OKX_DEFAULT_LEVERAGE,
                           client_order_id: Optional[str] = None,
                           account_id: Optional[str] = None) -> dict:
    """OKX 实盘开多单"""
    if not config.OKX_LIVE_MODE:
        return {"success": True, "order_id": "SHADOW_OKX", "price": 0, "amount": 0, "error": ""}

    exchange = get_okx_live_exchange(account_id)
    if not exchange:
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": "OKX 交易所连接失败"}

    try:
        exchange.set_leverage(leverage, symbol, params={'mgnMode': 'cross'})

        ticker = exchange.fetch_ticker(symbol)
        ref_price = ticker['last']
        notional = stake * leverage
        amount = notional / ref_price

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
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": str(e)}


def execute_okx_close_position(symbol: str, direction: str, amount: float,
                                client_order_id: Optional[str] = None) -> dict:
    """OKX 实盘平仓"""
    if not config.OKX_LIVE_MODE:
        return {"success": True, "order_id": "SHADOW_OKX", "price": 0, "amount": amount, "error": ""}

    exchange = get_okx_live_exchange()
    if not exchange:
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": "OKX 交易所连接失败"}

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
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": str(e)}


def check_okx_balance() -> dict:
    """查询 OKX 合约账户余额"""
    if not config.OKX_LIVE_MODE:
        return {"available": 0, "total": 0}

    exchange = get_okx_live_exchange()
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
      client_order_id: 幂等键；若为 None，上层应传入 symbol+timestamp 的拼接串
      account_id: 指定账户 ID（多账户并行模式）；None 使用活跃账户
    """
    if exchange_name == 'okx':
        lev = leverage or config.OKX_DEFAULT_LEVERAGE
        if direction == 'SHORT':
            return execute_okx_open_short(symbol, stake, lev, client_order_id=client_order_id, account_id=account_id)
        else:
            return execute_okx_open_long(symbol, stake, lev, client_order_id=client_order_id, account_id=account_id)
    else:
        lev = leverage or config.LEVERAGE
        if direction == 'SHORT':
            return execute_open_short(symbol, stake, lev, client_order_id=client_order_id, account_id=account_id)
        else:
            return execute_open_long(symbol, stake, lev, client_order_id=client_order_id, account_id=account_id)


def execute_close(symbol: str, direction: str, amount: float,
                  exchange_name: str = 'binance',
                  client_order_id: Optional[str] = None) -> dict:
    """
    统一平仓接口，根据交易所名称路由。
    影子交易（exchange_name='shadow'）直接返回成功，不发真实订单。
    """
    if exchange_name == 'shadow':
        return {"success": True, "order_id": "SHADOW", "price": 0, "amount": amount, "error": ""}
    if exchange_name == 'okx':
        return execute_okx_close_position(symbol, direction, amount, client_order_id=client_order_id)
    return execute_close_position(symbol, direction, amount, client_order_id=client_order_id)


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
