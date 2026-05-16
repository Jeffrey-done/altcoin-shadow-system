# 最终验证审计报告

**审计对象**：`altcoin-shadow-system` (HEAD `76efb76`)  
**审计日期**：2026-05-16  
**审计基线**：233/233 测试通过（含 73 个针对修复的回归测试）  
**审计方式**：独立批判视角 — 不预设任何修复有效，逐行核查实际行为  
**关联 PR**：#58 (batch-1) → #59 (batch-2) → #60 (M-3 event-driven)

---

## 一、总结

整体评估：**系统健康度由 B+ 提升至 A-，可以进入"小仓位实盘验证"阶段**，但**不建议直接全仓上线**。

- 原始 16 项问题：**14 项确认彻底解决，2 项虽然修复但有边界遗留**
- 在修复过程中暴露/引入的**次生问题**：识别出 5 项（其中 1 项中等、4 项低危）
- 测试覆盖：从原始 ~50 个测试增长到 233 个，新增的 73 个用例直接锁定本次修复的逻辑断点
- 模块间一致性：实盘 vs 回测 vs 风控的核心口径已经对齐（M-2 余额基准、M-3 复利/BTC过滤、M-7 冷却作用域），过去多份"半实现"被收敛
- 性能：无明显劣化；M-3 事件驱动引擎的额外开销（每 bar O(open_trades)）可忽略，因 open_trades 通常 ≤ 3

**实盘准入建议**：
1. ✅ 可在 testnet（Binance Futures Testnet）跑 1~2 周影子交易做最终校验
2. ⚠️ 上线前需修复 NF-1（fcntl Windows shim）— 仅当用户在 Windows 本地做并发测试时才会发作
3. ⚠️ 上线前需补一份"反向代理 access log 过滤" runbook（见 NF-2）

---

## 二、原始问题状态确认

| 编号 | 严重 | 状态 | 验证方式 | 结论 |
|------|------|------|----------|------|
| H-1 | 高 | ✅ 已解决 | 读 `common.send_tg`：HTML 解析失败 → plain-text 自动重发；递归保护正确（第二次调用 `html=False` 不再降级，无死循环） | 完全解决 |
| M-1 | 中 | ✅ 已解决 | 读 TP1 触发路径 `altcoin_tracker.evaluate_trade` → `pending_risk_partial` → `release_partial_stake`；新增 `risk_control.release_partial_stake` 仅减 `total_open_stake`，不动 `daily_loss/consecutive_losses`；TP1+TP2 闭环数学：100−50−50=0 ✓ | 完全解决 |
| M-2 | 中 | ✅ 已解决 | `config.FUNDING_MIN=-0.03` 已生效；`scan_daily` 在写候选池前直接 `continue`；signal_score 仍保留 -5 扣分作为二次防御 | 完全解决 |
| M-3 | 中 | ✅ 已解决 | PR #60 完整重写为事件驱动主循环；与 legacy 引擎的对照测试 `test_aligned_when_dynamic_features_disabled` 验证关闭所有动态特性时数值精确到 0.01U 一致 | 完全解决 |
| M-4 | 中 | ✅ 已解决 | `config.SLIPPAGE_ALERT_PCT = 1.0` 已生效；live_executor 直接读取 | 完全解决 |
| M-5 | 中 | ⚠️ 部分解决 | 限速逻辑生效，但 `_unauth_seen` dict 永不清理过期 chat_id（见 NF-3） | 主要功能解决，存在轻微边界 |
| M-6 | 中 | ✅ 已解决 | `DEFAULT_STAKE = 30`；scheduler 启动会调 `validate_cross_field_consistency` 并 TG 告警；admin panel 写入会预校验 | 完全解决 |
| M-7 | 中 | ✅ 已解决 | `config.COOLDOWN_SCOPE='global'`；`_evaluate_candidate` 仅在 global 模式做全局检查；`_open_position_for_candidate` per-account 路径加 `and acc_id` 守卫；`is_in_cooldown('', None)` 语义统一 | 完全解决 |
| M-8 | 中 | ✅ 已解决 | `/setup` GET + POST 双端点都校验 `.admin_setup_token` 文件存在；setup 成功后 `os.unlink` 删除；README 已更新 | 完全解决 |
| L-1 | 低 | ⚠️ 部分解决 | fcntl shim 让 Windows 测试可跑；但 Windows shim 的 byte-range lock 语义在某些情况下可能失效（见 NF-1） | 单进程测试解决，多进程隐患仍在 |
| L-2 | 低 | ✅ 已解决 | README 已改 RSI ≥75 + 注脚以 config 为准 | 完全解决 |
| L-3 | 低 | ✅ 已解决 | `signal_score.calculate_signal_score` 注释已说明 `stake` 字段非真实使用 | 完全解决（注释而非删字段，保持向后兼容） |
| L-4 | 低 | ✅ 已解决 | `_record_failure / _clear_ip_failures` 已切到 `LockedJsonFile` 原子 RMW | 完全解决 |
| L-5 | 低 | ✅ 已解决 | `_bonus_first = (BONUS+1)//2; _bonus_second = BONUS - _bonus_first`；测试用例验证总和守恒 | 完全解决 |
| L-6 | 低 | ✅ 已解决 | `/api/events` mtime 缓存已生效，trades.json 不变时直接返回缓存 | 完全解决 |
| L-7 | 低 | ✅ 已解决 | `evaluate_trade` 入口 `if entry <= 0 or current_price <= 0: return EvalResult(0,0)` 已加 | 完全解决 |

**Round 2 用户后续要求**：

| 项 | 状态 | 验证 |
|----|------|------|
| `is_in_cooldown` 语义统一 | ✅ | `if account_id:` 守卫，`None` 与 `''` 语义统一为全扫；测试覆盖 |
| Dashboard 启动日志脱敏 | ✅ | 静态扫描确认无 length print；保留 `Admin Panel 已启用 ✓` |
| 配置一致性硬阻塞 | ✅ | `validate_cross_field_consistency` 返回 `(errors, warnings)` 元组；admin panel 预校验返回 400；`save_overrides` 二次防御抛 ValueError |

---

## 三、新发现问题列表

### 🟡 NF-1 [中 · 健壮性] Windows fcntl shim 的 byte-range lock 在多进程并发下可能失效

**位置**：`common.py:_LockShim`、`admin_secrets.py:_LockShim`

**问题**：Windows 实现用 `msvcrt.locking(fileno, LK_LOCK, 1)`，这是**当前文件指针位置开始的 1 字节**字节范围锁，而 Linux `fcntl.flock` 是**整文件锁**。

`LockedJsonFile.__enter__` 用：
```python
self.lock_fd = open(self.lockfile, 'a')      # 'a' 模式 → 文件指针在末尾
fcntl.flock(self.lock_fd, fcntl.LOCK_EX)
```

在 Windows 下：
- 文件已有内容时，进程 A 在末尾位置（如 byte 100）锁 1 字节，进程 B 在末尾位置（如 byte 200，因为 A 写过数据让文件长大了）锁 1 字节 → **互不冲突**！
- 进程之间的锁不再互斥 → 风控状态、TG journal、admin secrets 文件理论上可能撕裂

**影响**：
- 生产是 Docker (Linux fcntl) 不受影响 ✓
- Windows 本地测试 pytest 单进程通过（无并发）→ 测试无法暴露
- 任何 Windows 用户做手动多进程压测会看到状态不一致

**严重程度**：中（仅影响 Windows，且生产不用）

**修复建议**：
```python
@staticmethod
def flock(fd, op: int) -> None:
    fileno = fd.fileno() if hasattr(fd, 'fileno') else fd
    # 强制定位到文件开头，保证两个进程锁同一个 byte
    try:
        os.lseek(fileno, 0, os.SEEK_SET)
    except OSError:
        pass
    if op == _LockShim.LOCK_UN:
        try:
            msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    msvcrt.locking(fileno, msvcrt.LK_LOCK, 1)
```

---

### 🟡 NF-2 [中 · 安全/部署] 反向代理 access log 仍可能记录 admin URL secret

**位置**：部署 runbook（README）、nginx/caddy 配置示例

**问题**：dashboard 启动日志已经不再泄露 `ADMIN_URL_SECRET` 长度，但**反向代理（nginx/caddy/Cloudflare）的 access log 会原样记录请求 URL**，包含完整 secret 前缀：

```
123.45.67.89 - - [16/May/2026:10:30] "GET /Kx3mQ8-pLz2yH9rW5vNcE7bDgJ6fSu4AtT1oIkXzM0s/login HTTP/2"
```

如果运维使用统一日志聚合（ELK/Loki），secret 会同步到日志系统、被多名运维查看。

**影响**：admin secret 间接暴露，攻击面扩大。

**严重程度**：中

**修复建议**：在 README 加 nginx 示例配置：
```nginx
# 替换 admin URL secret 段为占位符再写 access log
map $request_uri $loggable_uri {
    "~^/[A-Za-z0-9_-]{32,}/" "/admin-redacted/";
    default $request_uri;
}
log_format secure '$remote_addr - "$loggable_uri" $status';
access_log /var/log/nginx/access.log secure;
```

---

### 🟡 NF-3 [低 · 代码质量] `tg_bot._unauth_seen` dict 永不清理过期 entry

**位置**：`tg_bot.py:run_bot` 的 `_unauth_seen` 局部 dict

**问题**：
```python
_unauth_seen: dict = {}    # {chat_id: (count, first_seen_ts)}
...
cnt, ts = _unauth_seen.get(chat_id, (0, time.time()))
if time.time() - ts > _UNAUTH_WINDOW_SEC:
    cnt, ts = 0, time.time()
cnt += 1
_unauth_seen[chat_id] = (cnt, ts)
```

虽然有窗口重置逻辑，但**条目本身不会被清理** — 一旦某个 chat_id 进过这个 dict，它就永远在里面（哪怕 1 年没活动）。bot token 泄露被加入数千群组时，dict 会无限增长。

**影响**：极低 — chat_id 总量有限，单条目几十字节，1000 个就是几十 KB。但属于"代码异味"。

**严重程度**：低

**修复建议**：每 N 次循环清理超时的过期 entry：
```python
loop_count += 1
if loop_count % 100 == 0:
    now = time.time()
    _unauth_seen = {
        cid: (c, t) for cid, (c, t) in _unauth_seen.items()
        if now - t < _UNAUTH_WINDOW_SEC * 2
    }
```

---

### 🔵 NF-4 [低 · 多账户语义遗留] `release_partial_stake(account_id=None)` 在老数据上会错账

**位置**：`altcoin_tracker.run()`、`realtime_monitor.check_main_trades()`

**问题**：
```python
for _ppnl, _pstake, _pacc in pending_risk_partials:
    release_partial_stake(_pstake, account_id=_pacc or None)
```

如果 `_pacc` 是空字符串（v4.3 之前的老数据），`_pacc or None == None` → `release_partial_stake` 内 `_resolve_account_id(None)` → 读 `get_current_account_id()` = **当前活跃账户**。

具体场景：
1. acc_A 是当前活跃，acc_B 有一笔老数据交易（`account_id=''`）
2. acc_B 那笔交易触发 TP1
3. tracker 出锁后调 `release_partial_stake(stake, account_id=None)`
4. 实际把 acc_A 的 `total_open_stake` 减少了 → **错账**

注意：`record_trade_closed` 也有同样的语义（这是项目继承的多账户问题，不是 M-1 引入的），但 M-1 让它更显眼。

**影响**：仅在多账户 + 老数据混用 + 升级未做账户标记迁移时出现。一般用户看不到。

**严重程度**：低

**修复建议**：升级时把 `account_id=''` 的旧 trade 全部归属到默认账户（影子账户或第一个真实账户）。或者在 `release_partial_stake / record_trade_closed` 内：
```python
if account_id is None and trade_account_id is not None:
    account_id = trade_account_id  # 优先用 trade 自带的
```

---

### 🔵 NF-5 [低 · code smell] fcntl shim 在 `common.py` 与 `admin_secrets.py` 重复

**位置**：`common.py:18-43`、`admin_secrets.py:13-39`

**问题**：两个文件中各定义了一份完全相同的 `_LockShim` 类。

**影响**：维护性 — 修复 NF-1 时需要改两处，容易遗漏。

**修复建议**：抽出到 `common._LockShim` 单独导出，`admin_secrets` 改 `from common import _LockShim as fcntl`（或更干净地引入 `portalocker` 第三方库）。

---

## 四、回归点压力验证

我尝试**重现原始 bug 行为**确认它们不再发生：

| 原始 bug 行为 | 重现方式（验证手段） | 结果 |
|--------------|---------------------|------|
| H-1：交易所返回错误含 `<` 导致 TG 静默失败 | `tests/test_audit_fixes.py::test_send_tg_falls_back_to_plain_on_html_parse_error` mock 400 响应 | ✅ 自动降级重发，最终送达 |
| M-1：TP1+TP2 后 `total_open_stake` 多 50% | `tests/test_audit_fixes.py::test_tp1_then_tp2_full_round_trip` 模拟开仓+TP1+TP2 | ✅ 净值精确为 0 |
| M-2：极端负费率信号被开仓 | 配置 `FUNDING_MIN = -0.03`，scanner 在 scan_daily 直接 skip | ✅ 静态读源码确认 continue 路径 |
| M-3：回测胜率虚高 | `test_aligned_when_dynamic_features_disabled` 验证两个引擎对齐 | ✅ 两引擎在动态特性关闭时数值一致 |
| M-7：跨账户冷却把对冲账户也卡住 | `test_account_id_filter` 显式传 acc_B 不被 acc_A 止损 block | ✅ 通过 |
| 配置硬阻塞：`DEFAULT_STAKE > balance` 静默写入坏配置 | `test_save_overrides_raises_on_error` 验证 ValueError | ✅ 抛异常，文件未创建 |

---

## 五、未解决问题列表

实质性未解决：**无**。

边界遗留（已被新发现问题覆盖）：
- M-3 中 `BACKTEST_FEE_PCT = 0.04`、`BACKTEST_SLIPPAGE_PCT = 0.1` 默认值仍偏低（建议分别提到 0.05、0.3） — **这是参数选择问题，不是 bug**
- L-1 的 Windows shim 严格说仅"基本可用" — **NF-1 描述了上限**

---

## 六、整体系统健康评估

| 维度 | 评分（满分 5） | 说明 |
|------|---------------|------|
| 功能正确性 | ★★★★★ | 233 个测试覆盖核心路径；事件驱动引擎与实盘口径对齐 |
| 安全性 | ★★★★☆ | 多层防御扎实；NF-2（access log 泄露 secret）需补 runbook |
| 健壮性 | ★★★★☆ | 异常路径都有兜底；NF-1 在 Windows 下有理论隐患 |
| 性能 | ★★★★☆ | 修复未引入明显延迟；事件驱动引擎略增 CPU 但仍 sub-second/小时 |
| 可维护性 | ★★★★☆ | 注释充分；NF-5（fcntl shim 重复）需要重构 |
| 文档完整性 | ★★★★☆ | README 同步及时；NF-2 缺反代部署指南 |

**实盘准入建议**：
- **小仓位实盘**（DEFAULT_STAKE=20U + RISK_MAX_DAILY_LOSS=10U）：✅ 可上线
- **中等规模**（按 README 默认值）：⚠️ 先在 testnet 跑 2 周，验证 TP1+TP2 路径下 `/risk` 数字与 `_calc_actual_open_stake()` 完全一致后上线
- **生产规模**：❌ 不建议；先把 NF-1/NF-2 修掉、跑足 1 个月数据样本

---

## 七、后续建议

### 优先级高（建议下次迭代）
1. **修复 NF-1**：fcntl shim 加 `os.lseek(fd, 0, SEEK_SET)`，让 Windows 锁实际互斥
2. **补 NF-2 runbook**：README 增加 nginx/caddy 示例配置过滤 admin secret URL
3. **多账户老数据迁移脚本**：把 `account_id=''` 的历史 trade 归属到默认账户，永久消除 NF-4

### 优先级中（按需执行）
4. **回测口径**：把 `BACKTEST_FEE_PCT` 默认提到 0.05、`BACKTEST_SLIPPAGE_PCT` 提到 0.3（贴近小币现实）
5. **`fcntl shim` 抽公共模块**：消除 NF-5 的代码重复
6. **Test 矩阵扩 Windows**：CI 加 `windows-latest`，让 fcntl shim 进入持续验证

### 优先级低（长期改进）
7. **覆盖率门禁**：CI 加 `pytest --cov=. --cov-fail-under=70`
8. **集成测试 dockerized**：用 docker-compose + WireMock 模拟 Binance API 做端到端
9. **滑点 / 资金费率统计纳入周报**：现有 `Trade.slippage_pct` 等字段已持久化但无人消费
10. **`fault-injection` 测试**：模拟"下单成功但写盘失败"、"双 LIVE_MODE 一所失败一所成功" 等极端场景
11. **回测引擎 v2 完整化**：把 OKX 交叉验证、量价背离 bonus 也接入事件驱动引擎

---

## 八、关键质量指标

| 指标 | 修复前 | 修复后 | 变化 |
|------|--------|--------|------|
| 测试用例数 | ~50 | 233 | **+366%** |
| 高严重问题 | 1 | 0 | -1 |
| 中严重问题 | 8 | 0 (新发现 NF-1, NF-2 计入新问题) | -8 |
| 低严重问题 | 7 | 0 (新发现 NF-3, NF-4, NF-5 计入新问题) | -7 |
| 主代码 LOC | ~6000 | ~7000 | +1000（含事件驱动引擎） |
| 测试 LOC | ~1500 | ~3500 | +2000 |
| Windows 本地可跑 | ❌ | ✅（有 NF-1 边界） | ✓ |

---

## 九、待办（NF-1~NF-5 未执行）

下次迭代候选清单：

| ID | 严重 | 操作 | 估时 |
|----|------|------|------|
| NF-1 | 中 | fcntl Windows shim 加 `os.lseek(0)` | 30 分钟 |
| NF-2 | 中 | README 补 nginx 反代脱敏配置示例 | 30 分钟 |
| NF-3 | 低 | tg_bot `_unauth_seen` 加定期清理 | 15 分钟 |
| NF-4 | 低 | 多账户老数据迁移脚本 + release_partial_stake 优先用 trade.account_id | 1 小时 |
| NF-5 | 低 | fcntl shim 抽公共模块 | 30 分钟 |

---

**结论**：系统已经从"半成品"演进到"可信基线"。本次审计未发现任何会导致直接资金损失的问题；新增的 NF-1/NF-2 都是边界场景。可以**带着 NF-1/NF-2 的 known-issue 进入小仓位实盘验证阶段**，不会因这些问题造成实盘事故。
