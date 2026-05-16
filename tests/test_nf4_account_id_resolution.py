"""
NF-4 回归测试：release_partial_stake / record_trade_closed 在调用方
未指定 ``account_id`` 但传入 ``trade_account_id`` 时，必须优先用后者，
不允许回退到当前活跃账户。

修复语义：
    if account_id is None and trade_account_id is not None:
        account_id = trade_account_id

边界场景：
    * trade_account_id == ''      → 老数据（v4.3 之前）→ 落到 '_default' 桶
    * trade_account_id == 'acc_X' → 多账户标记 → 落到 'acc_X' 桶
    * trade_account_id is None    → 调用方真的不知道 → 才回退到当前活跃账户
"""

import pytest

# conftest 已经 mock 了 ccxt
import common
import risk_control
from risk_control import (
    release_partial_stake, record_trade_closed,
    _resolve_account_id, _state_from_data,
    RiskState,
)


@pytest.fixture(autouse=True)
def patch_risk_files(monkeypatch, tmp_path):
    risk_file = str(tmp_path / "risk_state.json")
    trades_file = str(tmp_path / "trades.json")
    monkeypatch.setattr(common, 'RISK_FILE', risk_file)
    monkeypatch.setattr(common, 'TRADES_FILE', trades_file)
    monkeypatch.setattr(risk_control, 'RISK_FILE', risk_file)
    monkeypatch.setattr(risk_control, 'TRADES_FILE', trades_file)


@pytest.fixture
def active_account_acc_a(monkeypatch):
    """让 get_current_account_id() 返回 'acc_A'，模拟多账户启用且 acc_A 当前活跃"""
    monkeypatch.setattr(risk_control, 'get_current_account_id', lambda: 'acc_A')
    # 同时 mock common 内部的引用（_calc_actual_open_stake 间接经过 common）
    monkeypatch.setattr(common, 'get_current_account_id', lambda: 'acc_A',
                        raising=False)


def _seed_two_buckets(default_stake: float = 100.0, acc_a_stake: float = 200.0):
    """初始化 risk_state 文件：'_default' 和 'acc_A' 两个独立桶"""
    state_default = RiskState()
    state_default.total_open_stake = default_stake
    risk_control.save_risk_state(state_default, account_id='')

    state_acc_a = RiskState()
    state_acc_a.total_open_stake = acc_a_stake
    risk_control.save_risk_state(state_acc_a, account_id='acc_A')


def _read_bucket_stake(account_id: str) -> float:
    """直接读 RISK_FILE 拿到指定 bucket 的 total_open_stake"""
    state = risk_control.load_risk_state(account_id=account_id)
    return state.total_open_stake


# ══════════════════════════════════════════════════════════════════
#  NF-4: release_partial_stake
# ══════════════════════════════════════════════════════════════════

class TestNF4ReleasePartialStakeAccountIdPriority:

    def test_empty_trade_account_id_routes_to_default_bucket_not_active(
        self, active_account_acc_a, mock_config
    ):
        """
        关键回归：trade.account_id == '' （老数据）→ trade_account_id=''
        必须落到 '_default' 桶，而不是当前活跃的 acc_A。
        """
        _seed_two_buckets(default_stake=100.0, acc_a_stake=200.0)

        release_partial_stake(50.0, trade_account_id='')

        # _default 桶应被减 50
        assert _read_bucket_stake('') == pytest.approx(50.0), (
            "trade_account_id='' 应该落到 _default 桶（老数据全局）"
        )
        # acc_A 桶必须丝毫不变 —— 这是 NF-4 修复的核心
        assert _read_bucket_stake('acc_A') == pytest.approx(200.0), (
            "NF-4 回归：trade_account_id='' 不该影响当前活跃账户 acc_A 的 stake"
        )

    def test_named_trade_account_id_routes_to_that_bucket(
        self, active_account_acc_a, mock_config
    ):
        """trade_account_id='acc_A' 直接锁定到 acc_A 桶（与之前行为一致）"""
        _seed_two_buckets(default_stake=100.0, acc_a_stake=200.0)

        release_partial_stake(50.0, trade_account_id='acc_A')

        assert _read_bucket_stake('acc_A') == pytest.approx(150.0)
        assert _read_bucket_stake('') == pytest.approx(100.0)

    def test_explicit_account_id_overrides_trade_account_id(
        self, active_account_acc_a, mock_config
    ):
        """显式 account_id 优先级最高 —— trade_account_id 仅在 account_id is None 时才用"""
        _seed_two_buckets(default_stake=100.0, acc_a_stake=200.0)

        # 故意制造矛盾：account_id='acc_A'，trade_account_id=''
        release_partial_stake(30.0, account_id='acc_A', trade_account_id='')

        # account_id 显式胜出 → acc_A 减 30
        assert _read_bucket_stake('acc_A') == pytest.approx(170.0)
        assert _read_bucket_stake('') == pytest.approx(100.0)

    def test_both_none_falls_back_to_current_active(
        self, active_account_acc_a, mock_config
    ):
        """两个都是 None 时维持旧行为：回退到当前活跃账户"""
        _seed_two_buckets(default_stake=100.0, acc_a_stake=200.0)

        release_partial_stake(40.0)  # account_id=None, trade_account_id=None

        # 没有任何 trade 信息 → 回退到当前活跃 acc_A
        assert _read_bucket_stake('acc_A') == pytest.approx(160.0)
        assert _read_bucket_stake('') == pytest.approx(100.0)

    def test_zero_stake_is_noop(self, active_account_acc_a, mock_config):
        """stake <= 0 早返回，不动任何桶（保留原有保护）"""
        _seed_two_buckets(default_stake=100.0, acc_a_stake=200.0)
        release_partial_stake(0.0, trade_account_id='')
        release_partial_stake(-5.0, trade_account_id='acc_A')
        assert _read_bucket_stake('') == pytest.approx(100.0)
        assert _read_bucket_stake('acc_A') == pytest.approx(200.0)


# ══════════════════════════════════════════════════════════════════
#  NF-4: record_trade_closed
# ══════════════════════════════════════════════════════════════════

class TestNF4RecordTradeClosedAccountIdPriority:

    def test_empty_trade_account_id_routes_to_default_bucket(
        self, active_account_acc_a, mock_config
    ):
        """老数据平仓亏损只能记到 _default 桶"""
        _seed_two_buckets(default_stake=100.0, acc_a_stake=200.0)

        record_trade_closed(pnl=-15.0, stake=100.0, trade_account_id='')

        default_state = risk_control.load_risk_state(account_id='')
        acc_a_state = risk_control.load_risk_state(account_id='acc_A')

        # _default 桶记到亏损与连亏
        assert default_state.daily_loss == pytest.approx(15.0)
        assert default_state.consecutive_losses == 1
        assert default_state.total_open_stake == pytest.approx(0.0)  # 100-100

        # acc_A 桶不应被污染
        assert acc_a_state.daily_loss == pytest.approx(0.0), (
            "NF-4 回归：trade_account_id='' 不该污染 acc_A 的 daily_loss"
        )
        assert acc_a_state.consecutive_losses == 0
        assert acc_a_state.total_open_stake == pytest.approx(200.0)

    def test_named_trade_account_id_routes_correctly(
        self, active_account_acc_a, mock_config
    ):
        """trade_account_id='acc_A' 直接落到 acc_A 桶"""
        _seed_two_buckets(default_stake=100.0, acc_a_stake=200.0)

        record_trade_closed(pnl=-10.0, stake=50.0, trade_account_id='acc_A')

        acc_a_state = risk_control.load_risk_state(account_id='acc_A')
        default_state = risk_control.load_risk_state(account_id='')

        assert acc_a_state.daily_loss == pytest.approx(10.0)
        assert acc_a_state.consecutive_losses == 1
        assert acc_a_state.total_open_stake == pytest.approx(150.0)

        assert default_state.daily_loss == pytest.approx(0.0)
        assert default_state.consecutive_losses == 0

    def test_explicit_account_id_overrides_trade_account_id(
        self, active_account_acc_a, mock_config
    ):
        """显式 account_id 胜出"""
        _seed_two_buckets(default_stake=100.0, acc_a_stake=200.0)

        record_trade_closed(
            pnl=-5.0, stake=50.0,
            account_id='acc_A', trade_account_id='',
        )

        acc_a_state = risk_control.load_risk_state(account_id='acc_A')
        default_state = risk_control.load_risk_state(account_id='')

        assert acc_a_state.daily_loss == pytest.approx(5.0)
        assert default_state.daily_loss == pytest.approx(0.0)

    def test_both_none_falls_back_to_current_active(
        self, active_account_acc_a, mock_config
    ):
        """两个都 None → 旧行为：用当前活跃账户"""
        _seed_two_buckets(default_stake=100.0, acc_a_stake=200.0)

        record_trade_closed(pnl=-7.0, stake=50.0)

        acc_a_state = risk_control.load_risk_state(account_id='acc_A')
        assert acc_a_state.daily_loss == pytest.approx(7.0)


# ══════════════════════════════════════════════════════════════════
#  NF-4: 端到端 —— 模拟 caller 用新 trade_account_id kwarg
# ══════════════════════════════════════════════════════════════════

class TestNF4CallerWiring:
    """验证新 kwarg 与 'or None' 旧模式的语义差异"""

    def test_new_kwarg_preserves_empty_string(self, active_account_acc_a, mock_config):
        """新 kwarg 路径下空字符串保留语义（不像 'or None' 会丢失）"""
        _seed_two_buckets(default_stake=100.0, acc_a_stake=200.0)

        # 新风格：直接传 _pacc，不再用 or None
        for stake_to_release in [25.0, 25.0]:
            release_partial_stake(stake_to_release, trade_account_id='')

        # 两次释放 25 → _default 共减 50
        assert _read_bucket_stake('') == pytest.approx(50.0)
        assert _read_bucket_stake('acc_A') == pytest.approx(200.0)
