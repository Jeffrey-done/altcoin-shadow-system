"""
POSITION_MODE 比例模式回归测试

覆盖:
    - manual 模式: apply_position_scale 不动任何字段
    - proportional 模式 + 影子: 用 ACCOUNT_BALANCE 手填值
    - proportional 模式 + LIVE_MODE: 从 check_live_balance mock 拉余额
    - proportional 模式 + 双所 'both': 两所余额相加
    - proportional 模式 + 'auto' 路由: 取较大者
    - proportional 模式 + ACCOUNT_BALANCE=0: 边界保护
    - COMPOUND_MAX_STAKE 不缩放: 永远是 PRISTINE 默认值
    - apply_overrides 末尾自动调 apply_position_scale
    - 60s TTL 余额缓存

设计原则:
    - 所有测试都重置 _balance_cache 和 config 模块属性，避免相互污染
    - mock check_live_balance / check_okx_balance 避免真打交易所
    - PRISTINE_DEFAULTS 已在模块导入时快照，测试中只动 config 模块属性
"""

import os
import sys
import time

import pytest

# 让 tests/ 之外的项目根目录在 import path 上（与其他测试一致）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config
import runtime_config


# 比例模式下被自动缩放的 4 个字段（COMPOUND_MAX_STAKE 不在内）
SCALED = ('DEFAULT_STAKE', 'RISK_MAX_DAILY_LOSS', 'COMPOUND_STEP', 'COMPOUND_INCREASE')


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """每个测试前重置 config 模块属性 + 余额缓存，避免污染"""
    # 备份 config 当前值
    saved = {k: getattr(config, k, None) for k in (
        'POSITION_MODE', 'BASELINE_BALANCE', 'ACCOUNT_BALANCE',
        'LIVE_MODE', 'OKX_LIVE_MODE', 'PRIMARY_EXCHANGE',
        *SCALED, 'COMPOUND_MAX_STAKE', 'LEVERAGE',
    )}

    # 默认状态：manual / 100U / 影子 / 单所
    monkeypatch.setattr(config, 'POSITION_MODE', 'manual')
    monkeypatch.setattr(config, 'BASELINE_BALANCE', 100)
    monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 100)
    monkeypatch.setattr(config, 'LIVE_MODE', False)
    monkeypatch.setattr(config, 'OKX_LIVE_MODE', False)
    monkeypatch.setattr(config, 'PRIMARY_EXCHANGE', 'binance')

    # 把每个被缩放字段恢复到 PRISTINE 默认值
    for k in SCALED:
        v = runtime_config.get_pristine_default(k)
        if v is not None:
            monkeypatch.setattr(config, k, v)

    # 清空 60s 余额缓存
    runtime_config._balance_cache.update({
        'value': None,
        'source': None,
        'expires_at': 0.0,
    })
    yield
    # monkeypatch 自动还原


# ══════════════════════════════════════════════════════════════════
#  ALLOWED 白名单和 GLOBAL_FIELDS 校验
# ══════════════════════════════════════════════════════════════════

class TestRegistration:
    def test_position_mode_is_in_allowed(self):
        assert 'POSITION_MODE' in runtime_config.ALLOWED

    def test_position_mode_is_global_field(self):
        assert 'POSITION_MODE' in runtime_config.GLOBAL_FIELDS
        assert 'POSITION_MODE' not in runtime_config.ACCOUNT_FIELDS

    def test_position_mode_validator_rejects_unknown(self):
        ok, _err = runtime_config.validate_change('POSITION_MODE', 'crazy')
        assert not ok
        ok, _err = runtime_config.validate_change('POSITION_MODE', 'manual')
        assert ok
        ok, _err = runtime_config.validate_change('POSITION_MODE', 'proportional')
        assert ok


# ══════════════════════════════════════════════════════════════════
#  apply_position_scale: manual 模式
# ══════════════════════════════════════════════════════════════════

class TestManualMode:
    def test_manual_does_not_scale(self, monkeypatch):
        monkeypatch.setattr(config, 'POSITION_MODE', 'manual')
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 500)  # 即使余额变了也不缩放
        monkeypatch.setattr(config, 'DEFAULT_STAKE', 30)

        state = runtime_config.apply_position_scale()

        assert state['mode'] == 'manual'
        assert state['scale'] == 1.0
        assert state['scaled_fields'] == {}
        # config.DEFAULT_STAKE 不应被改动
        assert config.DEFAULT_STAKE == 30


# ══════════════════════════════════════════════════════════════════
#  apply_position_scale: proportional 模式 + 影子
# ══════════════════════════════════════════════════════════════════

class TestProportionalShadow:
    def test_500u_balance_scales_5x(self, monkeypatch):
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 500)

        state = runtime_config.apply_position_scale()

        assert state['mode'] == 'proportional'
        assert state['effective_balance'] == 500.0
        assert state['scale'] == 5.0
        assert state['balance_source'] == 'config'  # 影子模式

        # PRISTINE DEFAULT_STAKE=30 → 30 × 5 = 150
        pristine_stake = runtime_config.get_pristine_default('DEFAULT_STAKE')
        assert config.DEFAULT_STAKE == pristine_stake * 5

        # PRISTINE RISK_MAX_DAILY_LOSS=30 → 30 × 5 = 150
        pristine_loss = runtime_config.get_pristine_default('RISK_MAX_DAILY_LOSS')
        # 浮点字段是 round(... ,2)
        assert abs(config.RISK_MAX_DAILY_LOSS - pristine_loss * 5) < 0.01

    def test_50u_balance_scales_half(self, monkeypatch):
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 50)

        state = runtime_config.apply_position_scale()

        assert state['scale'] == 0.5
        pristine_stake = runtime_config.get_pristine_default('DEFAULT_STAKE')  # 30
        # int 字段：max(1, round(30 × 0.5)) = 15
        assert config.DEFAULT_STAKE == max(1, int(round(pristine_stake * 0.5)))

    def test_compound_max_stake_never_scales(self, monkeypatch):
        """COMPOUND_MAX_STAKE 用户要求绝对值上限，永远不缩放"""
        pristine_max = runtime_config.get_pristine_default('COMPOUND_MAX_STAKE')

        # 不管 scale 多大，COMPOUND_MAX_STAKE 都不动
        for balance in [50, 100, 500, 1000]:
            monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
            monkeypatch.setattr(config, 'ACCOUNT_BALANCE', balance)
            # 重置避免之前 round 的状态
            monkeypatch.setattr(config, 'COMPOUND_MAX_STAKE', pristine_max)

            runtime_config.apply_position_scale()

            assert config.COMPOUND_MAX_STAKE == pristine_max, (
                f"balance={balance}: COMPOUND_MAX_STAKE 被错误缩放成 "
                f"{config.COMPOUND_MAX_STAKE}（应保持 {pristine_max}）"
            )

    def test_strategy_pct_fields_not_scaled(self, monkeypatch):
        """杠杆/止盈止损百分比/RSI 阈值这些非金额字段不参与缩放"""
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 500)

        leverage_before = config.LEVERAGE
        runtime_config.apply_position_scale()
        assert config.LEVERAGE == leverage_before


# ══════════════════════════════════════════════════════════════════
#  apply_position_scale: proportional 模式 + LIVE_MODE
# ══════════════════════════════════════════════════════════════════

class TestProportionalLive:
    def test_binance_live_pulls_real_balance(self, monkeypatch):
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'LIVE_MODE', True)
        monkeypatch.setattr(config, 'PRIMARY_EXCHANGE', 'binance')
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 100)  # config 值会被忽略

        # mock check_live_balance 返回 1000U
        import live_executor
        monkeypatch.setattr(live_executor, 'check_live_balance',
                            lambda account_id=None: {'available': 800, 'total': 1000})
        monkeypatch.setattr(live_executor, 'check_okx_balance',
                            lambda account_id=None: {'available': 0, 'total': 0})

        state = runtime_config.apply_position_scale()

        assert state['effective_balance'] == 1000.0
        assert state['balance_source'] == 'binance'
        assert state['scale'] == 10.0

    def test_okx_live_pulls_okx_balance(self, monkeypatch):
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'OKX_LIVE_MODE', True)
        monkeypatch.setattr(config, 'PRIMARY_EXCHANGE', 'okx')

        import live_executor
        monkeypatch.setattr(live_executor, 'check_live_balance',
                            lambda account_id=None: {'available': 0, 'total': 0})
        monkeypatch.setattr(live_executor, 'check_okx_balance',
                            lambda account_id=None: {'available': 200, 'total': 250})

        state = runtime_config.apply_position_scale()

        assert state['effective_balance'] == 250.0
        assert state['balance_source'] == 'okx'
        assert state['scale'] == 2.5

    def test_both_mode_sums_balances(self, monkeypatch):
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'LIVE_MODE', True)
        monkeypatch.setattr(config, 'OKX_LIVE_MODE', True)
        monkeypatch.setattr(config, 'PRIMARY_EXCHANGE', 'both')

        import live_executor
        monkeypatch.setattr(live_executor, 'check_live_balance',
                            lambda account_id=None: {'available': 300, 'total': 400})
        monkeypatch.setattr(live_executor, 'check_okx_balance',
                            lambda account_id=None: {'available': 500, 'total': 600})

        state = runtime_config.apply_position_scale()

        # 400 + 600 = 1000
        assert state['effective_balance'] == 1000.0
        assert state['balance_source'] == 'both'
        assert state['scale'] == 10.0

    def test_auto_mode_picks_larger(self, monkeypatch):
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'LIVE_MODE', True)
        monkeypatch.setattr(config, 'OKX_LIVE_MODE', True)
        monkeypatch.setattr(config, 'PRIMARY_EXCHANGE', 'auto')

        import live_executor
        monkeypatch.setattr(live_executor, 'check_live_balance',
                            lambda account_id=None: {'available': 0, 'total': 800})
        monkeypatch.setattr(live_executor, 'check_okx_balance',
                            lambda account_id=None: {'available': 0, 'total': 200})

        state = runtime_config.apply_position_scale()

        # auto 取较大者 (800 > 200) → binance
        assert state['effective_balance'] == 800.0
        assert state['balance_source'] == 'binance'


# ══════════════════════════════════════════════════════════════════
#  边界保护
# ══════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_zero_balance_does_not_zero_out_stake(self, monkeypatch):
        """余额 0 时不应把 DEFAULT_STAKE 缩放成 0（会让风控锁死）"""
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 0)

        state = runtime_config.apply_position_scale()

        # 余额 0 应回退到 PRISTINE 默认值
        assert state['scale'] == 1.0
        pristine_stake = runtime_config.get_pristine_default('DEFAULT_STAKE')
        assert config.DEFAULT_STAKE == pristine_stake

    def test_negative_balance_does_not_invert(self, monkeypatch):
        """负余额（异常情况）不应把 stake 变成负数"""
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', -100)

        runtime_config.apply_position_scale()

        # 负余额应被边界保护拒绝，保持 PRISTINE
        pristine_stake = runtime_config.get_pristine_default('DEFAULT_STAKE')
        assert config.DEFAULT_STAKE == pristine_stake

    def test_live_balance_unavailable_falls_back_to_config(self, monkeypatch):
        """实盘但拉余额失败/返回 0，应 fallback 到 ACCOUNT_BALANCE"""
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'LIVE_MODE', True)
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 200)

        import live_executor
        monkeypatch.setattr(live_executor, 'check_live_balance',
                            lambda account_id=None: {'available': 0, 'total': 0})
        monkeypatch.setattr(live_executor, 'check_okx_balance',
                            lambda account_id=None: {'available': 0, 'total': 0})

        state = runtime_config.apply_position_scale()

        # 拉到 0 → fallback 到 ACCOUNT_BALANCE=200
        assert state['effective_balance'] == 200.0
        assert state['scale'] == 2.0

    def test_minimum_int_field_value_is_1(self, monkeypatch):
        """极小 scale 时 int 字段不能变 0（max(1, ...) 保护）"""
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 1)  # scale = 0.01

        runtime_config.apply_position_scale()

        # PRISTINE COMPOUND_INCREASE=25 × 0.01 = 0.25 → max(1, 0) = 1
        assert config.COMPOUND_INCREASE >= 1


# ══════════════════════════════════════════════════════════════════
#  apply_overrides 集成
# ══════════════════════════════════════════════════════════════════

class TestApplyOverridesIntegration:
    def test_apply_overrides_calls_apply_position_scale(self, tmp_path, monkeypatch):
        """验证 apply_overrides 末尾会自动调 apply_position_scale"""
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')
        monkeypatch.setattr(runtime_config, '_last_applied_mtime', 0.0)

        # POSITION_MODE='proportional' + ACCOUNT_BALANCE=300 → scale=3
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 300)

        runtime_config.apply_overrides(force=True)

        state = runtime_config.get_position_scale_state()
        assert state['mode'] == 'proportional'
        assert state['scale'] == 3.0


# ══════════════════════════════════════════════════════════════════
#  60s TTL 缓存
# ══════════════════════════════════════════════════════════════════

class TestBalanceCache:
    def test_cache_avoids_repeat_fetch(self, monkeypatch):
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'LIVE_MODE', True)
        monkeypatch.setattr(config, 'PRIMARY_EXCHANGE', 'binance')

        # 计数 mock
        call_count = {'n': 0}

        def fake_check_live(account_id=None):
            call_count['n'] += 1
            return {'available': 0, 'total': 500}

        import live_executor
        monkeypatch.setattr(live_executor, 'check_live_balance', fake_check_live)
        monkeypatch.setattr(live_executor, 'check_okx_balance',
                            lambda account_id=None: {'available': 0, 'total': 0})

        # 连续调 3 次，应该只打 1 次交易所
        runtime_config.apply_position_scale()
        runtime_config.apply_position_scale()
        runtime_config.apply_position_scale()

        assert call_count['n'] == 1, f"60s 缓存未生效，被调 {call_count['n']} 次"

    def test_cache_expires_after_60s(self, monkeypatch):
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'LIVE_MODE', True)
        monkeypatch.setattr(config, 'PRIMARY_EXCHANGE', 'binance')

        call_count = {'n': 0}

        def fake_check_live(account_id=None):
            call_count['n'] += 1
            return {'available': 0, 'total': 500}

        import live_executor
        monkeypatch.setattr(live_executor, 'check_live_balance', fake_check_live)
        monkeypatch.setattr(live_executor, 'check_okx_balance',
                            lambda account_id=None: {'available': 0, 'total': 0})

        runtime_config.apply_position_scale()
        # 手工把缓存"过期"
        runtime_config._balance_cache['expires_at'] = time.time() - 1
        runtime_config.apply_position_scale()

        assert call_count['n'] == 2
