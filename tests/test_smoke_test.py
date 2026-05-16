"""
smoke_test 引擎与单个 check 的单元测试

策略：
  * 引擎层（CheckResult / 装饰器 / run_phase / run_phases / _summarize / CLI 参数解析）
    是纯函数 → 直接测
  * 每个 check 函数依赖外部（网络 / 文件 / config）→ 用 monkeypatch 替换依赖
  * 网络相关的 check 用 monkeypatch requests.get / requests.post，不发真请求
"""

import json
import os
import time

import pytest

# conftest 已经 mock 了 ccxt，smoke_test 内部 lazy import 不会触发真 ccxt
import smoke_test
from smoke_test import (
    CheckResult, Status,
    run_phase, run_phases, _summarize, _normalize, main,
    VALID_PHASES, DEFAULT_PHASES,
)


# ══════════════════════════════════════════════════════════════════
#  引擎核心：CheckResult / _normalize / run_phase / _summarize
# ══════════════════════════════════════════════════════════════════

class TestCheckResultModel:
    def test_is_ok_for_pass_skip_warn(self):
        for s in (Status.PASS, Status.SKIP, Status.WARN):
            assert CheckResult(id='X', phase='A', name='n', status=s).is_ok()

    def test_is_ok_false_for_fail(self):
        assert not CheckResult(id='X', phase='A', name='n', status=Status.FAIL).is_ok()


class TestNormalize:
    def test_tuple_form(self):
        t0 = time.monotonic()
        r = _normalize('Z9', 'A', 'name', (Status.PASS, 'ok'), t0)
        assert r.id == 'Z9' and r.phase == 'A' and r.name == 'name'
        assert r.status == Status.PASS and r.detail == 'ok'
        assert r.duration_ms >= 0

    def test_checkresult_form_overrides_meta(self):
        t0 = time.monotonic()
        cr = CheckResult(id='wrong', phase='wrong', name='wrong',
                         status=Status.WARN, detail='inner')
        r = _normalize('Z9', 'B', 'right', cr, t0)
        # _normalize 应当用注册时的 cid/phase/name 覆盖
        assert r.id == 'Z9' and r.phase == 'B' and r.name == 'right'
        assert r.status == Status.WARN and r.detail == 'inner'

    def test_invalid_form_becomes_fail(self):
        t0 = time.monotonic()
        r = _normalize('X', 'A', 'name', "wrong shape", t0)
        assert r.status == Status.FAIL
        assert "格式错误" in r.detail


class TestRunPhaseIsolation:
    """单个 check 抛异常不应中断其他 check"""

    def test_unhandled_exception_becomes_fail(self, monkeypatch):
        # 临时注册一个会抛异常的 check
        def boom():
            raise RuntimeError("synthetic boom")

        # 也注册一个会正常返回的 check（在 boom 之后），验证不被影响
        def ok():
            return Status.PASS, "fine"

        # 用 monkeypatch 替换 _REGISTRY 里的某个阶段为我们临时构造的两条
        monkeypatch.setitem(
            smoke_test._REGISTRY, 'A',
            [('XX1', 'boom check', boom), ('XX2', 'ok check', ok)]
        )
        results = run_phase('A')
        assert len(results) == 2
        assert results[0].status == Status.FAIL
        assert "synthetic boom" in results[0].detail
        assert results[0].error  # 有异常摘要
        assert results[1].status == Status.PASS  # 没被前面的异常波及

    def test_phase_order_preserved(self, monkeypatch):
        seen = []
        for cid, name in [('S1', 'first'), ('S2', 'second'), ('S3', 'third')]:
            seen.append((cid, name))
        monkeypatch.setitem(
            smoke_test._REGISTRY, 'B',
            [(cid, name, lambda c=cid: (Status.PASS, c)) for cid, name in seen]
        )
        results = run_phase('B')
        assert [r.id for r in results] == ['S1', 'S2', 'S3']


class TestSummarize:
    def test_counts(self):
        rs = [
            CheckResult('1', 'A', 'a', Status.PASS),
            CheckResult('2', 'A', 'b', Status.FAIL),
            CheckResult('3', 'A', 'c', Status.WARN),
            CheckResult('4', 'A', 'd', Status.SKIP),
        ]
        s = _summarize(rs, ['A'])
        assert s['total'] == 4
        assert s['pass'] == 1 and s['fail'] == 1 and s['warn'] == 1 and s['skip'] == 1
        assert s['all_ok'] is False

    def test_all_ok_when_no_fail(self):
        rs = [
            CheckResult('1', 'A', 'a', Status.PASS),
            CheckResult('2', 'A', 'b', Status.WARN),
            CheckResult('3', 'A', 'c', Status.SKIP),
        ]
        s = _summarize(rs, ['A'])
        assert s['all_ok'] is True


class TestRunPhasesFiltering:
    def test_unknown_phase_filtered_to_default(self):
        # 全是非法的 → 退回默认 phases
        result = run_phases(['Z', 'X'])
        assert tuple(result['summary']['phases']) == DEFAULT_PHASES

    def test_known_phases_preserved(self, monkeypatch):
        # 让 A 阶段只有 1 条假 check，不依赖外部环境
        monkeypatch.setitem(
            smoke_test._REGISTRY, 'A',
            [('A_FAKE', 'fake', lambda: (Status.PASS, 'ok'))]
        )
        # 只跑 A 不跑 B/D，免得依赖网络
        result = run_phases(['A'])
        assert result['summary']['phases'] == ['A']
        assert result['summary']['total'] == 1
        assert result['summary']['all_ok']


# ══════════════════════════════════════════════════════════════════
#  Phase A · 个别 check 行为
# ══════════════════════════════════════════════════════════════════

class TestPhaseAEnvKeys:
    def test_passes_when_required_set(self, monkeypatch):
        monkeypatch.setenv('TG_BOT_TOKEN', 'abc')
        monkeypatch.setenv('TG_CHAT_ID', '123')
        status, detail = smoke_test._check_env_keys()
        assert status == Status.PASS

    def test_fails_when_missing(self, monkeypatch):
        monkeypatch.delenv('TG_BOT_TOKEN', raising=False)
        monkeypatch.setenv('TG_CHAT_ID', '123')
        status, detail = smoke_test._check_env_keys()
        assert status == Status.FAIL
        assert 'TG_BOT_TOKEN' in detail


class TestPhaseAConfigConsistency:
    def test_passes_when_no_errors(self, monkeypatch):
        import runtime_config
        monkeypatch.setattr(
            runtime_config, 'validate_cross_field_consistency',
            lambda overrides=None, account_id=None: ([], [])
        )
        status, detail = smoke_test._check_config_consistency()
        assert status == Status.PASS

    def test_fail_when_errors(self, monkeypatch):
        import runtime_config
        monkeypatch.setattr(
            runtime_config, 'validate_cross_field_consistency',
            lambda overrides=None, account_id=None: (['stake>balance'], [])
        )
        status, detail = smoke_test._check_config_consistency()
        assert status == Status.FAIL
        assert 'stake>balance' in detail

    def test_warn_when_only_warnings(self, monkeypatch):
        import runtime_config
        monkeypatch.setattr(
            runtime_config, 'validate_cross_field_consistency',
            lambda overrides=None, account_id=None: ([], ['stake>balance*pct'])
        )
        status, detail = smoke_test._check_config_consistency()
        assert status == Status.WARN


class TestPhaseADataFiles:
    def test_pass_for_clean_files(self, tmp_path, monkeypatch):
        import common
        for attr, content in [
            ('TRADES_FILE', '[]'),
            ('CANDIDATES_FILE', '[]'),
            ('RISK_FILE', '{}'),
            ('WEEKLY_REPORT_FILE', '[]'),
        ]:
            p = tmp_path / f"{attr}.json"
            p.write_text(content, encoding='utf-8')
            monkeypatch.setattr(common, attr, str(p))
        status, detail = smoke_test._check_data_files()
        assert status == Status.PASS

    def test_fail_on_corrupt(self, tmp_path, monkeypatch):
        import common
        bad = tmp_path / "trades.json"
        bad.write_text("{not json}", encoding='utf-8')
        monkeypatch.setattr(common, 'TRADES_FILE', str(bad))
        for attr in ['CANDIDATES_FILE', 'RISK_FILE', 'WEEKLY_REPORT_FILE']:
            p = tmp_path / f"{attr}.json"
            p.write_text("[]", encoding='utf-8')
            monkeypatch.setattr(common, attr, str(p))
        status, detail = smoke_test._check_data_files()
        assert status == Status.FAIL
        assert 'trades' in detail


# ══════════════════════════════════════════════════════════════════
#  Phase B · 网络 check（mock requests）
# ══════════════════════════════════════════════════════════════════

class _FakeResp:
    def __init__(self, status_code=200, text=''):
        self.status_code = status_code
        self.text = text


class TestPhaseBNetwork:
    def test_binance_pass(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, 'get', lambda *a, **kw: _FakeResp(200))
        status, _ = smoke_test._check_binance_public()
        assert status == Status.PASS

    def test_binance_fail_on_500(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, 'get', lambda *a, **kw: _FakeResp(500))
        status, detail = smoke_test._check_binance_public()
        assert status == Status.FAIL
        assert '500' in detail

    def test_binance_network_exception(self, monkeypatch):
        import requests
        def boom(*a, **kw):
            raise requests.ConnectionError("DNS down")
        monkeypatch.setattr(requests, 'get', boom)
        status, detail = smoke_test._check_binance_public()
        assert status == Status.FAIL
        assert 'ConnectionError' in detail or 'DNS down' in detail

    def test_telegram_skip_when_no_token(self, monkeypatch):
        monkeypatch.delenv('TG_BOT_TOKEN', raising=False)
        status, detail = smoke_test._check_telegram_api()
        assert status == Status.SKIP

    def test_telegram_fail_on_401(self, monkeypatch):
        import requests
        monkeypatch.setenv('TG_BOT_TOKEN', 'badtoken')
        monkeypatch.setattr(requests, 'get', lambda *a, **kw: _FakeResp(401))
        status, detail = smoke_test._check_telegram_api()
        assert status == Status.FAIL
        assert '401' in detail


# ══════════════════════════════════════════════════════════════════
#  Phase C · 实盘凭证 (mock admin_secrets + exchange_manager)
# ══════════════════════════════════════════════════════════════════

class TestPhaseCExchangeAuth:
    def test_skip_when_no_creds(self, monkeypatch):
        import admin_secrets
        monkeypatch.setattr(
            admin_secrets, 'get_exchange_credentials',
            lambda exchange_id: {'api_key': '', 'secret': ''}
        )
        monkeypatch.delenv('BINANCE_API_KEY', raising=False)
        status, detail = smoke_test._check_exchange_auth('binance')
        assert status == Status.SKIP

    def test_pass_with_balance(self, monkeypatch):
        import admin_secrets
        monkeypatch.setattr(
            admin_secrets, 'get_exchange_credentials',
            lambda eid: {'api_key': 'abc', 'secret': 'def'}
        )
        # 用 fake exchange object 替换 make_exchange
        import exchange_manager

        class _FakeEx:
            def fetch_balance(self):
                return {'USDT': {'total': 1000, 'free': 800}}

        monkeypatch.setattr(exchange_manager, 'make_exchange', lambda *a, **kw: _FakeEx())
        status, detail = smoke_test._check_exchange_auth('binance')
        assert status == Status.PASS
        assert '1000' in detail or '1000.00' in detail

    def test_warn_when_balance_below_stake(self, monkeypatch):
        import admin_secrets
        import exchange_manager
        import config

        monkeypatch.setattr(config, 'DEFAULT_STAKE', 100)
        monkeypatch.setattr(
            admin_secrets, 'get_exchange_credentials',
            lambda eid: {'api_key': 'abc', 'secret': 'def'}
        )

        class _FakeEx:
            def fetch_balance(self):
                return {'USDT': {'total': 10, 'free': 5}}

        monkeypatch.setattr(exchange_manager, 'make_exchange', lambda *a, **kw: _FakeEx())
        status, detail = smoke_test._check_exchange_auth('binance')
        assert status == Status.WARN
        assert 'DEFAULT_STAKE' in detail

    def test_fail_on_fetch_balance_exception(self, monkeypatch):
        import admin_secrets
        import exchange_manager
        monkeypatch.setattr(
            admin_secrets, 'get_exchange_credentials',
            lambda eid: {'api_key': 'abc', 'secret': 'def'}
        )

        class _FakeEx:
            def fetch_balance(self):
                raise Exception("Invalid API-key, IP, or permissions")

        monkeypatch.setattr(exchange_manager, 'make_exchange', lambda *a, **kw: _FakeEx())
        status, detail = smoke_test._check_exchange_auth('binance')
        assert status == Status.FAIL
        assert 'Invalid' in detail


# ══════════════════════════════════════════════════════════════════
#  Phase D · 运行时
# ══════════════════════════════════════════════════════════════════

class TestPhaseDRuntime:
    def test_candidates_warn_when_missing(self, tmp_path, monkeypatch):
        import common
        monkeypatch.setattr(common, 'CANDIDATES_FILE', str(tmp_path / 'absent.json'))
        status, detail = smoke_test._check_candidates_freshness()
        assert status == Status.WARN

    def test_candidates_pass_for_fresh(self, tmp_path, monkeypatch):
        import common
        p = tmp_path / 'cand.json'
        p.write_text(json.dumps([{'symbol': 'X'}, {'symbol': 'Y'}]), encoding='utf-8')
        monkeypatch.setattr(common, 'CANDIDATES_FILE', str(p))
        status, detail = smoke_test._check_candidates_freshness()
        assert status == Status.PASS
        assert 'N=2' in detail

    def test_candidates_warn_for_stale(self, tmp_path, monkeypatch):
        import common
        p = tmp_path / 'cand.json'
        p.write_text('[]', encoding='utf-8')
        # 把 mtime 改到 30 小时前
        old = time.time() - 30 * 3600
        os.utime(str(p), (old, old))
        monkeypatch.setattr(common, 'CANDIDATES_FILE', str(p))
        status, detail = smoke_test._check_candidates_freshness()
        assert status == Status.WARN
        assert '25h' in detail or '未更新' in detail

    def test_trades_pass_when_missing(self, tmp_path, monkeypatch):
        import common
        monkeypatch.setattr(common, 'TRADES_FILE', str(tmp_path / 'absent.json'))
        status, detail = smoke_test._check_trades_health()
        assert status == Status.PASS

    def test_trades_fail_when_not_list(self, tmp_path, monkeypatch):
        import common
        p = tmp_path / 'trades.json'
        p.write_text('{"oops": "not a list"}', encoding='utf-8')
        monkeypatch.setattr(common, 'TRADES_FILE', str(p))
        status, detail = smoke_test._check_trades_health()
        assert status == Status.FAIL


# ══════════════════════════════════════════════════════════════════
#  CLI 参数解析
# ══════════════════════════════════════════════════════════════════

class TestCLI:
    def test_unknown_phase_errors(self, capsys, monkeypatch):
        monkeypatch.setitem(smoke_test._REGISTRY, 'A', [])
        with pytest.raises(SystemExit):
            main(['--phases', 'A,Z'])

    def test_runs_with_valid_phase_returns_int_exit(self, monkeypatch):
        # 让 A 只有 1 条 PASS 假 check
        monkeypatch.setitem(
            smoke_test._REGISTRY, 'A',
            [('A_FAKE', 'fake', lambda: (Status.PASS, 'ok'))]
        )
        # 把 B/C/D 也清空避免 default 参与
        for p in ('B', 'C', 'D'):
            monkeypatch.setitem(smoke_test._REGISTRY, p, [])
        rc = main(['--phases', 'A', '--json', '--no-color'])
        assert rc == 0

    def test_failure_returns_nonzero(self, monkeypatch):
        monkeypatch.setitem(
            smoke_test._REGISTRY, 'A',
            [('A_FAKE', 'fake', lambda: (Status.FAIL, 'broken'))]
        )
        for p in ('B', 'C', 'D'):
            monkeypatch.setitem(smoke_test._REGISTRY, p, [])
        rc = main(['--phases', 'A', '--json'])
        assert rc != 0
