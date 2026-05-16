"""
NF-1 回归测试：Windows fcntl shim byte-range 锁修复

修复点：`common._LockShim.flock` / `admin_secrets._LockShim.flock` 在调用
`msvcrt.locking` 之前 `os.lseek(fd, 0, SEEK_SET)`，强制不同进程都锁住
固定的 byte 0；否则 'a' 模式下不同 EOF 位置会让 byte-range 锁互不冲突，
互斥语义失效（详见 docs/AUDIT_FINAL_REPORT_2026_05.md NF-1）。

测试策略：
  * Windows-only —— 非 Windows 直接 skip（Linux/Mac 用原生 fcntl.flock）
  * 通过 monkeypatch `msvcrt.locking` 拦截调用，断言调用瞬间 fd 位置为 0
  * 同时覆盖 LOCK_EX、LOCK_UN、LockedJsonFile 集成路径
  * `admin_secrets._LockShim` 同样验证（两份重复实现，NF-5 待重构）
"""

import json
import os
import sys

import pytest

# conftest 已经 mock 了 ccxt
import common


WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != 'win32',
    reason="NF-1 仅修复 Windows fcntl shim；Linux/Mac 用原生 fcntl.flock"
)


# ══════════════════════════════════════════════════════════════════
#  common._LockShim 直接测试
# ══════════════════════════════════════════════════════════════════

@WINDOWS_ONLY
class TestNF1CommonShimLseek:
    """直接验证 common._LockShim 在调用 msvcrt.locking 前会 lseek 到 byte 0"""

    def test_lock_ex_seeks_to_zero_before_msvcrt_locking(self, tmp_path, monkeypatch):
        """
        关键回归：之前 'a' 模式下 fp 在 EOF，msvcrt.locking 会锁 byte EOF；
        不同进程 EOF 位置可能不同 → 锁不冲突 → 互斥失效。
        修复后必须锁 byte 0。
        """
        import msvcrt as real_msvcrt
        positions_at_lock = []

        def fake_locking(fd, mode, nbytes):
            # 记录 msvcrt.locking 被调用瞬间，fd 的当前位置
            positions_at_lock.append((mode, os.lseek(fd, 0, os.SEEK_CUR)))

        monkeypatch.setattr(real_msvcrt, 'locking', fake_locking)

        # 准备一个已有数据的 .lock 文件，模拟"老的 lock 文件已经长大"的场景
        # 这是 NF-1 描述的边界场景：'a' 模式打开后 fp 在 byte 200
        lock_path = tmp_path / "victim.lock"
        lock_path.write_bytes(b"X" * 200)

        with open(lock_path, 'a') as fd:
            common._LockShim.flock(fd, common._LockShim.LOCK_EX)

        assert len(positions_at_lock) == 1
        mode, pos = positions_at_lock[0]
        assert pos == 0, (
            f"NF-1 回归失败：msvcrt.locking 调用瞬间 fd 必须在 byte 0，实际 pos={pos}。"
            f"未 lseek 会导致不同进程在不同 EOF 位置加锁，互斥失效。"
        )
        assert mode == real_msvcrt.LK_LOCK

    def test_lock_un_also_seeks_to_zero(self, tmp_path, monkeypatch):
        """解锁路径同样必须先 lseek(0)，否则解锁的不是加锁的那个 byte"""
        import msvcrt as real_msvcrt
        positions_at_unlock = []

        def fake_locking(fd, mode, nbytes):
            positions_at_unlock.append((mode, os.lseek(fd, 0, os.SEEK_CUR)))

        monkeypatch.setattr(real_msvcrt, 'locking', fake_locking)

        lock_path = tmp_path / "victim.lock"
        lock_path.write_bytes(b"Y" * 100)

        with open(lock_path, 'a') as fd:
            common._LockShim.flock(fd, common._LockShim.LOCK_UN)

        assert len(positions_at_unlock) == 1
        mode, pos = positions_at_unlock[0]
        assert pos == 0, f"NF-1: 解锁瞬间 fd 必须在 byte 0，实际 pos={pos}"
        assert mode == real_msvcrt.LK_UNLCK

    def test_flock_accepts_int_fileno(self, tmp_path):
        """传入整数 fd（非 file-like）也必须正常工作 —— 不 crash"""
        lock_path = tmp_path / "victim.lock"
        lock_path.write_bytes(b"")
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            # 不应抛异常
            common._LockShim.flock(fd, common._LockShim.LOCK_EX)
            common._LockShim.flock(fd, common._LockShim.LOCK_UN)
        finally:
            os.close(fd)

    def test_flock_swallows_lseek_oserror(self, tmp_path, monkeypatch):
        """fd 不可 seek（pipe/socket）时 lseek 抛 OSError，必须被吞掉，仍走到 msvcrt.locking"""
        import msvcrt as real_msvcrt
        locking_calls = []
        monkeypatch.setattr(
            real_msvcrt, 'locking',
            lambda *args, **kwargs: locking_calls.append(args)
        )

        def failing_lseek(fd, offset, whence):
            raise OSError(29, "Illegal seek")

        monkeypatch.setattr(os, 'lseek', failing_lseek)

        lock_path = tmp_path / "victim.lock"
        lock_path.touch()
        with open(lock_path, 'a') as fd:
            # lseek 失败也不应该让 flock 崩 —— 退化为旧行为
            common._LockShim.flock(fd, common._LockShim.LOCK_EX)

        assert len(locking_calls) == 1, "lseek 失败时仍要尝试加锁"


# ══════════════════════════════════════════════════════════════════
#  LockedJsonFile 集成路径
# ══════════════════════════════════════════════════════════════════

@WINDOWS_ONLY
class TestNF1LockedJsonFileIntegration:
    """LockedJsonFile 是 _LockShim 的最大消费者，必须验证集成路径"""

    def test_locks_byte_zero_even_when_lockfile_preexists_with_data(
        self, tmp_path, monkeypatch
    ):
        """
        模拟运行多次后 .lock 文件已经累积了内容（例如有人误写、或者旧版本曾经写过日志）。
        新代码进入 LockedJsonFile 时，fd 在 'a' 模式下原本会停在 EOF，
        NF-1 修复必须把它强制 seek 回 byte 0。
        """
        import msvcrt as real_msvcrt
        positions_during_locking = []
        original_locking = real_msvcrt.locking

        def recording_locking(fd, mode, nbytes):
            positions_during_locking.append((mode, os.lseek(fd, 0, os.SEEK_CUR)))
            return original_locking(fd, mode, nbytes)

        monkeypatch.setattr(real_msvcrt, 'locking', recording_locking)

        target = tmp_path / "trades.json"
        target.write_text("[]", encoding='utf-8')

        # 关键：让 .lock 文件已经有 500 字节的内容
        # 这是 NF-1 修复前会导致互斥失效的精确条件
        lock_path = str(target) + '.lock'
        with open(lock_path, 'w') as f:
            f.write("STALE-CONTENT-" * 40)  # ≈ 560 bytes

        # 走完整 enter/exit
        with common.LockedJsonFile(str(target), default=[]) as (data, save):
            assert data == []
            save([{"symbol": "PEPE/USDT", "ok": 1}])

        # 入口 LOCK_EX + 出口 LOCK_UN，至少 2 次 msvcrt.locking 调用
        assert len(positions_during_locking) >= 2, (
            f"应至少有加锁+解锁 2 次调用，实际 {len(positions_during_locking)}"
        )
        for mode, pos in positions_during_locking:
            assert pos == 0, (
                f"NF-1 回归：lockfile 已有数据时，msvcrt.locking(mode={mode}) "
                f"瞬间 fd 必须在 byte 0，实际 pos={pos}。否则不同进程会锁不同 byte，"
                f"互斥失效。"
            )

        # 业务行为也应正常：写入生效
        final = json.loads(target.read_text(encoding='utf-8'))
        assert final == [{"symbol": "PEPE/USDT", "ok": 1}]

    def test_repeated_enter_exit_still_locks_byte_zero(self, tmp_path, monkeypatch):
        """重复进入退出也得保持锁 byte 0 —— 防止某次退出后 fd 状态污染下次"""
        import msvcrt as real_msvcrt
        positions_during_locking = []
        original_locking = real_msvcrt.locking

        def recording_locking(fd, mode, nbytes):
            positions_during_locking.append((mode, os.lseek(fd, 0, os.SEEK_CUR)))
            return original_locking(fd, mode, nbytes)

        monkeypatch.setattr(real_msvcrt, 'locking', recording_locking)

        target = tmp_path / "state.json"
        target.write_text(json.dumps({"count": 0}), encoding='utf-8')

        for i in range(5):
            with common.LockedJsonFile(str(target), default={"count": 0}) as (data, save):
                data["count"] += 1
                save(data)

        assert json.loads(target.read_text(encoding='utf-8')) == {"count": 5}
        # 5 次循环，每次 enter+exit ≥ 2 次 locking 调用
        assert len(positions_during_locking) >= 10
        for mode, pos in positions_during_locking:
            assert pos == 0, f"循环中 fd 位置漂移到 {pos}，NF-1 回归"


# ══════════════════════════════════════════════════════════════════
#  NF-5: admin_secrets 不再有自己的 _LockShim，直接共用 common.fcntl
# ══════════════════════════════════════════════════════════════════

class TestNF5SharedFcntlShim:
    """NF-5: fcntl shim 在 common 与 admin_secrets 不再重复 —— 单一真源"""

    def test_admin_secrets_fcntl_is_common_fcntl(self):
        """两个模块的 fcntl 必须是同一个对象，避免 NF-1 类修复需要改两处"""
        import admin_secrets
        import common
        assert admin_secrets.fcntl is common.fcntl, (
            "NF-5 回归：admin_secrets.fcntl 必须复用 common.fcntl，否则同一类修复"
            "（如 NF-1 lseek(0)）需要在两个文件分别维护，容易遗漏。"
        )

    def test_admin_secrets_has_no_local_lockshim_class(self):
        """确认 admin_secrets 不再定义本地 _LockShim 类（去重彻底完成）"""
        import admin_secrets
        # 顶层 dict 不应有 _LockShim；fcntl 是从 common 导入的，不是这里定义的类
        assert '_LockShim' not in vars(admin_secrets), (
            "NF-5 回归：admin_secrets 仍有本地 _LockShim 定义，未完全去重"
        )

    @WINDOWS_ONLY
    def test_admin_secrets_fcntl_inherits_nf1_lseek_fix(self, tmp_path, monkeypatch):
        """通过 admin_secrets.fcntl 调用也应该走到 common 的 lseek(0) 修复"""
        import admin_secrets
        import msvcrt as real_msvcrt
        positions = []

        def fake_locking(fd, mode, nbytes):
            positions.append((mode, os.lseek(fd, 0, os.SEEK_CUR)))

        monkeypatch.setattr(real_msvcrt, 'locking', fake_locking)

        lock_path = tmp_path / "admin.lock"
        lock_path.write_bytes(b"Z" * 300)
        with open(lock_path, 'a') as fd:
            admin_secrets.fcntl.flock(fd, admin_secrets.fcntl.LOCK_EX)
            admin_secrets.fcntl.flock(fd, admin_secrets.fcntl.LOCK_UN)

        assert len(positions) == 2
        for mode, pos in positions:
            assert pos == 0, (
                f"通过 admin_secrets.fcntl 锁的 byte 不是 0 (pos={pos}, mode={mode})，"
                f"NF-5 切换到共享 shim 后 NF-1 修复应该自动生效"
            )
