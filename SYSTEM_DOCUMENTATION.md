# 影子做空交易系统 - 技术文档 v4.1

> **重要**: 从 v4.1 开始本文档精简为"只反映真实代码"的版本。
> 权威入口仍然是 [README.md](README.md)，本文档只补充 README 没有的细节。
> 旧版 v6.0 提到的 funding_arb / long_scanner / low_risk_strategy 等策略
> **已在 v4.1 整体移除**，当前系统只保留 `short_overbought` 做空策略。

---

## 1. 架构总览

参见 [README.md](README.md) 中的"系统架构"章节。核心进程有三个：

- `scheduler.py` — 定时任务 + 启动 hot_scanner / tg_bot 子线程
- `realtime_monitor.py` — Binance WebSocket 实时止盈止损
- `dashboard.py` — Flask + SocketIO 仪表盘

三者通过 `altcoin_shadow_trades.json` / `risk_state.json` / `altcoin_candidates.json`
进行数据共享，所有读写都走 `common.LockedJsonFile`（fcntl 排他锁 + 原子写）。

---

## 2. 关键数据语义（v4.1 修订）

### 2.1 Trade 的盈亏字段

这是 v4.1 改动最大的地方，**请在扩展代码时严格遵守这套语义**：

| 字段 | 含义 | 关闭后 |
|---|---|---|
| `stake` | 开仓时保证金 | 不变 |
| `stake_remaining` | 当前剩余仓位保证金 | 关闭时保持关闭前的值 |
| `tp1_locked_pnl` | TP1 已锁定的盈利（独立记账） | 不变 |
| `pnl` | **剩余仓位**的盈亏（浮动或实现） | 剩余仓位的实现盈亏 |
| **总盈亏** | `tp1_locked_pnl + pnl` | 同左 |

### 2.2 为什么不合并？

因为 TP1 触发后交易仍然 `status == 'open'`，展示端（dashboard / tg_bot）
需要区分"已锁住的钱"和"还在浮动的剩余仓位"。

### 2.3 错误示范（v4.0 曾犯的 bug）

```python
# ❌ 错误：关闭时把合计写进 pnl，后续累计处又 + tp1_locked_pnl，等于重复计算一次 TP1
trade.pnl = trade.tp1_locked_pnl + remaining_pnl
total = trade.tp1_locked_pnl + trade.pnl   # TP1 被算两次
```

### 2.4 正确用法

```python
# ✅ 正确：pnl 只记剩余仓位，合计时才加 tp1_locked_pnl
trade.pnl = remaining_pnl
total = trade.tp1_locked_pnl + trade.pnl   # 一次性就对
```

### 2.5 CloseType 枚举

`trade.close_type` 字段记录关闭原因（机器可读）。可选值：

| 值 | 含义 | 触发冷却 |
|---|---|---|
| `hard_stop` | 硬止损 | ✅ |
| `trail_stop` | 移动止损 | ✅ |
| `breakeven_stop` | TP1 后保本止损 | ❌（TP1 已锁了利润） |
| `time_stop` | 时间止损 | ✅（仍按亏损对待） |
| `tp2` | TP2 全仓止盈 | ❌ |
| `manual` | 手动平仓 | ❌ |

**不要用字符串匹配 `close_reason` 来判断是否止损**，应改用 `CloseType.is_stop_loss(trade.close_type)`。

---

## 3. 并发与数据一致性

### 3.1 锁顺序

所有"开仓 / 平仓"副作用严格按以下顺序：

1. 持锁读 `altcoin_shadow_trades.json`
2. 修改内存中的 Trade 对象
3. 持锁原子写回
4. **出锁** 后才 `record_trade_closed()` / `record_trade_opened()`
5. 最后 `send_tg()`

这样即使 4/5 崩溃，下一次 `reconcile_risk_state()` 也能从 trades 反推，
不会出现"风控记了账但交易没落盘"的幽灵亏损。

### 3.2 启动对账

`scheduler.main_loop()` 启动时调用 `reconcile_risk_state(notify=True)`，
从 `altcoin_shadow_trades.json` 反推 `daily_loss / daily_trades_opened /
total_open_stake`，与 `risk_state.json` 对比，有漂移就自动修正并 TG 告警。

### 3.3 候选池并发

`altcoin_scanner.scan_daily` 和 `check_candidates` 都通过
`LockedJsonFile(CANDIDATES_FILE, default={})` 原子更新，防止两条扫描
同时运行时互相覆盖。

---

## 4. 参数配置（当前实际值）

| 参数 | 值 | 注释 |
|---|---|---|
| `DEFAULT_STAKE` | 100 | 单笔保证金 (U) |
| `LEVERAGE` | 10 | 杠杆 |
| `HARD_STOP_LOSS_PCT` | 5.0 | 硬止损 % |
| `TP1_MULTIPLIER` | 0.95 | TP1 = 入场 × 0.95（-5%） |
| `TP2_MULTIPLIER` | 0.92 | TP2 = 入场 × 0.92（-8%） |
| `TP1_CLOSE_RATIO` | 0.5 | TP1 平仓比例 |
| `TRAIL_STOP_ACTIVATE_PCT` | 3.0 | 移动止损激活门槛 |
| `TRAIL_STOP_RETRACE_RATIO` | 0.4 | 移动止损回撤比例（从最高盈利回撤 40% 触发） |
| `MAX_HOLD_DAYS` | 1 | 最大持仓天数 |
| `RISK_MAX_DAILY_LOSS` | 30 | 日亏上限 (U) |
| `RISK_MAX_DAILY_TRADES` | 3 | 日开仓上限 |
| `RISK_MAX_POSITION_PCT` | 0.5 | 最大持仓占比 |
| `RISK_CONSECUTIVE_LOSS_PAUSE` | 3 | 连亏暂停阈值 |
| `RISK_PAUSE_HOURS` | 24 | 暂停时长 |
| `COOLDOWN_HOURS` | 24 | 止损后同币冷却 |
| `CANDIDATE_EXPIRE_HOURS` | 12 | 候选池超时 |

完整清单见 [config.py](config.py)。

---

## 5. 数据文件

| 文件 | 路径 | 用途 |
|---|---|---|
| `altcoin_shadow_trades.json` | 项目根 | 所有交易记录（开仓+平仓） |
| `altcoin_candidates.json` | 项目根 | 候选池（日线 RSI>80 的币） |
| `risk_state.json` | 项目根 | 风控状态（当日亏损/次数/暂停时间） |
| `altcoin_trades_archive.json` | 项目根 | 30 天前的归档交易 |
| `weekly_report.json` | 项目根 | 最近一期周报数据 |
| `backtest_cache/` | 项目根 | 回测 K 线缓存 |

**这些文件不应该提交到 Git**，`.gitignore` 已排除。

---

## 6. 实盘切换

1. 配置 `.env` 中的 `BINANCE_API_KEY` / `BINANCE_SECRET`
2. 修改 `config.py` → `LIVE_MODE = True`
3. `altcoin_scanner.check_candidates()` 会在写入 trade 之前调用
   `live_executor.execute_open_short()` 实际下单，使用返回的成交均价
   作为 `entry_price`，而不是下单前的 ticker 价
4. 下单使用 `newClientOrderId` 幂等键，网络抖动时重试不会重复下单

---

## 7. 测试

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -x -q
```

测试夹具在 `tests/conftest.py`，mock 了 `ccxt` 和 `config` 关键值。
如果修改了 Trade.pnl 语义，所有断言应保持"合计盈亏 = tp1_locked_pnl + pnl"。

---

## 8. 变更历史

- **v4.1** (本次)
  - 修复 TP1 双计数 bug（`trade.pnl` 语义从"合计"改为"剩余仓位"）
  - 新增 `CloseType` 枚举替代字符串匹配
  - 移除已废弃的 funding_arb / low_risk / long_scanner 残留代码与配置
  - `.gitignore` 补齐敏感文件（.env、运行数据）
  - `RISK_MAX_POSITION_PCT` 从 0.9 回退到 0.5
  - `RISK_MAX_DAILY_TRADES` 从 2 改为 3
  - `realtime_monitor` 改用内存快速过滤，减少磁盘 IO
  - scheduler 改用"上次执行时间 + 周期"判断，避免跨分钟丢任务
  - Dockerfile 使用 `requirements.txt` 并固定版本
  - `docker-compose` 只挂载 data/，代码通过 COPY 打入镜像
  - RSI 计算丢弃最后一根未收盘 K 线，避免 intra-bar 噪声

- **v4.0** - 多策略版本（funding_arb + low_risk + long_scanner），已整体弃用
