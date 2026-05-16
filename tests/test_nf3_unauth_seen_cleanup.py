"""
NF-3 回归测试：tg_bot._unauth_seen 周期清理

修复点：M-5 引入的 ``_unauth_seen`` dict 记录每个未授权 chat_id 的命中次数与
首次出现时间，但条目永不删除。bot token 泄露后被加入数千群组时 dict 会无限
增长。``_prune_unauth_seen(d, now, max_age)`` 把超期条目过滤掉，``run_bot``
主循环每 ``_UNAUTH_PRUNE_INTERVAL`` 次轮询调用一次。

测试只覆盖纯函数 ``_prune_unauth_seen``（注入 ``now`` 时间戳，避免依赖真实时钟），
不启动整个 ``run_bot`` 阻塞循环。
"""

import pytest

# conftest 已经 mock 了 ccxt
import tg_bot


class TestNF3PruneUnauthSeen:
    """_prune_unauth_seen 的纯函数行为"""

    def test_drops_entries_older_than_max_age(self):
        """超过 max_age 的条目必须被丢弃"""
        d = {
            'old_chat': (5, 1000.0),       # age = 9000，超期
            'fresh_chat': (3, 9500.0),     # age = 500，保留
        }
        result = tg_bot._prune_unauth_seen(d, now=10000.0, max_age=3600.0)
        assert 'old_chat' not in result
        assert 'fresh_chat' in result
        assert result['fresh_chat'] == (3, 9500.0)

    def test_at_exact_boundary_is_dropped(self):
        """now - ts == max_age 视为过期（严格小于才保留），避免边界条目堆积"""
        d = {'edge_chat': (1, 0.0)}
        # age = 3600 == max_age → 过滤掉
        result = tg_bot._prune_unauth_seen(d, now=3600.0, max_age=3600.0)
        assert result == {}

    def test_just_under_boundary_is_kept(self):
        """now - ts < max_age 必须保留"""
        d = {'edge_chat': (1, 0.001)}  # age ≈ 3599.999
        result = tg_bot._prune_unauth_seen(d, now=3600.0, max_age=3600.0)
        assert 'edge_chat' in result

    def test_empty_dict_returns_empty(self):
        """空输入不应崩，返回空 dict"""
        assert tg_bot._prune_unauth_seen({}, now=999.0, max_age=10.0) == {}

    def test_all_fresh_returns_unchanged_data(self):
        """全部新鲜条目时返回内容等价的 dict"""
        d = {
            'a': (1, 100.0),
            'b': (2, 100.5),
            'c': (3, 101.0),
        }
        result = tg_bot._prune_unauth_seen(d, now=120.0, max_age=3600.0)
        assert result == d

    def test_returns_new_dict_not_mutating_input(self):
        """必须返回新 dict —— 不修改原对象，避免 run_bot 中遗留旧引用"""
        d = {'old': (1, 0.0), 'new': (1, 1000.0)}
        snapshot = dict(d)
        result = tg_bot._prune_unauth_seen(d, now=1100.0, max_age=500.0)
        assert d == snapshot, "_prune_unauth_seen 不应修改入参"
        assert result is not d

    def test_simulated_long_running_bot_dict_stays_bounded(self):
        """
        集成层模拟：1000 个不同 chat_id 在 2 小时内被记录，prune 后只剩
        最近 1 小时内的条目。验证 NF-3 主张的"无限增长"问题已被消除。
        """
        d = {}
        # 0~3600s 期间出现的 600 个 chat_id
        for i in range(600):
            d[f"old_{i}"] = (1, float(i * 6))   # ts in [0, 3594]
        # 3600~7200s 期间出现的 400 个新 chat_id
        for i in range(400):
            d[f"new_{i}"] = (1, 3600.0 + float(i * 9))  # ts in [3600, 7191]
        # 当前时间 7200s，max_age=3600s（保留最近 1 小时）
        result = tg_bot._prune_unauth_seen(d, now=7200.0, max_age=3600.0)
        # old_* 全部 age >= 3606，应该全删
        assert not any(k.startswith('old_') for k in result), \
            "超过 max_age 的旧条目仍残留，dict 仍会无限增长"
        # new_0 的 age = 3600 → 边界过期；new_1+ age <= 3591 → 保留
        assert 'new_0' not in result
        assert 'new_399' in result


class TestNF3RunBotConstantsExposed:
    """轻量保护：run_bot 内的清理常量与函数引用必须存在（防回归）"""

    def test_prune_helper_exists_at_module_level(self):
        assert callable(getattr(tg_bot, '_prune_unauth_seen', None)), \
            "_prune_unauth_seen 必须在模块顶层暴露，方便单元测试"

    def test_run_bot_loop_uses_prune_helper(self):
        """静态扫描 run_bot 源码，确认确实调用了 _prune_unauth_seen"""
        import inspect
        src = inspect.getsource(tg_bot.run_bot)
        assert '_prune_unauth_seen' in src, (
            "NF-3 回归：run_bot 主循环不再调用 _prune_unauth_seen，"
            "_unauth_seen 会重新陷入无限增长"
        )
        # 也确认有节流常量，避免每次循环都全量扫描 dict
        assert '_UNAUTH_PRUNE_INTERVAL' in src
        assert '_UNAUTH_PRUNE_MAX_AGE' in src
