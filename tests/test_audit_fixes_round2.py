"""
回归测试：is_in_cooldown 语义统一 / dashboard 日志脱敏 / 配置硬阻塞 (2026-05)
"""

import io
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# conftest 已经 mock 了 ccxt

import common
import config
from common import atomic_write_json
import risk_control
import runtime_config


# ══════════════════════════════════════════════════════════════════
#  is_in_cooldown 语义统一：'' 与 None 都视为全账户扫描
# ══════════════════════════════════════════════════════════════════

class TestCooldownSemanticsUnified:
    @pytest.fixture(autouse=True)
    def patch_files(self, monkeypatch, tmp_path):
        risk_file = str(tmp_path / "risk.json")
        trades_file = str(tmp_path / "trades.json")
        monkeypatch.setattr(common, 'RISK_FILE', risk_file)
        monkeypatch.setattr(common, 'TRADES_FILE', trades_file)
        monkeypatch.setattr(risk_control, 'RISK_FILE', risk_file)
        monkeypatch.setattr(risk_control, 'TRADES_FILE', trades_file)
        return {'trades': trades_file}

    def _seed(self, path, account_id):
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        atomic_write_json(path, [{
            'symbol': 'PEPE/USDT', 'status': 'closed',
            'closed_at': recent, 'close_type': 'hard_stop',
            'account_id': account_id,
        }])

    def test_none_means_global_scan(self, patch_files, mock_config):
        """account_id=None → 全账户扫描"""
        self._seed(patch_files['trades'], 'acc_A')
        in_cd, _ = risk_control.is_in_cooldown('PEPE/USDT', account_id=None)
        assert in_cd is True

    def test_empty_string_means_global_scan(self, patch_files, mock_config):
        """account_id='' → 全账户扫描（与 None 行为统一）

        关键回归点：旧实现 '' 被 _resolve_account_id 转成 '_default' 后跳过 filter
        刚好也是全扫，但语义混乱（注释写"仅老数据"）。新实现明确：'' = None。
        """
        self._seed(patch_files['trades'], 'acc_A')
        in_cd, _ = risk_control.is_in_cooldown('PEPE/USDT', account_id='')
        assert in_cd is True, (
            "account_id='' 应当与 None 行为一致，跨账户扫描"
        )

    def test_explicit_other_account_isolates(self, patch_files, mock_config):
        """显式传 'acc_B' → 只看 acc_B 的交易，acc_A 的止损不影响"""
        self._seed(patch_files['trades'], 'acc_A')
        in_cd, _ = risk_control.is_in_cooldown('PEPE/USDT', account_id='acc_B')
        assert in_cd is False

    def test_explicit_matching_account(self, patch_files, mock_config):
        """显式传 'acc_A' → 命中 acc_A 自己的止损"""
        self._seed(patch_files['trades'], 'acc_A')
        in_cd, _ = risk_control.is_in_cooldown('PEPE/USDT', account_id='acc_A')
        assert in_cd is True

    def test_legacy_empty_account_id_seen_when_global(self, patch_files, mock_config):
        """老数据 account_id='' → 默认全扫语义下也能看到（向后兼容）"""
        self._seed(patch_files['trades'], '')
        for acc in (None, ''):
            in_cd, _ = risk_control.is_in_cooldown('PEPE/USDT', account_id=acc)
            assert in_cd is True, f"account_id={acc!r} 应能看到老数据"


# ══════════════════════════════════════════════════════════════════
#  dashboard 日志脱敏（不打印 ADMIN_URL_SECRET 长度）
# ══════════════════════════════════════════════════════════════════

class TestDashboardLogSanitization:
    """
    测试 dashboard 启动消息不暴露 ADMIN_URL_SECRET 任何信息。
    这里通过静态读 dashboard.py 源代码确认不再含旧的泄露 print。
    """

    def test_no_secret_length_in_source(self):
        """dashboard.py 不应"打印"长度信息（仅判断 len() 是允许的）"""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'dashboard.py'
        )
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()

        offenders = []
        for lineno, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            # 只检查 print 语句里是否含 len(_admin_url_secret) 或 len(_token)
            # （ if len(...) < 16: 这种判断不算）
            is_print_or_msg = (
                'print(' in stripped
                or 'logger' in stripped.lower()
                or stripped.startswith('"')
                or stripped.startswith("'")
                or 'f"' in stripped
                or "f'" in stripped
            )
            if not is_print_or_msg:
                continue
            if 'len(_admin_url_secret)' in line and 'print' in line:
                offenders.append(f"line {lineno}: {stripped}")
            if '"secret 长度' in line or "'secret 长度" in line:
                offenders.append(f"line {lineno}: {stripped}")
            if '"<ADMIN_URL_SECRET>' in line:
                offenders.append(f"line {lineno}: {stripped}")
            if 'len(_token)' in line and ('print' in line or 'f"' in line or "f'" in line):
                # 排除赋值/比较，只看 print/f-string
                if '<' not in line.split('len(_token)')[0][-3:]:  # 不是 if len(_token) <
                    offenders.append(f"line {lineno}: {stripped}")
            if '"DASHBOARD_TOKEN 长度仅' in line or "'DASHBOARD_TOKEN 长度仅" in line:
                offenders.append(f"line {lineno}: {stripped}")
        assert not offenders, (
            "dashboard.py 仍含暴露 secret/token 长度的 print:\n"
            + "\n".join(offenders)
        )

    def test_required_message_present(self):
        """启用路径必须保留 'Admin Panel 已启用 ✓' 提示"""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'dashboard.py'
        )
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()
        assert '🔐 Admin Panel 已启用 ✓' in src


# ══════════════════════════════════════════════════════════════════
#  配置硬阻塞：DEFAULT_STAKE > ACCOUNT_BALANCE → errors，admin 返回 400
# ══════════════════════════════════════════════════════════════════

class TestConfigHardBlock:
    def test_returns_tuple_errors_warnings(self, monkeypatch):
        """validate_cross_field_consistency 返回 (errors, warnings) 元组"""
        result = runtime_config.validate_cross_field_consistency({
            'ACCOUNT_BALANCE': 100,
            'DEFAULT_STAKE': 50,
            'RISK_MAX_POSITION_PCT': 0.5,
        })
        assert isinstance(result, tuple)
        assert len(result) == 2
        errors, warnings = result
        assert isinstance(errors, list)
        assert isinstance(warnings, list)

    def test_stake_exceeds_balance_is_error(self):
        """DEFAULT_STAKE > ACCOUNT_BALANCE → errors 非空"""
        errors, warnings = runtime_config.validate_cross_field_consistency({
            'ACCOUNT_BALANCE': 100,
            'DEFAULT_STAKE': 200,
            'RISK_MAX_POSITION_PCT': 0.5,
        })
        assert errors, "stake > balance 应当返回 error"
        assert any('保证金超过本金' in e for e in errors)

    def test_stake_exceeds_max_position_is_warning(self):
        """DEFAULT_STAKE > max_position 但 ≤ balance → warnings"""
        errors, warnings = runtime_config.validate_cross_field_consistency({
            'ACCOUNT_BALANCE': 100,
            'DEFAULT_STAKE': 80,
            'RISK_MAX_POSITION_PCT': 0.5,
        })
        # 80 < 100 → 不是 error；但 80 > 50（=100*0.5） → warning
        assert not errors
        assert warnings
        assert any('最大持仓上限' in w for w in warnings)

    def test_safe_config_no_errors_no_warnings(self):
        """合理配置 → 都为空"""
        errors, warnings = runtime_config.validate_cross_field_consistency({
            'ACCOUNT_BALANCE': 1000,
            'DEFAULT_STAKE': 50,
            'RISK_MAX_POSITION_PCT': 0.5,
        })
        assert not errors
        assert not warnings

    def test_save_overrides_raises_on_error(self, tmp_path, monkeypatch):
        """save_overrides 遇到 errors 直接 raise ValueError 拒绝写盘"""
        rt_file = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_file)

        with pytest.raises(ValueError) as ei:
            runtime_config.save_overrides({
                'ACCOUNT_BALANCE': 50,
                'DEFAULT_STAKE': 100,  # 超过 balance
            })
        assert '保证金超过本金' in str(ei.value)

        # 文件不应被创建（拒绝写盘）
        assert not os.path.exists(rt_file)

    def test_save_overrides_succeeds_on_warning_only(self, tmp_path, monkeypatch):
        """warnings（非 errors）不阻止 save_overrides"""
        rt_file = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_file)

        # warning 级别：80 < 100 但 80 > 50（max_position）
        runtime_config.save_overrides({
            'ACCOUNT_BALANCE': 100,
            'DEFAULT_STAKE': 80,
            'RISK_MAX_POSITION_PCT': 0.5,
        })
        assert os.path.exists(rt_file)
