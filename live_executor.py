#!/usr/bin/env python3
"""
实盘执行模块 v2.0
支持 Binance + OKX 双交易所实盘执行。
当 LIVE_MODE=True 时，通过 Binance API 真实下单。
当 OKX_LIVE_MODE=True 时，通过 OKX API 真实下单。
当两者都为 False 时，只记录影子交易（纸上模拟）。

⚠️ 警告：开启实盘前务必：
  1. 纸上交易验证至少2周
  2. 确认胜率>50%、盈亏比>1.5
  3. 从最小仓位开始（DEFAULT_STAKE=20）
  4. 配置好对应交易所的 API 凭证
"""

import os
from typing import Optional

import ccxt

import config
from common import setup_logger, to_binance_symbol
from exchange_manager import get_binance, get_okx, to_okx_inst_id

logger = setup_logger("live_executor")


def get_live_exchange():
    """创建已认证的 Binance 合约交易所实例"""
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


def execute_open_short(symbol: str, stake: float, leverage: int = config.LEVERAGE,
                       client_order_id: Optional[str] = None) -> dict:
    """
    实盘开空单。

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

    exchange = get_live_exchange()
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

        # 滑点校验：成交价与 ticker 偏差 > 0.5% 告警（但不回滚）
        if ref_price > 0:
            slippage_pct = abs(fill_price - ref_price) / ref_price * 100
            if slippage_pct > 0.5:
                logger.warning(
                    f"⚠️ 滑点异常 {symbol}: ticker={ref_price:.6f} 成交={fill_price:.6f} "
                    f"({slippage_pct:.2f}%)"
                )

        logger.info(
            f"✅ 实盘开空: {symbol} | 成交={fill_price:.6f} | "
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
        logger.error(f"❌ 实盘开空失败 ({symbol}): {e}")
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": str(e)}


def execute_open_long(symbol: str, stake: float, leverage: int = config.LEVERAGE,
                      client_order_id: Optional[str] = None) -> dict:
    """
    实盘开多单（当前做空系统未使用，保留给未来扩展）。
    """
    if not config.LIVE_MODE:
        return {"success": True, "order_id": "SHADOW", "price": 0, "amount": 0, "error": ""}

    exchange = get_live_exchange()
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

        logger.info(
            f"✅ 实盘开多: {symbol} | 成交={fill_price:.6f} | "
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
        logger.error(f"❌ 实盘开多失败 ({symbol}): {e}")
        return {"success": False, "order_id": "", "price": 0, "amount": 0, "error": str(e)}


def execute_close_position(symbol: str, direction: str, amount: float) -> dict:
    """
    实盘平仓。

    参数:
      symbol: ccxt 格式
      direction: 'SHORT' 或 'LONG'
      amount: 平仓数量
    """
    if not config.LIVE_MODE:
        return {"success": True, "order_id": "SHADOW", "price": 0, "error": ""}

    exchange = get_live_exchange()
    if not exchange:
        return {"success": False, "order_id": "", "price": 0, "error": "交易所连接失败"}

    try:
        if direction == 'SHORT':
            # 平空 = 买入
            side = 'buy'
            position_side = 'SHORT'
        else:
            # 平多 = 卖出
            side = 'sell'
            position_side = 'LONG'

        order = exchange.create_order(
            symbol=symbol,
            type='market',
            side=side,
            amount=amount,
            params={'positionSide': position_side},
        )

        logger.info(f"✅ 实盘平仓: {symbol} {direction} | 数量={amount:.4f} | 订单={order['id']}")

        return {
            "success": True,
            "order_id": order.get('id', ''),
            "price": float(order.get('average', 0)),
            "error": "",
        }

    except Exception as e:
        logger.error(f"❌ 实盘平仓失败 ({symbol}): {e}")
        return {"success": False, "order_id": "", "price": 0, "error": str(e)}


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

def get_okx_live_exchange():
    """创建已认证的 OKX 合约交易所实例"""
    return get_okx(authenticated=True)


def execute_okx_open_short(symbol: str, stake: float, leverage: int = config.OKX_DEFAULT_LEVERAGE) -> dict:
    """
    OKX 实盘开空单。
    """
    if not config.OKX_LIVE_MODE:
        return {"success": True, "order_id": "SHADOW_OKX", "price": 0, "error": ""}

    exchange = get_okx_live_exchange()
    if not exchange:
        return {"success": False, "order_id": "", "price": 0, "error": "OKX 交易所连接失败"}

    try:
        # OKX 设置杠杆
        inst_id = to_okx_inst_id(symbol)
        exchange.set_leverage(leverage, symbol, params={'mgnMode': 'cross'})

        # 计算下单数量
        ticker = exchange.fetch_ticker(symbol)
        price = ticker['last']
        notional = stake * leverage
        amount = notional / price

        # 市价做空
        order = exchange.create_order(
            symbol=symbol,
            type='market',
            side='sell',
            amount=amount,
            params={
                'tdMode': 'cross',
                'posSide': 'short',
            },
        )

        logger.info(f"✅ OKX 开空: {symbol} | 数量={amount:.4f} | 杠杆={leverage}x | 订单={order['id']}")

        return {
            "success": True,
            "order_id": order.get('id', ''),
            "price": float(order.get('average', price)),
            "error": "",
        }

    except Exception as e:
        logger.error(f"❌ OKX 开空失败 ({symbol}): {e}")
        return {"success": False, "order_id": "", "price": 0, "error": str(e)}


def execute_okx_open_long(symbol: str, stake: float, leverage: int = config.OKX_DEFAULT_LEVERAGE) -> dict:
    """
    OKX 实盘开多单。
    """
    if not config.OKX_LIVE_MODE:
        return {"success": True, "order_id": "SHADOW_OKX", "price": 0, "error": ""}

    exchange = get_okx_live_exchange()
    if not exchange:
        return {"success": False, "order_id": "", "price": 0, "error": "OKX 交易所连接失败"}

    try:
        exchange.set_leverage(leverage, symbol, params={'mgnMode': 'cross'})

        ticker = exchange.fetch_ticker(symbol)
        price = ticker['last']
        notional = stake * leverage
        amount = notional / price

        order = exchange.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=amount,
            params={
                'tdMode': 'cross',
                'posSide': 'long',
            },
        )

        logger.info(f"✅ OKX 开多: {symbol} | 数量={amount:.4f} | 杠杆={leverage}x | 订单={order['id']}")

        return {
            "success": True,
            "order_id": order.get('id', ''),
            "price": float(order.get('average', price)),
            "error": "",
        }

    except Exception as e:
        logger.error(f"❌ OKX 开多失败 ({symbol}): {e}")
        return {"success": False, "order_id": "", "price": 0, "error": str(e)}


def execute_okx_close_position(symbol: str, direction: str, amount: float) -> dict:
    """
    OKX 实盘平仓。
    """
    if not config.OKX_LIVE_MODE:
        return {"success": True, "order_id": "SHADOW_OKX", "price": 0, "error": ""}

    exchange = get_okx_live_exchange()
    if not exchange:
        return {"success": False, "order_id": "", "price": 0, "error": "OKX 交易所连接失败"}

    try:
        if direction == 'SHORT':
            side = 'buy'
            pos_side = 'short'
        else:
            side = 'sell'
            pos_side = 'long'

        order = exchange.create_order(
            symbol=symbol,
            type='market',
            side=side,
            amount=amount,
            params={
                'tdMode': 'cross',
                'posSide': pos_side,
            },
        )

        logger.info(f"✅ OKX 平仓: {symbol} {direction} | 数量={amount:.4f} | 订单={order['id']}")

        return {
            "success": True,
            "order_id": order.get('id', ''),
            "price": float(order.get('average', 0)),
            "error": "",
        }

    except Exception as e:
        logger.error(f"❌ OKX 平仓失败 ({symbol}): {e}")
        return {"success": False, "order_id": "", "price": 0, "error": str(e)}


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

def execute_open(symbol: str, direction: str, stake: float,
                 exchange_name: str = 'binance', leverage: int = None) -> dict:
    """
    统一开仓接口，根据交易所名称路由到对应执行函数。

    参数:
      exchange_name: 'binance' | 'okx'
      direction: 'SHORT' | 'LONG'
    """
    if exchange_name == 'okx':
        lev = leverage or config.OKX_DEFAULT_LEVERAGE
        if direction == 'SHORT':
            return execute_okx_open_short(symbol, stake, lev)
        else:
            return execute_okx_open_long(symbol, stake, lev)
    else:
        lev = leverage or config.LEVERAGE
        if direction == 'SHORT':
            return execute_open_short(symbol, stake, lev)
        else:
            return execute_open_long(symbol, stake, lev)


def execute_close(symbol: str, direction: str, amount: float,
                  exchange_name: str = 'binance') -> dict:
    """
    统一平仓接口，根据交易所名称路由。
    """
    if exchange_name == 'okx':
        return execute_okx_close_position(symbol, direction, amount)
    else:
        return execute_close_position(symbol, direction, amount)
