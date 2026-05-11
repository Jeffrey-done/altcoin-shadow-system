#!/usr/bin/env python3
"""
实盘执行模块 v1.0
当 LIVE_MODE=True 时，通过 Binance API 真实下单。
当 LIVE_MODE=False 时，只记录影子交易（纸上模拟）。

⚠️ 警告：开启实盘前务必：
  1. 纸上交易验证至少2周
  2. 确认胜率>50%、盈亏比>1.5
  3. 从最小仓位开始（DEFAULT_STAKE=20）
  4. 配置好 BINANCE_API_KEY / BINANCE_SECRET
"""

import os
import ccxt

import config
from common import setup_logger, to_binance_symbol

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


def execute_open_short(symbol: str, stake: float, leverage: int = config.LEVERAGE) -> dict:
    """
    实盘开空单。

    参数:
      symbol: ccxt 格式 (如 'PEPE/USDT')
      stake: 保证金 (USDT)
      leverage: 杠杆倍数

    返回:
      {"success": True/False, "order_id": str, "price": float, "error": str}
    """
    if not config.LIVE_MODE:
        return {"success": True, "order_id": "SHADOW", "price": 0, "error": ""}

    exchange = get_live_exchange()
    if not exchange:
        return {"success": False, "order_id": "", "price": 0, "error": "交易所连接失败"}

    try:
        # 设置杠杆
        binance_symbol = to_binance_symbol(symbol)
        exchange.fapiPrivate_post_leverage({
            'symbol': binance_symbol,
            'leverage': leverage,
        })

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
            params={'positionSide': 'SHORT'},
        )

        logger.info(f"✅ 实盘开空: {symbol} | 数量={amount:.4f} | 杠杆={leverage}x | 订单={order['id']}")

        return {
            "success": True,
            "order_id": order.get('id', ''),
            "price": float(order.get('average', price)),
            "error": "",
        }

    except Exception as e:
        logger.error(f"❌ 实盘开空失败 ({symbol}): {e}")
        return {"success": False, "order_id": "", "price": 0, "error": str(e)}


def execute_open_long(symbol: str, stake: float, leverage: int = config.LEVERAGE) -> dict:
    """
    实盘开多单。
    """
    if not config.LIVE_MODE:
        return {"success": True, "order_id": "SHADOW", "price": 0, "error": ""}

    exchange = get_live_exchange()
    if not exchange:
        return {"success": False, "order_id": "", "price": 0, "error": "交易所连接失败"}

    try:
        binance_symbol = to_binance_symbol(symbol)
        exchange.fapiPrivate_post_leverage({
            'symbol': binance_symbol,
            'leverage': leverage,
        })

        ticker = exchange.fetch_ticker(symbol)
        price = ticker['last']
        notional = stake * leverage
        amount = notional / price

        order = exchange.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=amount,
            params={'positionSide': 'LONG'},
        )

        logger.info(f"✅ 实盘开多: {symbol} | 数量={amount:.4f} | 杠杆={leverage}x | 订单={order['id']}")

        return {
            "success": True,
            "order_id": order.get('id', ''),
            "price": float(order.get('average', price)),
            "error": "",
        }

    except Exception as e:
        logger.error(f"❌ 实盘开多失败 ({symbol}): {e}")
        return {"success": False, "order_id": "", "price": 0, "error": str(e)}


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
    """查询合约账户余额"""
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
