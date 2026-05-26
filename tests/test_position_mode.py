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

    def test_position_mode_is_account_field(self):
        assert 'POSITION_MODE' in runtime_config.ACCOUNT_FIELDS
        assert 'POSITION_MODE' not in runtime_config.GLOBAL_FIELDS

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

    def test_manual_clears_proportional_residue(self, tmp_path, monkeypatch):
        """
        P3-3 回归（2026-05）：从 proportional 切回 manual 必须清除 config 模块里
        被 apply_position_scale 写入的缩放值。否则 COMPOUND_STEP/INCREASE 会
        保留 PROPORTIONAL 时的脏数据（如 143/72），让用户看到错误的状态卡数字。
        """
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')

        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'get_active_account_id',
                            lambda: 'acc_test', raising=False)
        runtime_config.save_account_overrides('acc_test', {'DEFAULT_STAKE': 30})

        # 步骤1: 模拟 proportional 写入了缩放值（直接 monkeypatch config 触发 scale=5）
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 500)
        runtime_config.apply_position_scale()
        # 此时 config.COMPOUND_STEP 应被缩放
        assert config.COMPOUND_STEP > runtime_config.get_pristine_default('COMPOUND_STEP'), (
            f"proportional 时 COMPOUND_STEP 应大于 PRISTINE，实际 {config.COMPOUND_STEP}"
        )

        # 步骤2: 切回 manual
        monkeypatch.setattr(config, 'POSITION_MODE', 'manual')
        runtime_config.apply_position_scale()

        # config 模块必须被清回 PRISTINE，不能保留 proportional 脏值
        pristine_step = runtime_config.get_pristine_default('COMPOUND_STEP')
        assert config.COMPOUND_STEP == pristine_step, (
            f"manual 模式必须把 COMPOUND_STEP 恢复到 PRISTINE {pristine_step}，"
            f"实际还是 {config.COMPOUND_STEP}（proportional 残留）"
        )
        # admin 给 acc_test 设过 DEFAULT_STAKE=30（恰好等于 PRISTINE）→ 用 override
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


# ══════════════════════════════════════════════════════════════════
#  _account_param 在 proportional 模式下应忽略 per-account override
#  （否则 admin 给账号 X 填的 DEFAULT_STAKE=48 会让 proportional 失效）
# ══════════════════════════════════════════════════════════════════

class TestAccountParamShortCircuit:
    """
    回归用户报告的 bug:
        admin 给某账号填了 DEFAULT_STAKE=48 (manual 模式时的值)
        切到 proportional 后，apply_position_scale 把 config.DEFAULT_STAKE
        缩到 86，但 dashboard 显示仍是 48 因为 _account_param 优先读
        per-account override。
        修复：proportional 模式下，4 个被缩放字段强制走 config 模块。
    """

    def test_manual_mode_uses_account_override(self, tmp_path, monkeypatch):
        """manual 模式下行为不变 — 优先 per-account override"""
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')

        # 写一个账号 override
        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'get_active_account_id',
                            lambda: 'acc_test', raising=False)
        runtime_config.save_account_overrides('acc_test', {
            'ACCOUNT_BALANCE': 1000,
            'DEFAULT_STAKE': 48,
        })

        monkeypatch.setattr(config, 'POSITION_MODE', 'manual')

        from common import _account_param
        assert _account_param('acc_test', 'DEFAULT_STAKE', fallback=999) == 48

    def test_proportional_mode_ignores_account_override(self, tmp_path, monkeypatch):
        """proportional 模式下：4 个被缩放字段忽略 per-account override，按 per-account scale 算"""
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')

        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'list_accounts',
                            lambda: [{'id': 'acc_test', 'name': 'T'}], raising=False)
        monkeypatch.setattr(admin_secrets, 'get_active_account_id',
                            lambda: 'acc_test', raising=False)
        runtime_config.save_account_overrides('acc_test', {
            'POSITION_MODE': 'proportional',
            'ACCOUNT_BALANCE': 1000,
            'DEFAULT_STAKE': 48,  # 用户在 admin 改过的值（proportional 下应被忽略）
        })

        # 影子模式 → compute 用账号自己的 ACCOUNT_BALANCE
        monkeypatch.setattr(config, 'LIVE_MODE', False)
        monkeypatch.setattr(config, 'OKX_LIVE_MODE', False)
        runtime_config._balance_cache.update({'value': None, 'source': None, 'expires_at': 0.0})
        runtime_config._per_account_state.clear()
        runtime_config.compute_per_account_scaled()

        from common import _account_param
        # proportional 模式下：忽略 acc_test 的 48，按 scale=10 缩放 PRISTINE
        pristine = runtime_config.get_pristine_default('DEFAULT_STAKE')
        expected = max(1, int(round(pristine * 10)))  # 1000/100 = 10
        result = _account_param('acc_test', 'DEFAULT_STAKE', fallback=999)
        assert result == expected, \
            f"proportional 模式应返回 PRISTINE × scale = {expected}，实际 {result}"

    def test_proportional_mode_does_not_affect_other_fields(self, tmp_path, monkeypatch):
        """proportional 模式下：非缩放字段（如 LEVERAGE）仍走 per-account override"""
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')

        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'list_accounts',
                            lambda: [{'id': 'acc_test', 'name': 'T'}], raising=False)
        monkeypatch.setattr(admin_secrets, 'get_active_account_id',
                            lambda: 'acc_test', raising=False)
        runtime_config.save_account_overrides('acc_test', {
            'POSITION_MODE': 'proportional',
            'ACCOUNT_BALANCE': 1000,
            'LEVERAGE': 5,
        })

        monkeypatch.setattr(config, 'LIVE_MODE', False)
        monkeypatch.setattr(config, 'OKX_LIVE_MODE', False)
        runtime_config._balance_cache.update({'value': None, 'source': None, 'expires_at': 0.0})
        runtime_config._per_account_state.clear()
        runtime_config.compute_per_account_scaled()

        from common import _account_param
        # LEVERAGE 不在 _PROPORTIONAL_FIELDS → 仍优先 per-account 的 5
        assert _account_param('acc_test', 'LEVERAGE', fallback=999) == 5


# ══════════════════════════════════════════════════════════════════
#  P2-2: validate_cross_field_consistency 在 proportional 模式下用 PRISTINE × scale
# ══════════════════════════════════════════════════════════════════

class TestConsistencyCheckProportional:
    """proportional 模式下：一致性检查用 PRISTINE × scale 而不是 override 值"""

    def test_proportional_uses_scaled_stake_not_override(self, monkeypatch):
        # 切到 proportional + 大余额
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        # 模拟 apply_position_scale 已经跑过，scale=5
        runtime_config._position_scale_state.update({
            'mode': 'proportional',
            'effective_balance': 500.0,
            'scale': 5.0,
            'balance_source': 'binance',
            'scaled_fields': {
                'DEFAULT_STAKE': 150,  # 30 × 5
                'RISK_MAX_DAILY_LOSS': 150,
                'COMPOUND_STEP': 250,
                'COMPOUND_INCREASE': 125,
            },
        })

        # admin override 里 stake=30（manual 时用的）
        # 校验时应用 scaled 150，而不是 override 的 30
        errors, warnings_out = runtime_config.validate_cross_field_consistency({
            'DEFAULT_STAKE': 30,  # 这是 admin override
        })
        # 没有 ERROR，因为缩放后 150 < 500
        assert not errors, f"不应该有 error，实际: {errors}"

    def test_proportional_warns_when_scaled_exceeds_balance(self, monkeypatch):
        """缩放后 stake 超过 effective_balance 时应有 ERROR"""
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        # 模拟 effective_balance 极小，scale 巨大
        runtime_config._position_scale_state.update({
            'mode': 'proportional',
            'effective_balance': 50.0,
            'scale': 0.5,
            'balance_source': 'binance',
            'scaled_fields': {
                'DEFAULT_STAKE': 200,  # 假设 PRISTINE 巨大
                'RISK_MAX_DAILY_LOSS': 200,
                'COMPOUND_STEP': 500,
                'COMPOUND_INCREASE': 250,
            },
        })

        errors, _warnings = runtime_config.validate_cross_field_consistency({})
        # stake 200 > balance 50 → ERROR
        assert any('DEFAULT_STAKE' in e for e in errors), f"应该报 stake>balance 错误，实际: {errors}"


# ══════════════════════════════════════════════════════════════════
#  P2-3: COMPOUND_MAX_STAKE warning 用 effective_balance
# ══════════════════════════════════════════════════════════════════

class TestCompoundMaxStakeWarning:
    """proportional 下 300U vs 100U baseline 不再触发 warning"""

    def test_proportional_uses_effective_balance_not_baseline(self, monkeypatch):
        # 用户实际余额 500U（远大于 baseline 100），COMPOUND_MAX_STAKE=300
        monkeypatch.setattr(config, 'POSITION_MODE', 'proportional')
        runtime_config._position_scale_state.update({
            'mode': 'proportional',
            'effective_balance': 500.0,
            'scale': 5.0,
            'balance_source': 'binance',
            'scaled_fields': {'DEFAULT_STAKE': 150, 'RISK_MAX_DAILY_LOSS': 150,
                              'COMPOUND_STEP': 250, 'COMPOUND_INCREASE': 125},
        })

        _errors, warnings_out = runtime_config.validate_cross_field_consistency({
            'AUTO_COMPOUND_ENABLED': True,
            'COMPOUND_MAX_STAKE': 300,
            'ACCOUNT_BALANCE': 100,  # baseline 值，会被 effective_balance 覆盖
        })
        # 300 < 500，应不报 COMPOUND_MAX_STAKE 警告
        assert not any('COMPOUND_MAX_STAKE(300U)' in w and '实际可用本金(500U)' in w
                       for w in warnings_out
                       if 'COMPOUND_MAX_STAKE' in w and '本金' in w), (
            f"proportional + 余额够用时不应报 COMPOUND_MAX_STAKE 超过本金的警告，实际: {warnings_out}"
        )

    def test_manual_mode_still_uses_baseline(self, monkeypatch):
        """动态 cap 修复后：COMPOUND_MAX_STAKE > balance 不再报 WARNING（get_compound_stake 自动限制）"""
        monkeypatch.setattr(config, 'POSITION_MODE', 'manual')
        runtime_config._position_scale_state.update({'mode': 'manual'})

        _errors, warnings_out = runtime_config.validate_cross_field_consistency({
            'AUTO_COMPOUND_ENABLED': True,
            'COMPOUND_MAX_STAKE': 300,
            'ACCOUNT_BALANCE': 100,
            'DEFAULT_STAKE': 30,
        })
        # 动态 cap = min(300, 100×0.5) = 50 → 复利实际不会超 50，不再报 WARNING
        assert not any('COMPOUND_MAX_STAKE' in w for w in warnings_out), (
            f"动态 cap 已保护，不应再有 COMPOUND_MAX_STAKE 警告，实际: {warnings_out}"
        )



# ══════════════════════════════════════════════════════════════════
#  阶段 1（2026-05）每账号独立 POSITION_MODE + 独立 scale
#
#  回归用户原始 bug:
#    "我改了一个账号的单笔仓位，另一个账号也跟着变"
#  根因:
#    apply_position_scale 用全局 config.POSITION_MODE 决定 mode；
#    common._account_param short-circuit 又用 config.POSITION_MODE，
#    所以"活跃账号是 RN(proportional, balance=286)"时，主账户(manual,
#    DEFAULT_STAKE=99)的 stake 也被 proportional short-circuit 拉到
#    86 (PRISTINE 30 × 2.86)。
#
#  修复（阶段 1）:
#    - runtime_config.compute_per_account_scaled() 按每个账号独立算 scale
#    - common._account_param 改走 runtime_config.get_account_scaled_value
#      （读 _per_account_state cache，不再读 config.POSITION_MODE）
# ══════════════════════════════════════════════════════════════════


class TestPerAccountIsolation:
    """每个账号自己的 POSITION_MODE / ACCOUNT_BALANCE / scale 互不污染"""

    def _setup_two_accounts(self, tmp_path, monkeypatch,
                            acc_a_overrides: dict, acc_b_overrides: dict,
                            active='acc_a'):
        """
        通用 setup: 准备两个账号 acc_a / acc_b 各自的 overrides，
        并 mock admin_secrets.list_accounts / get_active_account_id。
        """
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')

        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'list_accounts',
                            lambda: [{'id': 'acc_a', 'name': 'A'},
                                     {'id': 'acc_b', 'name': 'B'}],
                            raising=False)
        monkeypatch.setattr(admin_secrets, 'get_active_account_id',
                            lambda: active, raising=False)

        runtime_config.save_account_overrides('acc_a', acc_a_overrides)
        runtime_config.save_account_overrides('acc_b', acc_b_overrides)

        # 影子模式（两个账号都用自己的 ACCOUNT_BALANCE）
        monkeypatch.setattr(config, 'LIVE_MODE', False)
        monkeypatch.setattr(config, 'OKX_LIVE_MODE', False)
        monkeypatch.setattr(config, 'BASELINE_BALANCE', 100)

        # 清缓存确保 compute_per_account_scaled 拿到新数据
        runtime_config._balance_cache.update({'value': None, 'source': None, 'expires_at': 0.0})
        runtime_config._per_account_state.clear()

    def test_M_plus_P__per_account_stake_independent(self, tmp_path, monkeypatch):
        """A=manual stake=99 / B=proportional balance=286，互不污染（用户原始 bug 场景）"""
        self._setup_two_accounts(tmp_path, monkeypatch,
            acc_a_overrides={'POSITION_MODE': 'manual', 'ACCOUNT_BALANCE': 100,
                             'DEFAULT_STAKE': 99},
            acc_b_overrides={'POSITION_MODE': 'proportional', 'ACCOUNT_BALANCE': 286},
            active='acc_b')  # B 是活跃账号（最容易污染 A 的场景）

        runtime_config.compute_per_account_scaled()

        from common import _account_param
        # A: manual → 应该返回自己的 99，不被 B 的 proportional 污染
        assert _account_param('acc_a', 'DEFAULT_STAKE') == 99
        # B: proportional → PRISTINE × (286/100) = 94
        assert _account_param('acc_b', 'DEFAULT_STAKE') == 94

    def test_M_plus_P__switch_active_does_not_change_per_account(self, tmp_path, monkeypatch):
        """切换活跃账号，每个账号自己的 stake 不变"""
        self._setup_two_accounts(tmp_path, monkeypatch,
            acc_a_overrides={'POSITION_MODE': 'manual', 'ACCOUNT_BALANCE': 100,
                             'DEFAULT_STAKE': 99},
            acc_b_overrides={'POSITION_MODE': 'proportional', 'ACCOUNT_BALANCE': 286},
            active='acc_a')

        runtime_config.compute_per_account_scaled()

        from common import _account_param
        a_stake_when_a_active = _account_param('acc_a', 'DEFAULT_STAKE')
        b_stake_when_a_active = _account_param('acc_b', 'DEFAULT_STAKE')

        # 切换活跃到 B
        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'get_active_account_id',
                            lambda: 'acc_b', raising=False)
        runtime_config._per_account_state.clear()
        runtime_config.compute_per_account_scaled()

        a_stake_when_b_active = _account_param('acc_a', 'DEFAULT_STAKE')
        b_stake_when_b_active = _account_param('acc_b', 'DEFAULT_STAKE')

        assert a_stake_when_a_active == a_stake_when_b_active == 99, \
            "主账户(manual) stake 不应受活跃账号切换影响"
        assert b_stake_when_a_active == b_stake_when_b_active == 94, \
            "RN(proportional) stake 不应受活跃账号切换影响"

    def test_P_plus_P__different_balance_yields_different_scale(self, tmp_path, monkeypatch):
        """两个账号都 proportional 但 balance 不同 → 各自独立 scale"""
        self._setup_two_accounts(tmp_path, monkeypatch,
            acc_a_overrides={'POSITION_MODE': 'proportional', 'ACCOUNT_BALANCE': 200},
            acc_b_overrides={'POSITION_MODE': 'proportional', 'ACCOUNT_BALANCE': 500},
            active='acc_a')

        runtime_config.compute_per_account_scaled()

        from common import _account_param
        pristine_stake = runtime_config.get_pristine_default('DEFAULT_STAKE')
        # A: scale = 200/100 = 2.0
        assert _account_param('acc_a', 'DEFAULT_STAKE') == max(1, int(round(pristine_stake * 2.0)))
        # B: scale = 500/100 = 5.0
        assert _account_param('acc_b', 'DEFAULT_STAKE') == max(1, int(round(pristine_stake * 5.0)))

    def test_modify_one_account_balance_does_not_affect_other(self, tmp_path, monkeypatch):
        """改 A 的 ACCOUNT_BALANCE 不影响 B 的 stake（核心隔离测试）"""
        self._setup_two_accounts(tmp_path, monkeypatch,
            acc_a_overrides={'POSITION_MODE': 'proportional', 'ACCOUNT_BALANCE': 100},
            acc_b_overrides={'POSITION_MODE': 'manual', 'ACCOUNT_BALANCE': 100,
                             'DEFAULT_STAKE': 50},
            active='acc_a')

        runtime_config.compute_per_account_scaled()

        from common import _account_param
        b_stake_before = _account_param('acc_b', 'DEFAULT_STAKE')

        # 改 A 的 balance: 100 → 1000（应该让 A 的 stake 翻 10 倍但 B 不变）
        runtime_config.save_account_overrides('acc_a', {'ACCOUNT_BALANCE': 1000})
        runtime_config._per_account_state.clear()
        runtime_config.compute_per_account_scaled()

        a_stake_after = _account_param('acc_a', 'DEFAULT_STAKE')
        b_stake_after = _account_param('acc_b', 'DEFAULT_STAKE')

        pristine_stake = runtime_config.get_pristine_default('DEFAULT_STAKE')
        assert a_stake_after == max(1, int(round(pristine_stake * 10.0))), \
            f"A 的 stake 应该 = PRISTINE × 10，实际 {a_stake_after}"
        assert b_stake_after == 50, \
            f"B(manual stake=50) 不应被 A 的 balance 改动影响，实际 {b_stake_after} (改前 {b_stake_before})"

    def test_modify_one_account_stake_does_not_affect_other(self, tmp_path, monkeypatch):
        """改 A 的 DEFAULT_STAKE (manual)，不影响 B 的 stake"""
        self._setup_two_accounts(tmp_path, monkeypatch,
            acc_a_overrides={'POSITION_MODE': 'manual', 'ACCOUNT_BALANCE': 100,
                             'DEFAULT_STAKE': 30},
            acc_b_overrides={'POSITION_MODE': 'manual', 'ACCOUNT_BALANCE': 100,
                             'DEFAULT_STAKE': 80},
            active='acc_a')

        runtime_config.compute_per_account_scaled()

        from common import _account_param
        # 改 A 的 stake: 30 → 120
        runtime_config.save_account_overrides('acc_a', {'DEFAULT_STAKE': 120})
        runtime_config._per_account_state.clear()
        runtime_config.compute_per_account_scaled()

        assert _account_param('acc_a', 'DEFAULT_STAKE') == 120
        assert _account_param('acc_b', 'DEFAULT_STAKE') == 80, \
            "B 的 manual stake 不应被 A 的修改影响"

    def test_get_account_scaled_value_returns_none_for_unknown_account(self, tmp_path, monkeypatch):
        """未知账号 → get_account_scaled_value 返回 None，不抛异常"""
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')

        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'list_accounts', lambda: [], raising=False)
        monkeypatch.setattr(admin_secrets, 'get_active_account_id', lambda: '', raising=False)

        runtime_config._per_account_state.clear()
        # 该账号在 admin_secrets 和 runtime_config.json 都不存在
        v = runtime_config.get_account_scaled_value('acc_does_not_exist', 'DEFAULT_STAKE')
        assert v is None

    def test_get_all_account_scale_states_returns_per_account_dict(self, tmp_path, monkeypatch):
        """get_all_account_scale_states 返回每个账号的状态副本（admin panel 用）"""
        self._setup_two_accounts(tmp_path, monkeypatch,
            acc_a_overrides={'POSITION_MODE': 'manual', 'ACCOUNT_BALANCE': 100,
                             'DEFAULT_STAKE': 30},
            acc_b_overrides={'POSITION_MODE': 'proportional', 'ACCOUNT_BALANCE': 500},
            active='acc_a')

        runtime_config.compute_per_account_scaled()
        states = runtime_config.get_all_account_scale_states()

        assert 'acc_a' in states
        assert 'acc_b' in states
        assert states['acc_a']['mode'] == 'manual'
        assert states['acc_b']['mode'] == 'proportional'
        assert states['acc_a']['scale'] == 1.0
        assert states['acc_b']['scale'] == 5.0
        # scaled_fields 都该有 4 个字段
        for acc in ('acc_a', 'acc_b'):
            for k in SCALED:
                assert k in states[acc]['scaled_fields'], \
                    f"{acc} 缺少 scaled_fields[{k}]"

    def test_account_param_for_unknown_account_falls_back_safely(self, tmp_path, monkeypatch):
        """对未知账号调 _account_param 不应崩
        
        2026-05 P3 优化：未知账号 + 显式 fallback → 直接返回 fallback；
        无 fallback → 退到 PRISTINE。get_account_scaled_value 内部已查过
        cache/override/PRISTINE，外层不再重复查询。
        """
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')

        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'list_accounts', lambda: [], raising=False)
        monkeypatch.setattr(admin_secrets, 'get_active_account_id', lambda: '', raising=False)
        runtime_config._per_account_state.clear()

        from common import _account_param
        # 未知账号 + 显式 fallback → 用 fallback
        assert _account_param('acc_unknown', 'DEFAULT_STAKE', fallback=42) == 42
        # 未知账号 + 无 fallback → 退到 PRISTINE
        pristine = runtime_config.get_pristine_default('DEFAULT_STAKE')
        assert _account_param('acc_unknown', 'DEFAULT_STAKE') == pristine


# ══════════════════════════════════════════════════════════════════
#  阶段 5（2026-05）_global 残留账号字段的自动迁移
#
#  旧版本里 POSITION_MODE 是全局字段，存在 _global 段；新版改成账号字段。
#  _load_raw_config 检测到 _global 里有 ACCOUNT_FIELDS 时，把它复制到
#  所有"没有自己 override"的账号下，再从 _global 删除。
# ══════════════════════════════════════════════════════════════════


class TestLegacyGlobalMigration:
    def test_global_position_mode_propagates_to_all_accounts(self, tmp_path, monkeypatch):
        """_global.POSITION_MODE='proportional' 应被复制到没有自己 mode 的账号下"""
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')

        # 写一个旧版结构的 runtime_config.json
        import json
        legacy = {
            '_global': {
                'LIVE_MODE': False,
                'POSITION_MODE': 'proportional',  # ← 残留账号字段
            },
            'acc_a': {
                'ACCOUNT_BALANCE': 100,
                # 没有自己的 POSITION_MODE → 应被 _global 的 'proportional' 填充
            },
            'acc_b': {
                'ACCOUNT_BALANCE': 500,
                'POSITION_MODE': 'manual',  # 已有自己的 → 不应被覆盖
            },
        }
        with open(rt_path, 'w', encoding='utf-8') as f:
            json.dump(legacy, f)

        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'list_accounts',
                            lambda: [{'id': 'acc_a', 'name': 'A'},
                                     {'id': 'acc_b', 'name': 'B'}],
                            raising=False)

        data = runtime_config._load_raw_config()

        # _global 中 POSITION_MODE 已被移除（LIVE_MODE 仍在，因为它是真的全局字段）
        assert 'POSITION_MODE' not in data['_global']
        assert data['_global'].get('LIVE_MODE') is False

        # acc_a 没有自己的 POSITION_MODE → 被填充成 proportional
        assert data['acc_a'].get('POSITION_MODE') == 'proportional'
        # acc_b 已有 manual → 不被覆盖
        assert data['acc_b'].get('POSITION_MODE') == 'manual'

    def test_migration_persists_to_disk(self, tmp_path, monkeypatch):
        """迁移后再读一次，应该已经是新结构"""
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')

        import json
        legacy = {
            '_global': {'POSITION_MODE': 'proportional'},
            'acc_a': {'ACCOUNT_BALANCE': 100},
        }
        with open(rt_path, 'w', encoding='utf-8') as f:
            json.dump(legacy, f)

        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'list_accounts',
                            lambda: [{'id': 'acc_a', 'name': 'A'}], raising=False)

        runtime_config._load_raw_config()  # 触发迁移

        # 再读磁盘
        with open(rt_path, 'r', encoding='utf-8') as f:
            disk = json.load(f)
        assert 'POSITION_MODE' not in disk.get('_global', {})
        assert disk['acc_a'].get('POSITION_MODE') == 'proportional'

    def test_no_migration_when_global_clean(self, tmp_path, monkeypatch):
        """_global 里只有真的全局字段时，不该触发迁移逻辑"""
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')

        import json
        clean = {
            '_global': {'LIVE_MODE': False, 'OKX_LIVE_MODE': False},
            'acc_a': {'POSITION_MODE': 'manual', 'DEFAULT_STAKE': 50},
        }
        with open(rt_path, 'w', encoding='utf-8') as f:
            json.dump(clean, f)

        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'list_accounts',
                            lambda: [{'id': 'acc_a', 'name': 'A'}], raising=False)

        # 取迁移前 mtime
        import os
        mtime_before = os.path.getmtime(rt_path)
        runtime_config._load_raw_config()
        mtime_after = os.path.getmtime(rt_path)

        # 没有触发写盘 → mtime 不变
        assert mtime_after == mtime_before

        # acc_a 的字段保持原样
        with open(rt_path, 'r', encoding='utf-8') as f:
            disk = json.load(f)
        assert disk['acc_a'].get('POSITION_MODE') == 'manual'
        assert disk['acc_a'].get('DEFAULT_STAKE') == 50


# ══════════════════════════════════════════════════════════════════
#  P1 修复（2026-05）：DEFAULT_STAKE > balance × pos_pct 升级为 ERROR
#
#  之前是 WARNING（允许保存），但风控会永远拒绝开仓导致 silent failure。
#  现在改为 ERROR：admin_panel 保存时硬阻塞返回 400，启动时报 TG 告警。
# ══════════════════════════════════════════════════════════════════


class TestStakeExceedsMaxPositionIsError:
    def test_basic_case_now_error(self, monkeypatch):
        """主账户实际场景：balance=100, stake=99, pos_pct=0.5 → max_position=50 → ERROR"""
        monkeypatch.setattr(config, 'POSITION_MODE', 'manual')
        runtime_config._position_scale_state.update({'mode': 'manual'})

        errors, warnings_out = runtime_config.validate_cross_field_consistency({
            'POSITION_MODE': 'manual',
            'ACCOUNT_BALANCE': 100,
            'DEFAULT_STAKE': 99,
            'RISK_MAX_POSITION_PCT': 0.5,
        })
        # 99 < 100 → 不是 stake>balance 的 ERROR；99 > 50 → 升级后是 ERROR（不是 warning）
        assert errors, f"期望 errors 非空，实际: errors={errors}, warnings={warnings_out}"
        assert any('最大持仓上限' in e for e in errors)
        assert not any('最大持仓上限' in w for w in warnings_out)

    def test_save_overrides_rejects_silent_failure_config(self, tmp_path, monkeypatch):
        """save_overrides 在 stake > max_position 时拒绝写盘"""
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')

        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'get_active_account_id',
                            lambda: 'acc_test', raising=False)

        with pytest.raises(ValueError, match="最大持仓上限"):
            runtime_config.save_overrides({
                'POSITION_MODE': 'manual',
                'ACCOUNT_BALANCE': 100,
                'DEFAULT_STAKE': 99,
                'RISK_MAX_POSITION_PCT': 0.5,
            })

    def test_safe_config_no_error(self, monkeypatch):
        """合理配置（stake <= max_position）→ 不报错"""
        monkeypatch.setattr(config, 'POSITION_MODE', 'manual')
        runtime_config._position_scale_state.update({'mode': 'manual'})

        errors, warnings_out = runtime_config.validate_cross_field_consistency({
            'POSITION_MODE': 'manual',
            'ACCOUNT_BALANCE': 100,
            'DEFAULT_STAKE': 30,
            'RISK_MAX_POSITION_PCT': 0.5,
        })
        # 30 < 50 → 一切正常
        assert not any('最大持仓上限' in e for e in errors)
        assert not any('最大持仓上限' in w for w in warnings_out)
