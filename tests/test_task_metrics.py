"""
task_metrics + diagnose_timeout 单元测试

策略:
  * 用 tmp_path 隔离 task_metrics.jsonl 文件 —— 通过 _path 注入参数
  * 验证写入/读取/截断/汇总 4 类逻辑
  * diagnose_timeout 引擎层(render_timeline / render_summary / main 退出码)
"""

import json
import os
import time

import pytest

# conftest 已经 mock 了 ccxt
import task_metrics
import diagnose_timeout


@pytest.fixture
def metrics_path(tmp_path):
    """每个测试一个独立 jsonl 文件"""
    return str(tmp_path / "task_metrics.jsonl")


# ══════════════════════════════════════════════════════════════════
#  task_metrics.record
# ══════════════════════════════════════════════════════════════════

class TestRecord:
    def test_writes_jsonl_line(self, metrics_path):
        task_metrics.record({"name": "X", "status": "ok",
                             "duration_sec": 1.5, "timeout_sec": 60},
                            _path=metrics_path)
        with open(metrics_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        ev = json.loads(lines[0])
        assert ev["name"] == "X" and ev["status"] == "ok"
        assert ev["duration_sec"] == 1.5
        assert "ts" in ev  # 自动补上

    def test_appends_multiple(self, metrics_path):
        for i in range(5):
            task_metrics.record({"name": "X", "status": "ok",
                                 "duration_sec": float(i), "timeout_sec": 60},
                                _path=metrics_path)
        with open(metrics_path) as f:
            assert sum(1 for _ in f) == 5

    def test_record_failure_swallowed(self, monkeypatch, metrics_path):
        """文件不可写时不应抛 —— metrics 不能影响主流程"""
        # 让 open 抛异常
        original_open = open

        def boom_open(path, *args, **kw):
            if path == metrics_path:
                raise OSError("disk full")
            return original_open(path, *args, **kw)

        monkeypatch.setattr("builtins.open", boom_open)
        # 不应抛
        task_metrics.record({"name": "X", "status": "ok",
                             "duration_sec": 1, "timeout_sec": 60},
                            _path=metrics_path)
        # 文件不存在
        assert not os.path.exists(metrics_path)


class TestTruncate:
    def test_truncate_bounds_file_growth(self, metrics_path, monkeypatch):
        """
        合同测试：连续写大量条目时,文件不应无限增长。
        我们临时把行数阈值调小,让 truncate 在更少条目下触发,
        避免 100KB 字节阈值导致测试时间过长。
        """
        monkeypatch.setattr(task_metrics, "_TRUNCATE_THRESHOLD_LINES", 50)
        monkeypatch.setattr(task_metrics, "_TRUNCATE_KEEP", 20)
        monkeypatch.setattr(task_metrics, "_TRUNCATE_CHECK_BYTES", 100)  # 跨出去就检查

        # 写远超阈值的条目
        for i in range(200):
            task_metrics.record({"name": "X", "status": "ok",
                                 "duration_sec": float(i), "timeout_sec": 60},
                                _path=metrics_path)

        # 文件应该被截断到接近 _TRUNCATE_KEEP 大小（不会超过阈值很多）
        with open(metrics_path) as f:
            count = sum(1 for _ in f)
        assert count <= task_metrics._TRUNCATE_THRESHOLD_LINES + 10, (
            f"truncate 应让文件保持有界,实际 {count} 行已超过阈值 "
            f"{task_metrics._TRUNCATE_THRESHOLD_LINES}+10"
        )

        # 业务语义：truncate 后保留的是最新的条目
        events = task_metrics.read_recent(_path=metrics_path, limit=5)
        durations = [e["duration_sec"] for e in events]
        assert durations[-1] == 199.0, "最新条目应为 i=199"


# ══════════════════════════════════════════════════════════════════
#  task_metrics.read_recent / read_all
# ══════════════════════════════════════════════════════════════════

class TestRead:
    def _seed(self, path):
        now = time.time()
        for i in range(5):
            task_metrics.record({
                "ts": now - (5 - i),
                "name": "candidates" if i % 2 == 0 else "tracker",
                "status": "timeout" if i == 4 else "ok",
                "duration_sec": float(i + 1) * 100,
                "timeout_sec": 600,
                "exitcode": 0 if i != 4 else -15,
                "pid": 1000 + i,
            }, _path=path)

    def test_read_all_sorted_by_ts(self, metrics_path):
        self._seed(metrics_path)
        events = task_metrics.read_all(_path=metrics_path)
        assert len(events) == 5
        tss = [e["ts"] for e in events]
        assert tss == sorted(tss)

    def test_read_recent_limit(self, metrics_path):
        self._seed(metrics_path)
        events = task_metrics.read_recent(limit=2, _path=metrics_path)
        assert len(events) == 2
        # 取的是最新 2 条
        assert events[-1]["status"] == "timeout"

    def test_read_recent_filter_name(self, metrics_path):
        self._seed(metrics_path)
        events = task_metrics.read_recent(name="tracker", _path=metrics_path)
        assert len(events) == 2
        assert all(e["name"] == "tracker" for e in events)

    def test_read_handles_corrupt_lines(self, metrics_path):
        with open(metrics_path, "w") as f:
            f.write('{"name":"good","status":"ok"}\n')
            f.write('this is not json\n')
            f.write('{"name":"good2","status":"ok"}\n')
        events = task_metrics.read_all(_path=metrics_path)
        assert len(events) == 2

    def test_read_returns_empty_when_no_file(self, tmp_path):
        absent = str(tmp_path / "absent.jsonl")
        assert task_metrics.read_recent(_path=absent) == []
        assert task_metrics.read_all(_path=absent) == []


class TestSummary:
    def test_summary_aggregates_per_task(self, metrics_path):
        events = [
            {"name": "X", "status": "ok",      "duration_sec": 10, "timeout_sec": 60},
            {"name": "X", "status": "ok",      "duration_sec": 30, "timeout_sec": 60},
            {"name": "X", "status": "timeout", "duration_sec": 60, "timeout_sec": 60},
            {"name": "Y", "status": "ok",      "duration_sec": 5,  "timeout_sec": 60},
        ]
        for ev in events:
            task_metrics.record(ev, _path=metrics_path)
        s = task_metrics.summary_by_task(_path=metrics_path)
        assert s["X"]["total"] == 3
        assert s["X"]["ok"] == 2 and s["X"]["timeout"] == 1
        assert s["X"]["avg_duration_sec"] == pytest.approx((10 + 30 + 60) / 3, rel=0.01)
        assert s["X"]["max_duration_sec"] == 60
        assert s["X"]["last_status"] == "timeout"
        assert s["Y"]["total"] == 1


# ══════════════════════════════════════════════════════════════════
#  diagnose_timeout 引擎
# ══════════════════════════════════════════════════════════════════

class TestRenderTimeline:
    def test_empty_returns_friendly_msg(self):
        out = diagnose_timeout.render_timeline([], use_color=False)
        assert "暂无任务执行事件" in out

    def test_renders_header_and_rows(self):
        events = [
            {"ts": 1737000000, "name": "X", "status": "ok",
             "duration_sec": 5, "timeout_sec": 60, "mode": "thread", "exitcode": None},
            {"ts": 1737000060, "name": "X", "status": "timeout",
             "duration_sec": 60, "timeout_sec": 60, "mode": "process", "exitcode": -15,
             "error": "exceeded 60s"},
        ]
        out = diagnose_timeout.render_timeline(events, use_color=False)
        assert "时间(UTC)" in out
        assert "X" in out
        assert "1m00.0s" in out  # 60 秒 = 1m00.0s（边界值落入分钟分支）
        assert "100% 接近超时" in out  # timeout 行
        assert "exceeded 60s" in out

    def test_unicode_glyphs_fallback_to_ascii(self, monkeypatch):
        """stdout encoding 不能编码 ✓ 时,返回 ASCII glyph 字典"""
        class _FakeStdout:
            encoding = "gbk"
        monkeypatch.setattr(diagnose_timeout.sys, "stdout", _FakeStdout())
        glyphs = diagnose_timeout._glyphs_for_stdout()
        assert glyphs == diagnose_timeout._STATUS_GLYPH_ASCII

    def test_unicode_glyphs_used_when_supported(self, monkeypatch):
        class _FakeStdout:
            encoding = "utf-8"
        monkeypatch.setattr(diagnose_timeout.sys, "stdout", _FakeStdout())
        glyphs = diagnose_timeout._glyphs_for_stdout()
        assert glyphs == diagnose_timeout._STATUS_GLYPH


class TestRenderSummary:
    def test_aggregates_correctly(self):
        events = [
            {"ts": 1, "name": "X", "status": "ok",      "duration_sec": 10},
            {"ts": 2, "name": "X", "status": "timeout", "duration_sec": 60},
            {"ts": 3, "name": "Y", "status": "ok",      "duration_sec": 5},
        ]
        out = diagnose_timeout.render_summary(events, use_color=False)
        assert "按任务聚合" in out
        assert "X" in out and "Y" in out

    def test_empty_summary_returns_blank(self):
        assert diagnose_timeout.render_summary([], use_color=False) == ""


class TestFormatters:
    def test_fmt_duration(self):
        assert diagnose_timeout._fmt_duration(None) == "—"
        assert diagnose_timeout._fmt_duration(5) == "5.0s"
        assert diagnose_timeout._fmt_duration(65) == "1m05.0s"
        assert diagnose_timeout._fmt_duration(601.2) == "10m01.2s"
        assert diagnose_timeout._fmt_duration("garbage") == "?"

    def test_fmt_ts(self):
        out = diagnose_timeout._fmt_ts(1737000000)
        assert "2025" in out or "2026" in out  # year
        assert ":" in out


class TestCLI:
    def _isolate_metrics(self, tmp_path, monkeypatch):
        """让 task_metrics.METRICS_FILE 指向 tmp，避免污染仓库"""
        path = str(tmp_path / "task_metrics.jsonl")
        monkeypatch.setattr(task_metrics, "METRICS_FILE", path)
        return path

    def test_main_returns_0_when_no_events(self, tmp_path, monkeypatch, capsys):
        self._isolate_metrics(tmp_path, monkeypatch)
        rc = diagnose_timeout.main(["--no-color"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "暂无任务执行事件" in captured.out

    def test_main_returns_2_on_timeout(self, tmp_path, monkeypatch, capsys):
        path = self._isolate_metrics(tmp_path, monkeypatch)
        task_metrics.record({"name": "X", "status": "timeout",
                             "duration_sec": 600, "timeout_sec": 600},
                            _path=path)
        rc = diagnose_timeout.main(["--no-color"])
        assert rc == 2

    def test_main_json_mode(self, tmp_path, monkeypatch, capsys):
        path = self._isolate_metrics(tmp_path, monkeypatch)
        task_metrics.record({"name": "X", "status": "ok",
                             "duration_sec": 1, "timeout_sec": 60},
                            _path=path)
        rc = diagnose_timeout.main(["--json"])
        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)
        assert "events" in payload and len(payload["events"]) == 1
        assert "summary" in payload

    def test_main_filter_by_name(self, tmp_path, monkeypatch, capsys):
        path = self._isolate_metrics(tmp_path, monkeypatch)
        task_metrics.record({"name": "X", "status": "ok",
                             "duration_sec": 1, "timeout_sec": 60},
                            _path=path)
        task_metrics.record({"name": "Y", "status": "ok",
                             "duration_sec": 1, "timeout_sec": 60},
                            _path=path)
        rc = diagnose_timeout.main(["--name", "X", "--json"])
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert len(payload["events"]) == 1
        assert payload["events"][0]["name"] == "X"
        assert rc == 0
