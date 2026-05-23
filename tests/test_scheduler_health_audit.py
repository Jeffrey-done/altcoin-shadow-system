import inspect

import scheduler


def test_classify_task_failure_retryable():
    assert scheduler._classify_task_failure('network timeout') == 'retryable'
    assert scheduler._classify_task_failure('Too many requests') == 'retryable'


def test_classify_task_failure_non_retryable():
    assert scheduler._classify_task_failure('SyntaxError: invalid syntax') == 'non_retryable'


def test_health_audit_nonzero_must_raise_path_present():
    src = inspect.getsource(scheduler.main_loop)
    assert "health_audit.py', '--all', '--tg" in src
    assert "res.returncode >= 2" in src
    assert "raise RuntimeError" in src

def test_health_audit_streak_alert_path_present():
    src = inspect.getsource(scheduler.main_loop)
    assert "_health_audit_fail_streak" in src
    assert ">= 3" in src
    assert "健康审计连续失败告警" in src
    assert "_health_audit_last_alert_ts" in src
    assert ">= 1800" in src
