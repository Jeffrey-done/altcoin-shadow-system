"""
共享测试夹具
提供：临时文件路径、合成K线数据、配置mock
"""

import os
import sys
import json
from unittest.mock import MagicMock

# 将项目根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 在导入项目模块之前 mock 掉 ccxt（测试环境没有安装）
sys.modules.setdefault('ccxt', MagicMock())

import pytest


@pytest.fixture
def tmp_trades_file(tmp_path):
    """临时交易文件路径"""
    return str(tmp_path / "trades.json")


@pytest.fixture
def tmp_risk_file(tmp_path):
    """临时风控状态文件路径"""
    return str(tmp_path / "risk_state.json")


@pytest.fixture
def sample_klines():
    """
    生成约50根合成1H K线，包含已知模式：
    前25根稳步上涨（制造高RSI），后25根稳步下跌（制造RSI回落信号）。
    """
    klines = []
    base_price = 0.5
    # 前25根：稳步上涨
    for i in range(25):
        open_p = base_price + i * 0.01
        close_p = open_p + 0.008
        high_p = close_p + 0.002
        low_p = open_p - 0.001
        klines.append({
            "time": f"2025-01-01T{i:02d}:00:00+00:00",
            "open": round(open_p, 6),
            "high": round(high_p, 6),
            "low": round(low_p, 6),
            "close": round(close_p, 6),
            "volume": 1000000 + i * 10000,
        })
    # 后25根：稳步下跌
    last_close = klines[-1]['close']
    for i in range(25):
        open_p = last_close - i * 0.01
        close_p = open_p - 0.008
        high_p = open_p + 0.001
        low_p = close_p - 0.002
        klines.append({
            "time": f"2025-01-02T{i:02d}:00:00+00:00",
            "open": round(open_p, 6),
            "high": round(high_p, 6),
            "low": round(low_p, 6),
            "close": round(close_p, 6),
            "volume": 1000000 + i * 10000,
        })
    return klines


@pytest.fixture
def mock_config(monkeypatch):
    """统一 mock 配置值，方便测试计算"""
    import config
    monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 1000)
    monkeypatch.setattr(config, 'LEVERAGE', 10)
    monkeypatch.setattr(config, 'DEFAULT_STAKE', 100)
    monkeypatch.setattr(config, 'HARD_STOP_LOSS_PCT', 3.0)
    monkeypatch.setattr(config, 'TP1_MULTIPLIER', 0.95)
    monkeypatch.setattr(config, 'TP2_MULTIPLIER', 0.90)
    monkeypatch.setattr(config, 'TP1_CLOSE_RATIO', 0.5)
    monkeypatch.setattr(config, 'TRAIL_STOP_ACTIVATE_PCT', 3)
    monkeypatch.setattr(config, 'TRAIL_STOP_DRAWDOWN_PCT', 0.10)
    monkeypatch.setattr(config, 'MAX_HOLD_DAYS', 1)
    monkeypatch.setattr(config, 'TIME_STOP_MIN_PROFIT_PCT', 3)
    monkeypatch.setattr(config, 'RSI_PERIOD', 14)
    monkeypatch.setattr(config, 'RISK_MAX_DAILY_LOSS', 30)
    monkeypatch.setattr(config, 'RISK_MAX_DAILY_TRADES', 2)
    monkeypatch.setattr(config, 'RISK_CONSECUTIVE_LOSS_PAUSE', 3)
    monkeypatch.setattr(config, 'RISK_PAUSE_HOURS', 24)
    monkeypatch.setattr(config, 'RISK_MAX_POSITION_PCT', 0.9)
    monkeypatch.setattr(config, 'SHORT_STRATEGY_POOL_PCT', 100)
    monkeypatch.setattr(config, 'FUNDING_ARB_POOL_PCT', 0)
    monkeypatch.setattr(config, 'LOW_RISK_POOL_PCT', 0)
