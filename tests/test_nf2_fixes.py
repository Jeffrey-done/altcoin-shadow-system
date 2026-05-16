"""
NF2-1 ~ NF2-5 回归测试（Round-2 final audit 后续修复）

覆盖修复:
    NF2-1: runtime_config.save_overrides RMW 加锁，防止并发写丢字段
    NF2-2: save_account_overrides / save_global_overrides 改为字段级合并
    NF2-3: api_set_credentials 给 Binance 提交 passphrase-only 时显式拒绝
    NF2-4: is_in_cooldown 用 astimezone(UTC).date() 处理本地时区写入
    NF2-5: task_metrics.record 与 truncate 用排他锁串行化

设计原则:
    - 每个 NF 一个 Test class，互相独立
    - 不依赖网络 / 不依赖真实交易所；conftest 已 mock ccxt
    - tmp_path 隔离运行时文件，避免污染真实仓库数据
"""

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

import pytest

# conftest 已经 mock 了 ccxt
import common
import runtime_config
import task_metrics
import risk_control


# ══════════════════════════════════════════════════════════════════
#  NF2-1: runtime_config.save_overrides 锁
# ══════════════════════════════════════════════════════════════════

class TestNF21SaveOverridesLocked:
    """save_overrides 现在用 _locked_config() 包裹整段 RMW"""

    def test_locked_config_context_exists(self):
        """模块顶层必须有 _locked_config 与 _RUNTIME_LOCK"""
        assert hasattr(runtime_config, '_locked_config'), (
            "NF2-1 回归：_locked_config 上下文管理器必须存在"
        )
        assert hasattr(runtime_config, '_RUNTIME_LOCK')

    def test_save_overrides_uses_locked_context(self):
        """静态扫描确认 save_overrides 调用了 _locked_config"""
        import inspect
        src = inspect.getsource(runtime_config.save_overrides)
        assert '_locked_config' in src, (
            "NF2-1 回归：save_overrides 必须用 _locked_config 包 RMW；"
            "否则两个 admin worker 并发写会丢字段"
        )

    def test_save_account_overrides_uses_locked_context(self):
        import inspect
        src = inspect.getsource(runtime_config.save_account_overrides)
        assert '_locked_config' in src

    def test_save_global_overrides_uses_locked_context(self):
        import inspect
        src = inspect.getsource(runtime_config.save_global_overrides)
        assert '_locked_config' in src

    def test_concurrent_save_overrides_no_lost_update(self, tmp_path, monkeypatch):
        """
        端到端验证：N 个线程并发 save_overrides，每个写入不同字段；
        最终文件里应能找到所有写入的字段（在 mock 出 active_account 后）。

        NF2-1 修复前：load → 修改 → save 之间无锁，并发写丢字段。
        修复后：_locked_config 排他锁串行化所有 RMW。
        """
        # 重定向到临时目录，避免污染真实文件
        rt_path = str(tmp_path / "runtime_config.json")
        lock_path = rt_path + '.lock'
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', lock_path)

        # mock 活跃账户为固定 ID，让 ACCOUNT_FIELDS 写入有归属
        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'get_active_account_id',
                            lambda: 'acc_test')

        # 不要触发跨字段一致性 ERROR：用合理的 stake 值
        # ACCOUNT_BALANCE=1000 / DEFAULT_STAKE=10..30 都满足 stake <= balance
        def writer(field_value):
            field, value = field_value
            runtime_config.save_overrides({
                'ACCOUNT_BALANCE': 1000,
                field: value,
            })

        # 8 个线程，每个写不同字段（避免互相覆盖同一 key 的歧义）
        # 选择互不冲突的字段，所有都属于 ACCOUNT_FIELDS
        targets = [
            ('DEFAULT_STAKE', 10),
            ('LEVERAGE', 5),
            ('OKX_DEFAULT_LEVERAGE', 5),
            ('TP1_MULTIPLIER', 0.95),
            ('TP2_MULTIPLIER', 0.92),
            ('HARD_STOP_LOSS_PCT', 5.0),
            ('RISK_MAX_DAILY_TRADES', 3),
            ('COOLDOWN_HOURS', 24),
        ]

        threads = [
            threading.Thread(target=writer, args=(t,)) for t in targets
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive(), "线程超时，可能死锁"

        # 读回文件，所有 8 个字段都应在 acc_test 下
        with open(rt_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        acc_data = data.get('acc_test', {})
        for field, value in targets:
            assert field in acc_data, (
                f"NF2-1 回归：并发写丢失了 {field}（{len(acc_data)}/{len(targets)} 字段保留）。"
                f"完整内容: {acc_data}"
            )
            # 值类型可能因 json 反序列化变成 int/float，不强制类型一致
            assert acc_data[field] == value or acc_data[field] == pytest.approx(value)

    def test_save_overrides_still_validates_consistency(self, tmp_path, monkeypatch):
        """配置一致性硬阻塞仍然生效（即便加了锁）"""
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')

        # DEFAULT_STAKE > ACCOUNT_BALANCE → 必须 raise
        with pytest.raises(ValueError, match="配置一致性校验失败"):
            runtime_config.save_overrides({
                'ACCOUNT_BALANCE': 100,
                'DEFAULT_STAKE': 200,
            })


# ══════════════════════════════════════════════════════════════════
#  NF2-2: save_account_overrides / save_global_overrides 改为合并
# ══════════════════════════════════════════════════════════════════

class TestNF22FieldLevelMerge:
    """save_account_overrides / save_global_overrides 应保留既有字段"""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        rt_path = str(tmp_path / "runtime_config.json")
        monkeypatch.setattr(runtime_config, 'RUNTIME_CONFIG_FILE', rt_path)
        monkeypatch.setattr(runtime_config, '_RUNTIME_LOCK', rt_path + '.lock')
        # mock active account
        import admin_secrets
        monkeypatch.setattr(admin_secrets, 'get_active_account_id',
                            lambda: 'acc_X', raising=False)
        yield rt_path

    def test_save_account_overrides_does_not_clear_other_fields(self):
        """先存两个字段，再用 save_account_overrides 写另一个字段，原字段不应被清空"""
        # 第一次：写 ACCOUNT_BALANCE 和 DEFAULT_STAKE
        runtime_config.save_account_overrides('acc_X', {
            'ACCOUNT_BALANCE': 500,
            'DEFAULT_STAKE': 30,
        })

        # 第二次：只写 LEVERAGE
        runtime_config.save_account_overrides('acc_X', {
            'LEVERAGE': 5,
        })

        # 三个字段都应存在
        overrides = runtime_config.load_account_overrides('acc_X')
        assert overrides.get('ACCOUNT_BALANCE') == 500, (
            "NF2-2 回归：save_account_overrides 不应清除既有 ACCOUNT_BALANCE"
        )
        assert overrides.get('DEFAULT_STAKE') == 30
        assert overrides.get('LEVERAGE') == 5

    def test_save_global_overrides_does_not_clear_other_fields(self):
        """global 字段同样合并"""
        runtime_config.save_global_overrides({
            'LIVE_MODE': True,
            'OKX_LIVE_MODE': False,
        })
        runtime_config.save_global_overrides({
            'PRIMARY_EXCHANGE': 'binance',
        })

        glob = runtime_config.load_global_overrides()
        assert glob.get('LIVE_MODE') is True, (
            "NF2-2 回归：save_global_overrides 不应清除既有 LIVE_MODE"
        )
        assert glob.get('OKX_LIVE_MODE') is False
        assert glob.get('PRIMARY_EXCHANGE') == 'binance'

    def test_save_account_overrides_overwrites_same_field(self):
        """对同一字段更新仍然生效（合并语义不等于忽略新值）"""
        runtime_config.save_account_overrides('acc_X', {'DEFAULT_STAKE': 30})
        runtime_config.save_account_overrides('acc_X', {'DEFAULT_STAKE': 50})
        assert runtime_config.load_account_overrides('acc_X').get('DEFAULT_STAKE') == 50

    def test_save_account_overrides_validates(self):
        """非 ACCOUNT_FIELDS 字段被静默忽略；非法值也被忽略，不影响其他字段"""
        runtime_config.save_account_overrides('acc_X', {
            'DEFAULT_STAKE': 30,
            'LIVE_MODE': True,        # 不属于 ACCOUNT_FIELDS，应被忽略
            'LEVERAGE': 999,           # 超出 [1, 20]，应被忽略
        })
        overrides = runtime_config.load_account_overrides('acc_X')
        assert overrides.get('DEFAULT_STAKE') == 30
        assert 'LIVE_MODE' not in overrides
        assert 'LEVERAGE' not in overrides


# ══════════════════════════════════════════════════════════════════
#  NF2-3: api_set_credentials passphrase-only Binance 拒绝
# ══════════════════════════════════════════════════════════════════
#
# admin_panel 的 api_set_credentials 是闭包函数（嵌在 create_blueprint 里），
# 直接通过 Flask test client 测试是最稳的方式。

@pytest.fixture
def admin_app(tmp_path, monkeypatch):
    """创建一个最小化 Flask app，挂载 admin blueprint，并 mock 所有外部依赖"""
    from flask import Flask
    import admin_secrets
    import admin_panel

    # 隔离 secrets 文件
    secrets_path = str(tmp_path / "admin_secrets.json")
    monkeypatch.setattr(admin_secrets, 'SECRETS_FILE', secrets_path)
    monkeypatch.setattr(admin_secrets, '_SECRETS_LOCK', secrets_path + '.lock')

    # 隔离 audit log + rate limit
    audit_path = str(tmp_path / "admin_audit.log")
    rate_limit_path = str(tmp_path / ".admin_ratelimit.json")
    monkeypatch.setattr(admin_panel, 'AUDIT_LOG', audit_path)
    monkeypatch.setattr(admin_panel, 'RATE_LIMIT_FILE', rate_limit_path)

    # 初始化一个测试账户
    admin_secrets.set_password('test-password-12345')
    admin_secrets.create_account('TestAcc')

    app = Flask(__name__,
                template_folder=os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'templates'),
                )
    app.config['SECRET_KEY'] = 'test-secret-key'
    app.config['TESTING'] = True

    bp = admin_panel.create_blueprint('test-secret-prefix-32-chars-or-more')
    app.register_blueprint(bp)
    return app


def _login_session(client):
    """模拟一个已登录 session（绕过 login，因为 TOTP 不好在测试里走全流程）"""
    with client.session_transaction() as sess:
        now = time.time()
        sess['admin_auth'] = {
            'created_at': now,
            'last_seen': now,
            'last_totp_at': now,    # 避免 fresh_totp 检查弹框
        }
        sess['csrf_token'] = 'test-csrf-token'


class TestNF23PassphraseOnlyBinanceRejected:
    """Binance passphrase-only POST 必须显式 400，不能静默吞"""

    def test_passphrase_only_binance_returns_400(self, admin_app):
        client = admin_app.test_client()
        _login_session(client)
        # mock TOTP 验证不强制（is_totp_enabled() False）
        # 但需要 disable TOTP fresh check on the route
        # 因为我们 last_totp_at 给了 now，应该过 fresh
        rv = client.post(
            '/test-secret-prefix-32-chars-or-more/api/credentials/binance',
            json={'passphrase': 'something'},
            headers={'X-Admin-CSRF': 'test-csrf-token'},
        )
        assert rv.status_code == 400, (
            f"NF2-3 回归：Binance + passphrase-only 必须 400，实际 {rv.status_code}: {rv.data}"
        )
        body = rv.get_json() or {}
        assert 'error' in body
        # 错误消息应清晰指出 binance 不用 passphrase
        assert 'passphrase' in body['error'].lower() or 'binance' in body['error']

    def test_okx_passphrase_only_still_works(self, admin_app, monkeypatch):
        """OKX 接受 passphrase-only（OKX 确实需要这个字段）"""
        import admin_secrets
        # mock set_exchange_credentials 不实际写文件（避免依赖文件状态）
        called_with = {}
        def mock_set(exchange, **kwargs):
            called_with[exchange] = kwargs
        monkeypatch.setattr(admin_secrets, 'set_exchange_credentials', mock_set)

        client = admin_app.test_client()
        _login_session(client)
        rv = client.post(
            '/test-secret-prefix-32-chars-or-more/api/credentials/okx',
            json={'passphrase': 'okx-pass'},
            headers={'X-Admin-CSRF': 'test-csrf-token'},
        )
        assert rv.status_code == 200, (
            f"OKX 仍应接受 passphrase-only 更新，实际 {rv.status_code}: {rv.data}"
        )
        body = rv.get_json() or {}
        assert body.get('ok') is True
        assert called_with.get('okx', {}).get('passphrase') == 'okx-pass'

    def test_binance_with_apikey_and_passphrase_drops_passphrase(self, admin_app, monkeypatch):
        """Binance + api_key + passphrase: 保存 api_key，丢弃 passphrase 并 warning"""
        import admin_secrets
        called_with = {}
        def mock_set(exchange, **kwargs):
            called_with[exchange] = kwargs
        monkeypatch.setattr(admin_secrets, 'set_exchange_credentials', mock_set)

        client = admin_app.test_client()
        _login_session(client)
        rv = client.post(
            '/test-secret-prefix-32-chars-or-more/api/credentials/binance',
            json={'api_key': 'k1', 'passphrase': 'should-be-dropped'},
            headers={'X-Admin-CSRF': 'test-csrf-token'},
        )
        assert rv.status_code == 200
        body = rv.get_json() or {}
        assert body.get('ok') is True
        assert 'warning' in body, (
            "NF2-3：丢弃 passphrase 必须返回 warning 让用户知晓"
        )
        # passphrase 被剥离，不应传给 set_exchange_credentials
        assert 'passphrase' not in called_with.get('binance', {})
        assert called_with.get('binance', {}).get('api_key') == 'k1'


# ══════════════════════════════════════════════════════════════════
#  NF2-4: is_in_cooldown 处理本地时区 closed_at
# ══════════════════════════════════════════════════════════════════

class TestNF24CooldownLocalTimezone:
    """closed_at 写入本地时区 ISO 串时也要正确判 UTC 同日"""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        trades_path = str(tmp_path / "trades.json")
        monkeypatch.setattr(common, 'TRADES_FILE', trades_path)
        monkeypatch.setattr(risk_control, 'TRADES_FILE', trades_path)
        # 为 _resolve_account_id 提供稳定的活跃账户（避免拉到真实 admin_secrets）
        monkeypatch.setattr(risk_control, 'get_current_account_id', lambda: '')
        yield trades_path

    def _write_trade(self, trades_path, closed_at, pnl=-5.0):
        """写一笔已平仓亏损 trade"""
        trades = [{
            'symbol': 'PEPE/USDT', 'status': 'closed',
            'closed_at': closed_at,
            'close_type': 'tp2',  # 非止损 → 走"今日已亏损"分支
            'tp1_locked_pnl': 0.0, 'pnl': pnl,
            'account_id': '',
        }]
        common.atomic_write_json(trades_path, trades)

    def test_local_timezone_same_utc_day_blocks(self, _isolate):
        """closed_at 是本地时区串但映射到 UTC 是今天 → 应触发冷却"""
        trades_path = _isolate
        # 构造一个东 8 区时间戳，对应的 UTC 时间在今天
        now_utc = datetime.now(timezone.utc)
        # 当前 UTC 时间 + 8 小时（伪装成本地东 8 区时间），保留同一 UTC 日
        # 注意：必须保证 (now_utc - 1h) 仍在同一 UTC 日内，避开零点
        if now_utc.hour < 1:
            pytest.skip("UTC 接近零点，跳过避免误判")
        local_dt = (now_utc - timedelta(hours=1)).astimezone(
            timezone(timedelta(hours=8))
        )
        local_iso = local_dt.isoformat()  # e.g. "2026-05-17T05:30:00+08:00"
        # 预期：local_dt.astimezone(UTC).date() == now_utc.date()

        self._write_trade(trades_path, local_iso, pnl=-5.0)
        in_cd, reason = risk_control.is_in_cooldown('PEPE/USDT')
        assert in_cd is True, (
            f"NF2-4 回归：本地 +08:00 时间串映射到 UTC 仍是今天，应触发冷却。"
            f"closed_at={local_iso}, now_utc={now_utc.isoformat()}, reason={reason}"
        )

    def test_local_timezone_different_utc_day_does_not_block(self, _isolate):
        """closed_at 本地时区是今天，但 UTC 是昨天 → 不应触发冷却"""
        trades_path = _isolate
        # 构造 UTC 昨天 23:30 = 东 8 区今天 07:30
        now_utc = datetime.now(timezone.utc)
        # 用昨天 UTC 23:30，加 +08:00 偏移 → 本地"今天" 07:30
        utc_yesterday_late = now_utc.replace(hour=23, minute=30) - timedelta(days=1)
        local_dt = utc_yesterday_late.astimezone(timezone(timedelta(hours=8)))
        local_iso = local_dt.isoformat()

        self._write_trade(trades_path, local_iso, pnl=-5.0)
        in_cd, reason = risk_control.is_in_cooldown('PEPE/USDT')
        assert in_cd is False, (
            f"NF2-4 回归：UTC 昨天的亏损不应触发今天冷却。"
            f"closed_at={local_iso}, now_utc={now_utc.isoformat()}, reason={reason}"
        )

    def test_naive_datetime_treated_as_utc(self, _isolate):
        """无时区的 naive 时间串按 UTC 处理（parse_iso 行为），同日仍触发冷却"""
        trades_path = _isolate
        now_utc = datetime.now(timezone.utc)
        # 早 1 小时，不带时区
        if now_utc.hour < 1:
            pytest.skip("UTC 接近零点，跳过避免误判")
        naive_iso = (now_utc - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        self._write_trade(trades_path, naive_iso, pnl=-5.0)

        in_cd, reason = risk_control.is_in_cooldown('PEPE/USDT')
        assert in_cd is True, (
            f"naive 时间应按 UTC 处理；同日亏损必须触发冷却。"
            f"closed_at={naive_iso}"
        )


# ══════════════════════════════════════════════════════════════════
#  NF2-5: task_metrics 加锁
# ══════════════════════════════════════════════════════════════════

class TestNF25TaskMetricsLocked:
    """task_metrics.record 现在用 _locked_metrics 串行化"""

    def test_locked_metrics_context_exists(self):
        assert hasattr(task_metrics, '_locked_metrics')
        assert hasattr(task_metrics, '_METRICS_LOCK')

    def test_record_uses_locked_context(self):
        import inspect
        src = inspect.getsource(task_metrics.record)
        assert '_locked_metrics' in src, (
            "NF2-5 回归：record 必须用 _locked_metrics 包临界区"
        )

    def test_concurrent_record_does_not_lose_events(self, tmp_path):
        """
        N 个线程并发调用 record；最终行数 == 总写入次数。
        Linux 上 < PIPE_BUF 已经 POSIX 原子；Windows 上必须靠锁。
        修复后：任何平台都应保证完整。
        """
        path = str(tmp_path / "metrics.jsonl")
        N_THREADS = 8
        N_PER_THREAD = 50

        def writer(tid):
            for i in range(N_PER_THREAD):
                task_metrics.record({
                    'name': f'task_{tid}',
                    'mode': 'thread',
                    'status': 'ok',
                    'duration_sec': 0.1,
                }, _path=path)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
            assert not t.is_alive(), "线程超时"

        # 计行：每行应是合法 JSON
        events = task_metrics.read_all(_path=path)
        assert len(events) == N_THREADS * N_PER_THREAD, (
            f"NF2-5 回归：并发 record 丢失事件，"
            f"期望 {N_THREADS*N_PER_THREAD}，实际 {len(events)}"
        )
        # 每条事件都能解析（没有撕裂行）
        for ev in events:
            assert isinstance(ev, dict)
            assert 'name' in ev

    def test_record_swallows_exceptions(self, tmp_path):
        """record 应吞掉所有异常，不影响 caller"""
        # 路径不存在的目录 → open 会失败
        bad_path = str(tmp_path / "nonexistent_dir" / "metrics.jsonl")
        # 不应抛 — 仅 logger.warning
        task_metrics.record({'name': 'x', 'status': 'ok'}, _path=bad_path)
        # 重要：到达这里就算通过

    def test_truncate_still_works_inside_lock(self, tmp_path, monkeypatch):
        """加锁后 truncate 仍然按阈值触发"""
        path = str(tmp_path / "metrics.jsonl")
        # 调小阈值方便测试
        monkeypatch.setattr(task_metrics, '_TRUNCATE_KEEP', 10)
        monkeypatch.setattr(task_metrics, '_TRUNCATE_THRESHOLD_LINES', 20)
        monkeypatch.setattr(task_metrics, '_TRUNCATE_CHECK_BYTES', 100)

        for i in range(50):
            task_metrics.record({
                'name': f'task_{i}',
                'mode': 'thread',
                'status': 'ok',
                'duration_sec': 0.01,
            }, _path=path)

        events = task_metrics.read_all(_path=path)
        # 截断后保留最后 _TRUNCATE_KEEP 行（10），最近若干次写后再 truncate
        # 不要求精确，但绝对不应超过 _TRUNCATE_THRESHOLD_LINES
        assert len(events) <= 20, (
            f"truncate 应触发，实际行数 {len(events)} > 20"
        )
        assert len(events) > 0
