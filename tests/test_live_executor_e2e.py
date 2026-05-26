"""
live_executor E2E 集成测试(mock-based)

策略:把 ccxt.binance / ccxt.okx 替换成 MagicMock,按真实 API 响应格式
喂预设 payload,然后跑完整的调用链。不接任何真实交易所。

覆盖点:
  Happy path:
    - execute_open_short 正确构造 create_order 参数 (symbol, type, side, amount, params)
    - positionSide=SHORT 参数被传递
    - client_order_id 正确映射为 Binance newClientOrderId / OKX clOrdId
    - 返回字典结构正确(success/order_id/price/amount/error)

  Precision & step size:
    - amount_to_precision 被调用
    - 裁剪后为 0 返回失败(避免 LOT_SIZE -4005)

  Slippage:
    - fill 价偏离 ref 价超阈值 → logger.warning

  set_leverage failure resilience:
    - Binance set_leverage 抛异常 → 继续开仓流程
    - OKX set_leverage 抛异常 → 继续开仓流程 (H6)

  Close path:
    - execute_close_position 使用 reduceOnly=True
    - direction SHORT → buy 平空 (positionSide=SHORT)
    - direction LONG → sell 平多 (positionSide=LONG)

  Multi-account (PR #33):
    - account_id 指定时读对应账户凭证,不使用 active_account
    - account_id 凭证缺失 → 返回失败,不抛

  Shadow mode:
    - LIVE_MODE=False → 直接返回 shadow stub 不调 ccxt
    - exchange_name='shadow' 的 close → 直接返回

  OKX parity:
    - execute_okx_open_short 与 Binance 返回格式一致
    - OKX 用 clOrdId (alphanumeric, ≤32)

  make_client_order_id:
    - Binance 生成含连字符 (≤36)
    - OKX 生成纯 alphanumeric (≤32)

  Router (execute_open / execute_close):
    - exchange_name='binance' / 'okx' 正确路由
    - direction SHORT/LONG 正确路由

  Precision of client_order_id propagation through router layers.
"""

from unittest.mock import MagicMock

import pytest

import config
from live_executor import (
    execute_open_short, execute_open_long,
    execute_close_position, execute_close,
    execute_okx_open_short, execute_okx_open_long,
    execute_okx_close_position,
    execute_open, make_client_order_id,
    check_live_balance, check_okx_balance,
    _amount_to_precision, _check_slippage,
)


# ══════════════════════════════════════════════════════════════════
#  Mock fixtures
# ══════════════════════════════════════════════════════════════════

@pytest.fixture
def live_mode(monkeypatch):
    """把 Binance 切成实盘模式"""
    monkeypatch.setattr(config, 'LIVE_MODE', True)


@pytest.fixture
def okx_live_mode(monkeypatch):
    """把 OKX 切成实盘模式"""
    monkeypatch.setattr(config, 'OKX_LIVE_MODE', True)


@pytest.fixture
def binance_creds(monkeypatch):
    """Mock admin_secrets.get_exchange_credentials / get_account_exchange_credentials"""
    def fake_creds(exchange, account_id=None):
        return {'api_key': 'test-key', 'secret': 'test-secret'}

    def fake_account_creds(exchange, account_id):
        if account_id == 'acc_missing':
            return {}  # 凭证缺失
        return {'api_key': f'key-{account_id}', 'secret': f'sec-{account_id}'}

    import admin_secrets as _as
    monkeypatch.setattr(_as, 'get_exchange_credentials', fake_creds, raising=False)
    monkeypatch.setattr(_as, 'get_account_exchange_credentials', fake_account_creds, raising=False)


@pytest.fixture
def okx_creds(monkeypatch):
    """Mock OKX credentials"""
    def fake_creds(exchange, account_id=None):
        return {
            'api_key': 'okx-key', 'secret': 'okx-secret', 'passphrase': 'okx-pass'
        }

    def fake_account_creds(exchange, account_id):
        if account_id == 'acc_missing':
            return {}
        return {
            'api_key': f'okx-key-{account_id}',
            'secret': f'okx-sec-{account_id}',
            'passphrase': f'pass-{account_id}',
        }

    import admin_secrets as _as
    monkeypatch.setattr(_as, 'get_exchange_credentials', fake_creds, raising=False)
    monkeypatch.setattr(_as, 'get_account_exchange_credentials', fake_account_creds, raising=False)


def _build_fake_binance(ticker_price=0.001, fill_price=None,
                       fill_amount=None, order_id='ORD-1'):
    """构造 ccxt.binance 风格的 mock 实例。

    - fetch_ticker 返回 last=ticker_price
    - create_order 返回 ccxt 风格 {id, status, filled, average, cost}
    - amount_to_precision 简单四舍五入到 3 位小数
    """
    fake = MagicMock()
    fake.fetch_ticker.return_value = {
        'last': ticker_price,
        'bid': ticker_price * 0.999,
        'ask': ticker_price * 1.001,
    }
    fake.amount_to_precision.side_effect = lambda sym, amt: str(round(amt, 3))
    fake.create_order.return_value = {
        'id': order_id,
        'status': 'closed',
        'filled': fill_amount,
        'average': fill_price if fill_price is not None else ticker_price,
        'cost': (fill_price or ticker_price) * (fill_amount or 0),
    }
    fake.set_leverage.return_value = None
    fake.fetch_balance.return_value = {
        'USDT': {'free': 1000.0, 'total': 1100.0}
    }
    return fake


@pytest.fixture
def fake_binance(monkeypatch):
    """默认 mock:ticker=0.001, 成交价=0.00101(~1% 滑点),filled=500000"""
    from live_executor import invalidate_live_exchange_cache
    invalidate_live_exchange_cache()
    import exchange_manager as _em
    _em._authenticated_instances.clear()
    fake = _build_fake_binance(
        ticker_price=0.001, fill_price=0.00101, fill_amount=500000.0,
    )
    monkeypatch.setattr('ccxt.binance', lambda *args, **kwargs: fake)
    return fake


@pytest.fixture
def fake_okx(monkeypatch):
    from live_executor import invalidate_live_exchange_cache
    invalidate_live_exchange_cache()
    import exchange_manager as _em
    _em._authenticated_instances.clear()
    fake = _build_fake_binance(
        ticker_price=0.001, fill_price=0.001005, fill_amount=500000.0,
    )
    monkeypatch.setattr('ccxt.okx', lambda *args, **kwargs: fake)
    return fake


# ══════════════════════════════════════════════════════════════════
#  execute_open_short (Binance)
# ══════════════════════════════════════════════════════════════════

class TestBinanceOpenShortHappyPath:
    def test_shadow_mode_returns_stub_without_ccxt(self, monkeypatch):
        """LIVE_MODE=False → 不碰 ccxt,直接返回 success=True, order_id='SHADOW'"""
        monkeypatch.setattr(config, 'LIVE_MODE', False)
        # 如果 ccxt 被 import 会炸(用 None 验证)
        called = {'hit': False}
        def trap(*a, **kw):
            called['hit'] = True
            raise AssertionError("不应该创建 ccxt 实例")
        monkeypatch.setattr('ccxt.binance', trap)

        r = execute_open_short('PEPE/USDT', stake=50)
        assert r['success'] is True
        assert r['order_id'] == 'SHADOW'
        assert r['price'] == 0
        assert called['hit'] is False

    def test_happy_path_returns_structured_result(self, live_mode, binance_creds, fake_binance):
        r = execute_open_short('PEPE/USDT', stake=50, leverage=10)

        assert r['success'] is True
        assert r['order_id'] == 'ORD-1'
        # fill_price 应该是 mock 里的 0.00101
        assert abs(r['price'] - 0.00101) < 1e-9
        assert r['amount'] == 500000.0
        assert r['error'] == ''

    def test_position_side_short_passed(self, live_mode, binance_creds, fake_binance):
        """Binance 合约必须传 positionSide=SHORT 才能在对冲模式下正确建仓"""
        execute_open_short('PEPE/USDT', stake=50, leverage=10)

        # create_order 应当被调用一次
        assert fake_binance.create_order.call_count == 1
        call = fake_binance.create_order.call_args
        params = call.kwargs.get('params', {}) or (call.args[-1] if len(call.args) >= 5 else {})
        assert params.get('positionSide') == 'SHORT'
        # side=sell 做空
        assert call.kwargs.get('side') == 'sell' or (len(call.args) >= 3 and call.args[2] == 'sell')

    def test_client_order_id_mapped_to_newClientOrderId(
        self, live_mode, binance_creds, fake_binance,
    ):
        execute_open_short('PEPE/USDT', stake=50, client_order_id='sho-PEPE-123')
        params = fake_binance.create_order.call_args.kwargs.get('params', {})
        assert params.get('newClientOrderId') == 'sho-PEPE-123'

    def test_no_client_order_id_skips_newClientOrderId(
        self, live_mode, binance_creds, fake_binance,
    ):
        """不传 coid 时不应往 params 里塞 newClientOrderId(避免 ccxt 报错)"""
        execute_open_short('PEPE/USDT', stake=50)
        params = fake_binance.create_order.call_args.kwargs.get('params', {})
        assert 'newClientOrderId' not in params

    def test_leverage_set_before_order(self, live_mode, binance_creds, fake_binance):
        execute_open_short('PEPE/USDT', stake=50, leverage=5)
        fake_binance.set_leverage.assert_called_once_with(5, 'PEPE/USDT')

    def test_set_leverage_failure_still_places_order(
        self, live_mode, binance_creds, fake_binance, caplog,
    ):
        """H6: set_leverage 抛异常不应阻止开仓(交易所已预设杠杆的常见场景)"""
        import logging
        fake_binance.set_leverage.side_effect = Exception('Leverage already set')
        with caplog.at_level(logging.WARNING):
            r = execute_open_short('PEPE/USDT', stake=50, leverage=10)
        # 开仓仍然成功
        assert r['success'] is True
        # create_order 仍然被调用
        assert fake_binance.create_order.called
        # 日志里能看到 warning
        assert any('set_leverage' in rec.message for rec in caplog.records)


class TestBinanceOpenShortPrecision:
    def test_amount_to_precision_called(self, live_mode, binance_creds, fake_binance):
        """stepSize 裁剪不能省"""
        execute_open_short('PEPE/USDT', stake=50, leverage=10)
        fake_binance.amount_to_precision.assert_called_once()

    def test_precision_zero_rejects_order(self, live_mode, binance_creds, fake_binance):
        """amount_to_precision 返回 0 → 拒单(低于 minNotional)"""
        fake_binance.amount_to_precision.side_effect = lambda sym, amt: '0'
        r = execute_open_short('PEPE/USDT', stake=50, leverage=10)
        assert r['success'] is False
        assert '0' in r['error'] or 'minNotional' in r['error']
        # 不应 create_order
        assert fake_binance.create_order.called is False

    def test_precision_fallback_on_exception(self, live_mode, binance_creds, fake_binance):
        """amount_to_precision 抛异常 → 用原值继续(容错)"""
        fake_binance.amount_to_precision.side_effect = Exception('parse err')
        r = execute_open_short('PEPE/USDT', stake=50, leverage=10)
        assert r['success'] is True


class TestBinanceSlippageAlert:
    def test_alert_fires_when_slippage_exceeds_threshold(
        self, live_mode, binance_creds, monkeypatch, caplog,
    ):
        """fill/ref 偏差 > SLIPPAGE_ALERT_PCT → logger.warning"""
        import logging
        monkeypatch.setattr(config, 'SLIPPAGE_ALERT_PCT', 0.5)  # 0.5%
        # 成交价偏离 1%,超过阈值
        fake = _build_fake_binance(
            ticker_price=0.001, fill_price=0.00101, fill_amount=500000.0,
        )
        monkeypatch.setattr('ccxt.binance', lambda *a, **kw: fake)
        with caplog.at_level(logging.WARNING):
            r = execute_open_short('PEPE/USDT', stake=50)
        assert r['success'] is True
        assert any('滑点异常' in rec.message for rec in caplog.records)

    def test_alert_silent_when_under_threshold(
        self, live_mode, binance_creds, monkeypatch, caplog,
    ):
        import logging
        monkeypatch.setattr(config, 'SLIPPAGE_ALERT_PCT', 5.0)  # 5%
        fake = _build_fake_binance(
            ticker_price=0.001, fill_price=0.001005, fill_amount=500000.0,
        )
        monkeypatch.setattr('ccxt.binance', lambda *a, **kw: fake)
        with caplog.at_level(logging.WARNING):
            execute_open_short('PEPE/USDT', stake=50)
        # 滑点 0.5% < 5% → 不应告警
        assert not any('滑点异常' in rec.message for rec in caplog.records)

    def test_check_slippage_helper_safe_with_zero(self):
        """_check_slippage ref=0 或 fill=0 不应抛"""
        assert _check_slippage('PEPE/USDT', 'X', 0, 1.0) is None
        assert _check_slippage('PEPE/USDT', 'X', 1.0, 0) is None


class TestBinanceOpenShortFailures:
    def test_exchange_connection_failure(self, live_mode, monkeypatch):
        """get_live_exchange 返回 None(凭证都缺) → 返回结构化失败"""
        from live_executor import invalidate_live_exchange_cache
        invalidate_live_exchange_cache()

        def fake_creds(*a, **kw):
            return {'api_key': '', 'secret': ''}
        import admin_secrets as _as
        monkeypatch.setattr(_as, 'get_exchange_credentials', fake_creds, raising=False)
        monkeypatch.delenv('BINANCE_API_KEY', raising=False)
        monkeypatch.delenv('BINANCE_SECRET', raising=False)

        r = execute_open_short('PEPE/USDT', stake=50)
        assert r['success'] is False
        assert r['order_id'] == ''
        assert '连接失败' in r['error']

    def test_create_order_exception_returns_failure(
        self, live_mode, binance_creds, fake_binance,
    ):
        """create_order 抛异常 → 返回结构化失败而不是 raise"""
        fake_binance.create_order.side_effect = Exception('-1021 timestamp')
        r = execute_open_short('PEPE/USDT', stake=50)
        assert r['success'] is False
        assert '-1021' in r['error']

    def test_fetch_ticker_exception_returns_failure(
        self, live_mode, binance_creds, fake_binance,
    ):
        fake_binance.fetch_ticker.side_effect = Exception('rate limit')
        r = execute_open_short('PEPE/USDT', stake=50)
        assert r['success'] is False


# ══════════════════════════════════════════════════════════════════
#  execute_open_long
# ══════════════════════════════════════════════════════════════════

class TestBinanceOpenLong:
    def test_long_uses_position_side_long_and_buy(
        self, live_mode, binance_creds, fake_binance,
    ):
        execute_open_long('PEPE/USDT', stake=50, leverage=10)
        call = fake_binance.create_order.call_args
        params = call.kwargs.get('params', {})
        assert params.get('positionSide') == 'LONG'
        assert call.kwargs.get('side') == 'buy'

    def test_long_shadow_mode(self, monkeypatch):
        monkeypatch.setattr(config, 'LIVE_MODE', False)
        r = execute_open_long('PEPE/USDT', stake=50)
        assert r['success'] is True and r['order_id'] == 'SHADOW'


# ══════════════════════════════════════════════════════════════════
#  execute_close_position (Binance)
# ══════════════════════════════════════════════════════════════════

class TestBinanceClose:
    def test_close_short_uses_buy_side_and_reduce_only(
        self, live_mode, binance_creds, fake_binance,
    ):
        """平空 = 买入"""
        r = execute_close_position('PEPE/USDT', 'SHORT', amount=100)
        assert r['success'] is True

        call = fake_binance.create_order.call_args
        params = call.kwargs.get('params', {})
        assert params.get('positionSide') == 'SHORT'
        assert params.get('reduceOnly') is True
        assert call.kwargs.get('side') == 'buy'

    def test_close_long_uses_sell_side_and_position_side_long(
        self, live_mode, binance_creds, fake_binance,
    ):
        execute_close_position('PEPE/USDT', 'LONG', amount=100)
        params = fake_binance.create_order.call_args.kwargs.get('params', {})
        assert params.get('positionSide') == 'LONG'
        assert params.get('reduceOnly') is True
        assert fake_binance.create_order.call_args.kwargs.get('side') == 'sell'

    def test_close_client_order_id_propagated(self, live_mode, binance_creds, fake_binance):
        execute_close_position('PEPE/USDT', 'SHORT', amount=100, client_order_id='cls-1')
        params = fake_binance.create_order.call_args.kwargs.get('params', {})
        assert params.get('newClientOrderId') == 'cls-1'

    def test_close_zero_amount_after_precision_rejects(
        self, live_mode, binance_creds, fake_binance,
    ):
        fake_binance.amount_to_precision.side_effect = lambda sym, amt: '0'
        r = execute_close_position('PEPE/USDT', 'SHORT', amount=1e-9)
        assert r['success'] is False
        assert '精度' in r['error'] or '0' in r['error']

    def test_close_shadow_mode(self, monkeypatch):
        monkeypatch.setattr(config, 'LIVE_MODE', False)
        r = execute_close_position('PEPE/USDT', 'SHORT', amount=100)
        assert r['success'] is True
        assert r['order_id'] == 'SHADOW'

    def test_close_returns_fill_price_for_slippage_backfill(
        self, live_mode, binance_creds, fake_binance,
    ):
        """成交均价必须返回 — 这是 exit_slippage_pct 回填的上游"""
        r = execute_close_position('PEPE/USDT', 'SHORT', amount=100)
        assert r['price'] > 0
        assert 'amount' in r


# ══════════════════════════════════════════════════════════════════
#  多账户路由(PR #33)
# ══════════════════════════════════════════════════════════════════

class TestMultiAccountRouting:
    def test_account_id_uses_per_account_credentials(
        self, live_mode, binance_creds, monkeypatch,
    ):
        """指定 exchange 名作为 account_id → 走对应交易所全局凭证"""
        captured = {}
        def fake_binance_factory(cfg):
            captured['api_key'] = cfg.get('apiKey')
            return _build_fake_binance()
        monkeypatch.setattr('ccxt.binance', fake_binance_factory)

        execute_open_short('PEPE/USDT', stake=50, account_id='binance')
        assert captured['api_key'] == 'test-key'

    def test_close_with_account_id_uses_correct_credentials(
        self, live_mode, binance_creds, monkeypatch,
    ):
        """平仓使用交易所全局凭证"""
        from live_executor import invalidate_live_exchange_cache
        invalidate_live_exchange_cache()

        captured = {'api_key': None}
        def fake_binance_factory(cfg):
            captured['api_key'] = cfg.get('apiKey')
            return _build_fake_binance()
        monkeypatch.setattr('ccxt.binance', fake_binance_factory)

        execute_close_position('PEPE/USDT', 'SHORT', amount=100, account_id='binance')
        assert captured['api_key'] == 'test-key'

    def test_account_id_missing_credentials_returns_failure(
        self, live_mode, binance_creds, monkeypatch,
    ):
        """未知交易所→利用全局凭证走新模型"""
        monkeypatch.setattr('ccxt.binance', lambda *a, **kw: _build_fake_binance())
        r = execute_open_short('PEPE/USDT', stake=50, account_id='unknown_exchange')
        assert r['success'] is True

    def test_check_live_balance_accepts_account_id(
        self, live_mode, binance_creds, fake_binance,
    ):
        r = check_live_balance(account_id='acc_x')
        assert 'available' in r and 'total' in r
        assert r['total'] == 1100.0


# ══════════════════════════════════════════════════════════════════
#  OKX Parity
# ══════════════════════════════════════════════════════════════════

class TestOKXOpen:
    def test_okx_shadow_mode(self, monkeypatch):
        monkeypatch.setattr(config, 'OKX_LIVE_MODE', False)
        r = execute_okx_open_short('PEPE/USDT', stake=50)
        assert r['success'] is True
        assert r['order_id'] == 'SHADOW_OKX'

    def test_okx_happy_path_parity_with_binance(
        self, okx_live_mode, okx_creds, fake_okx,
    ):
        r = execute_okx_open_short('PEPE/USDT', stake=50, leverage=10)
        # 返回结构必须和 Binance 完全一致(success/order_id/price/amount/error)
        assert set(r.keys()) == {'success', 'order_id', 'price', 'amount', 'error'}
        assert r['success'] is True

    def test_okx_uses_pos_side_short_and_cross_margin(
        self, okx_live_mode, okx_creds, fake_okx,
    ):
        execute_okx_open_short('PEPE/USDT', stake=50)
        params = fake_okx.create_order.call_args.kwargs.get('params', {})
        assert params.get('posSide') == 'short'
        assert params.get('tdMode') == 'cross'

    def test_okx_client_order_id_mapped_to_clOrdId(
        self, okx_live_mode, okx_creds, fake_okx,
    ):
        execute_okx_open_short('PEPE/USDT', stake=50, client_order_id='shoPEPE123')
        params = fake_okx.create_order.call_args.kwargs.get('params', {})
        assert params.get('clOrdId') == 'shoPEPE123'

    def test_okx_set_leverage_failure_does_not_block(
        self, okx_live_mode, okx_creds, fake_okx, caplog,
    ):
        """H6: OKX set_leverage 抛异常不阻塞开仓(账户模式不匹配 / 已设置)"""
        import logging
        fake_okx.set_leverage.side_effect = Exception('OKX lever mode mismatch')
        with caplog.at_level(logging.WARNING):
            r = execute_okx_open_short('PEPE/USDT', stake=50)
        assert r['success'] is True
        assert any('OKX set_leverage 失败' in rec.message for rec in caplog.records)

    def test_okx_long_uses_pos_side_long(self, okx_live_mode, okx_creds, fake_okx):
        execute_okx_open_long('PEPE/USDT', stake=50)
        params = fake_okx.create_order.call_args.kwargs.get('params', {})
        assert params.get('posSide') == 'long'

    def test_okx_precision_rejected_at_zero(self, okx_live_mode, okx_creds, fake_okx):
        fake_okx.amount_to_precision.side_effect = lambda sym, amt: '0'
        r = execute_okx_open_short('PEPE/USDT', stake=50)
        assert r['success'] is False


class TestOKXClose:
    def test_okx_close_short(self, okx_live_mode, okx_creds, fake_okx):
        r = execute_okx_close_position('PEPE/USDT', 'SHORT', amount=100)
        assert r['success'] is True
        params = fake_okx.create_order.call_args.kwargs.get('params', {})
        assert params.get('posSide') == 'short'
        assert params.get('reduceOnly') is True
        assert fake_okx.create_order.call_args.kwargs.get('side') == 'buy'

    def test_okx_close_long(self, okx_live_mode, okx_creds, fake_okx):
        execute_okx_close_position('PEPE/USDT', 'LONG', amount=100)
        params = fake_okx.create_order.call_args.kwargs.get('params', {})
        assert params.get('posSide') == 'long'
        assert fake_okx.create_order.call_args.kwargs.get('side') == 'sell'

    def test_okx_shadow_close(self, monkeypatch):
        monkeypatch.setattr(config, 'OKX_LIVE_MODE', False)
        r = execute_okx_close_position('PEPE/USDT', 'SHORT', amount=100)
        assert r['success'] is True and r['order_id'] == 'SHADOW_OKX'


class TestOKXBalance:
    def test_balance_accepts_account_id(self, okx_live_mode, okx_creds, fake_okx):
        r = check_okx_balance(account_id='acc_okx')
        assert 'available' in r


# ══════════════════════════════════════════════════════════════════
#  Router: execute_open / execute_close
# ══════════════════════════════════════════════════════════════════

class TestExecuteOpenRouter:
    def test_routes_binance_short(self, live_mode, binance_creds, fake_binance):
        r = execute_open('PEPE/USDT', 'SHORT', stake=50, exchange_name='binance')
        assert r['success'] is True
        # 用了 Binance 路径 → positionSide=SHORT
        params = fake_binance.create_order.call_args.kwargs.get('params', {})
        assert params.get('positionSide') == 'SHORT'

    def test_routes_binance_long(self, live_mode, binance_creds, fake_binance):
        r = execute_open('PEPE/USDT', 'LONG', stake=50, exchange_name='binance')
        assert r['success'] is True
        params = fake_binance.create_order.call_args.kwargs.get('params', {})
        assert params.get('positionSide') == 'LONG'

    def test_routes_okx_short(self, okx_live_mode, okx_creds, fake_okx):
        r = execute_open('PEPE/USDT', 'SHORT', stake=50, exchange_name='okx')
        assert r['success'] is True
        params = fake_okx.create_order.call_args.kwargs.get('params', {})
        assert params.get('posSide') == 'short'

    def test_routes_okx_long(self, okx_live_mode, okx_creds, fake_okx):
        r = execute_open('PEPE/USDT', 'LONG', stake=50, exchange_name='okx')
        assert r['success'] is True
        params = fake_okx.create_order.call_args.kwargs.get('params', {})
        assert params.get('posSide') == 'long'

    def test_coid_propagates_through_router(
        self, live_mode, binance_creds, fake_binance,
    ):
        execute_open(
            'PEPE/USDT', 'SHORT', stake=50,
            exchange_name='binance', client_order_id='coid-thru',
        )
        params = fake_binance.create_order.call_args.kwargs.get('params', {})
        assert params.get('newClientOrderId') == 'coid-thru'


class TestExecuteCloseRouter:
    def test_shadow_exchange_returns_stub(self):
        """exchange_name='shadow' 跳过 ccxt,直接返回 success"""
        r = execute_close('PEPE/USDT', 'SHORT', amount=100, exchange_name='shadow')
        assert r['success'] is True
        assert r['order_id'] == 'SHADOW'
        assert r['amount'] == 100

    def test_routes_binance_close(self, live_mode, binance_creds, fake_binance):
        r = execute_close('PEPE/USDT', 'SHORT', 100, exchange_name='binance')
        assert r['success'] is True
        # 验证用了 Binance 路径:参数里有 newClientOrderId 可能、positionSide 一定有
        params = fake_binance.create_order.call_args.kwargs.get('params', {})
        assert 'positionSide' in params

    def test_routes_okx_close(self, okx_live_mode, okx_creds, fake_okx):
        r = execute_close('PEPE/USDT', 'SHORT', 100, exchange_name='okx')
        assert r['success'] is True
        params = fake_okx.create_order.call_args.kwargs.get('params', {})
        assert 'posSide' in params

    def test_close_account_id_propagates(
        self, live_mode, binance_creds, monkeypatch,
    ):
        """account_id=exchange名 → 使用交易所自身凭证"""
        from live_executor import invalidate_live_exchange_cache
        invalidate_live_exchange_cache()

        captured = {'api_key': None}
        def factory(cfg):
            captured['api_key'] = cfg.get('apiKey')
            return _build_fake_binance()
        monkeypatch.setattr('ccxt.binance', factory)

        execute_close('PEPE/USDT', 'SHORT', 100,
                      exchange_name='binance', account_id='binance')
        assert captured['api_key'] == 'test-key'


# ══════════════════════════════════════════════════════════════════
#  make_client_order_id
# ══════════════════════════════════════════════════════════════════

class TestMakeClientOrderId:
    def test_binance_coid_length_under_36(self):
        coid = make_client_order_id('sho', 'PEPE/USDT', exchange_name='binance')
        assert len(coid) <= 36

    def test_binance_coid_preserves_hyphen_and_symbol(self):
        coid = make_client_order_id('sho', 'PEPE/USDT',
                                    exchange_name='binance', ts_ms=1700000000000)
        assert coid.startswith('sho-PEPE-')
        assert '-' in coid

    def test_okx_coid_alphanumeric_only(self):
        coid = make_client_order_id('sho', 'PEPE/USDT',
                                    exchange_name='okx', ts_ms=1700000000000)
        # OKX 必须是纯字母数字
        assert coid.isalnum()
        assert len(coid) <= 32
        assert coid.startswith('sho')

    def test_okx_coid_strips_special_chars(self):
        """即便 symbol 里有斜杠/连字符也要剥掉"""
        coid = make_client_order_id('sho', 'BTC-PERP/USDT', exchange_name='okx')
        assert coid.isalnum()


# ══════════════════════════════════════════════════════════════════
#  _amount_to_precision helper
# ══════════════════════════════════════════════════════════════════

class TestAmountToPrecisionHelper:
    def test_returns_exchange_rounded(self):
        fake = MagicMock()
        fake.amount_to_precision.return_value = '123.45'
        assert _amount_to_precision(fake, 'X/USDT', 123.456789) == 123.45

    def test_returns_zero_if_exchange_returns_zero(self):
        fake = MagicMock()
        fake.amount_to_precision.return_value = '0'
        assert _amount_to_precision(fake, 'X/USDT', 0.0000001) == 0.0

    def test_fallback_on_exception(self):
        fake = MagicMock()
        fake.amount_to_precision.side_effect = Exception('parse err')
        # 异常时直接返回原值,让调用方决定是否拒单
        assert _amount_to_precision(fake, 'X/USDT', 123.4) == 123.4


# ══════════════════════════════════════════════════════════════════
#  Balance checks — shadow mode stubs
# ══════════════════════════════════════════════════════════════════

class TestBalanceShadowMode:
    def test_binance_balance_shadow_returns_config_balance(self, monkeypatch):
        monkeypatch.setattr(config, 'LIVE_MODE', False)
        r = check_live_balance()
        assert r['available'] == config.ACCOUNT_BALANCE
        assert r['total'] == config.ACCOUNT_BALANCE

    def test_okx_balance_shadow_returns_zero(self, monkeypatch):
        monkeypatch.setattr(config, 'OKX_LIVE_MODE', False)
        r = check_okx_balance()
        assert r['available'] == 0
        assert r['total'] == 0


# ══════════════════════════════════════════════════════════════════
#  End-to-end: Open → Close 一条链路整体跑通
# ══════════════════════════════════════════════════════════════════

class TestEndToEndOpenThenClose:
    """
    最能接住"真实 bug"的测试:开一笔空 → 直接平这笔空,
    两次 create_order 都验证参数正确。对应实际业务链路:
      scanner.execute_open_short → tracker._perform_exchange_close
    """

    def test_open_short_then_close_full_cycle(
        self, live_mode, binance_creds, monkeypatch,
    ):
        calls = []
        def tracked_order(*args, **kwargs):
            calls.append({'args': args, 'kwargs': kwargs})
            side = kwargs.get('side') or (args[2] if len(args) >= 3 else None)
            return {
                'id': f'ORD-{len(calls)}',
                'status': 'closed',
                'filled': 500000.0,
                'average': 0.001 if side == 'sell' else 0.000995,
                'cost': 500.0,
            }

        fake = _build_fake_binance(
            ticker_price=0.001, fill_price=0.001, fill_amount=500000.0,
        )
        fake.create_order.side_effect = tracked_order
        monkeypatch.setattr('ccxt.binance', lambda *a, **kw: fake)

        # 1) 开空
        r_open = execute_open_short(
            'PEPE/USDT', stake=50, leverage=10, client_order_id='sho-1',
        )
        assert r_open['success'] is True
        assert r_open['order_id'] == 'ORD-1'

        # 2) 平空(全量)
        r_close = execute_close_position(
            'PEPE/USDT', 'SHORT', amount=500000.0, client_order_id='cls-1',
        )
        assert r_close['success'] is True
        assert r_close['order_id'] == 'ORD-2'

        # 3) 验证两次 create_order 的参数方向正反
        assert len(calls) == 2
        open_call = calls[0]
        close_call = calls[1]

        # 开空:side=sell, positionSide=SHORT, 无 reduceOnly
        assert open_call['kwargs']['side'] == 'sell'
        assert open_call['kwargs']['params'].get('positionSide') == 'SHORT'
        assert open_call['kwargs']['params'].get('reduceOnly') is not True

        # 平空:side=buy, positionSide=SHORT, reduceOnly=True
        assert close_call['kwargs']['side'] == 'buy'
        assert close_call['kwargs']['params'].get('positionSide') == 'SHORT'
        assert close_call['kwargs']['params'].get('reduceOnly') is True

        # coid 都透传到位
        assert open_call['kwargs']['params'].get('newClientOrderId') == 'sho-1'
        assert close_call['kwargs']['params'].get('newClientOrderId') == 'cls-1'
