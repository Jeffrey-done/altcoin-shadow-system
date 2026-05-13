"""
迁移脚本测试：verify v4.0 → v4.1 TP1 双计数修复
"""

import pytest
from migrate_v41 import migrate_trades


def _v40_closed_trade_with_tp1(tp1_locked=30.0, total_true_pnl=10.0):
    """
    构造一条 v4.0 风格的 closed 交易：
      pnl 字段写的就是"合计总盈亏"（含 tp1_locked）
      同时保留 tp1_locked_pnl 字段
    """
    return {
        'id': f'TEST-{tp1_locked}-{total_true_pnl}',
        'symbol': 'PEPE/USDT',
        'direction': 'SHORT',
        'status': 'closed',
        'stake': 100,
        'leverage': 10,
        'tp1_triggered': True,
        'tp1_locked_pnl': tp1_locked,
        'pnl': total_true_pnl,          # v4.0: 合计
        'opened_at': '2025-01-01T00:00:00+00:00',
        'closed_at': '2025-01-01T12:00:00+00:00',
        'close_reason': '硬止损（价格反弹3%触发）',
    }


def _v40_closed_trade_no_tp1(pnl=-25.0):
    """v4.0 下没触发 TP1 的 closed 交易，pnl 就是真实盈亏"""
    return {
        'id': f'NOTP1-{pnl}',
        'symbol': 'DOGE/USDT',
        'direction': 'SHORT',
        'status': 'closed',
        'stake': 100,
        'leverage': 10,
        'tp1_triggered': False,
        'tp1_locked_pnl': 0,
        'pnl': pnl,
        'opened_at': '2025-01-01T00:00:00+00:00',
        'closed_at': '2025-01-01T06:00:00+00:00',
        'close_reason': '硬止损（价格反弹3%触发）',
    }


def _open_trade(tp1_triggered=False, tp1_locked=0, pnl=5):
    return {
        'id': 'OPEN-1',
        'symbol': 'SHIB/USDT',
        'direction': 'SHORT',
        'status': 'open',
        'stake': 100,
        'leverage': 10,
        'tp1_triggered': tp1_triggered,
        'tp1_locked_pnl': tp1_locked,
        'pnl': pnl,
        'opened_at': '2025-01-01T00:00:00+00:00',
    }


class TestMigrateClosedWithTp1:
    """Closed 交易 + TP1 已锁：应该把 pnl 减掉 tp1_locked_pnl"""

    def test_basic_subtraction(self):
        """
        v4.0 记录：tp1_locked=30, pnl=10（合计=10, 说明剩余仓位实亏20）
        v4.1 修正后：pnl 应该是 10 - 30 = -20（剩余仓位的实现盈亏）
        合计 = 30 + (-20) = 10，与 v4.0 pnl 字段一致 = 真实总盈亏
        """
        t = _v40_closed_trade_with_tp1(tp1_locked=30.0, total_true_pnl=10.0)
        migrated, stats = migrate_trades([t])

        assert stats['fixed'] == 1
        assert stats['skipped_open'] == 0
        assert stats['skipped_no_tp1'] == 0

        mt = migrated[0]
        assert mt['pnl'] == -20.0
        assert mt['tp1_locked_pnl'] == 30.0
        assert mt['_v41_migrated'] is True
        # 合计仍然是 10 U（真实总盈亏不变）
        assert mt['tp1_locked_pnl'] + mt['pnl'] == 10.0

    def test_tp1_then_profit(self):
        """TP2 场景：tp1_locked=30, 剩余平仓又赚50 → v4.0 pnl=80"""
        t = _v40_closed_trade_with_tp1(tp1_locked=30.0, total_true_pnl=80.0)
        migrated, _ = migrate_trades([t])
        mt = migrated[0]
        assert mt['pnl'] == 50.0
        assert mt['tp1_locked_pnl'] + mt['pnl'] == 80.0

    def test_tp1_then_flat(self):
        """保本止损：tp1_locked=30, 剩余 0 → v4.0 pnl=30"""
        t = _v40_closed_trade_with_tp1(tp1_locked=30.0, total_true_pnl=30.0)
        migrated, _ = migrate_trades([t])
        mt = migrated[0]
        assert mt['pnl'] == 0.0
        assert mt['tp1_locked_pnl'] + mt['pnl'] == 30.0


class TestSkipCases:
    """不该动的情况"""

    def test_skip_open_trades(self):
        """open 交易一律不动"""
        t = _open_trade(tp1_triggered=True, tp1_locked=25, pnl=5)
        migrated, stats = migrate_trades([t])
        assert stats['skipped_open'] == 1
        assert stats['fixed'] == 0
        assert migrated[0]['pnl'] == 5   # 未变
        assert '_v41_migrated' not in migrated[0]

    def test_skip_closed_without_tp1(self):
        """没触发 TP1 的 closed 交易：pnl 本就正确，不动"""
        t = _v40_closed_trade_no_tp1(pnl=-25.0)
        migrated, stats = migrate_trades([t])
        assert stats['skipped_no_tp1'] == 1
        assert stats['fixed'] == 0
        assert migrated[0]['pnl'] == -25.0

    def test_idempotent(self):
        """跑两次只改一次（幂等）"""
        t = _v40_closed_trade_with_tp1(tp1_locked=30.0, total_true_pnl=10.0)
        migrated1, stats1 = migrate_trades([t])
        assert stats1['fixed'] == 1
        assert migrated1[0]['pnl'] == -20.0

        # 第二次：已标记 _v41_migrated，跳过
        migrated2, stats2 = migrate_trades(migrated1)
        assert stats2['fixed'] == 0
        assert stats2['skipped_already_migrated'] == 1
        assert migrated2[0]['pnl'] == -20.0   # 没被再减

    def test_force_overrides_idempotent(self):
        """--force 会无视标记再减一次（危险模式）"""
        t = _v40_closed_trade_with_tp1(tp1_locked=30.0, total_true_pnl=10.0)
        migrated1, _ = migrate_trades([t])
        assert migrated1[0]['pnl'] == -20.0

        # force=True 再跑
        migrated2, stats = migrate_trades(migrated1, force=True)
        assert stats['fixed'] == 1
        assert migrated2[0]['pnl'] == -50.0   # -20 - 30（错了，这就是 force 危险之处）


class TestMixedBatch:
    """混合批次统计正确"""

    def test_aggregate_stats(self):
        trades = [
            _v40_closed_trade_with_tp1(30.0, 10.0),    # fix
            _v40_closed_trade_with_tp1(25.0, -5.0),    # fix
            _v40_closed_trade_no_tp1(-40.0),            # skip no_tp1
            _open_trade(),                               # skip open
            _open_trade(tp1_triggered=True, tp1_locked=15),  # skip open
        ]
        migrated, stats = migrate_trades(trades)

        assert stats['total'] == 5
        assert stats['fixed'] == 2
        assert stats['skipped_no_tp1'] == 1
        assert stats['skipped_open'] == 2
        assert stats['skipped_already_migrated'] == 0

        # 被改的两笔
        assert migrated[0]['pnl'] == -20.0
        assert migrated[1]['pnl'] == -30.0
        # 其它三笔原样
        assert migrated[2]['pnl'] == -40.0
        assert '_v41_migrated' not in migrated[2]
        assert '_v41_migrated' not in migrated[3]
        assert '_v41_migrated' not in migrated[4]


class TestPnlFieldSemantic:
    """
    关键语义验证：迁移后，所有聚合代码 `tp1_locked_pnl + pnl` 的结果
    等于用户心目中的真实总盈亏，与 v4.0 写入的 pnl 字段一致。
    """

    def test_aggregate_matches_v40_pnl(self):
        """迁移前 v4.0.pnl = 真实总盈亏；迁移后 tp1_locked + pnl = 真实总盈亏"""
        for tp1, total in [(30.0, 10.0), (25.0, -5.0), (50.0, 80.0), (10.0, 10.0)]:
            t = _v40_closed_trade_with_tp1(tp1_locked=tp1, total_true_pnl=total)
            migrated, _ = migrate_trades([t])
            mt = migrated[0]
            # 聚合公式 = v4.0 原始 pnl（真实总盈亏）
            assert mt['tp1_locked_pnl'] + mt['pnl'] == pytest.approx(total)
