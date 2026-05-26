# 影子多策略交易系统 - 技术文档 v5.x

> **重要**: 从 v5.x 开始本系统是**多策略架构**（做空 + 做多并行），由 `StrategyRegistry`
> 自动发现 `strategies/<name>/strategy.py` 注册的策略并并行运行。
> 权威入口仍然是 [README.md](README.md)，本文档只补充 README 没有的细节。
>
> 历史变更：v4.0 是多策略版（funding_arb + low_risk + long_scanner），v4.1 一度精简到只剩
> short_overbought，v5.x 重新打开多策略路径——这次不依赖硬编码的策略列表，
> 而是基于 `BaseStrategy` 抽象 + 注册表机制，新增策略只需要新建子目录。

---

## 1. 架构总览

参见 [README.md](README.md) 中的"系统架构"章节。核心进程有三个：

- `async_engine.py` — 异步策略引擎 v2.0，asyncio 主进程（替代旧 `scheduler.py`）。
  内部启动 10 个 loop：scan / confirm / tracker / config_reload / reconcile /
  exchange_sync / health_audit / macro_collection / journal_recovery / daily_tasks /
  close_retry。
- `realtime_monitor.py` — Binance WebSocket 实时止盈止损；断线 30s 后降级到主动 REST 轮询。
- `dashboard.py` — Flask + SocketIO 仪表盘，含 admin 面板（多层防御）。

三者通过 `altcoin_shadow_trades.json` / `risk_state.json` / `altcoin_candidates.json`
进行数据共享，所有读写都走 `common.LockedJsonFile`（fcntl 排他锁 + 原子写，**默认 10s 超时**）。

---

## 2. 多策略框架

### 2.1 策略发现与加载

`strategies/registry.py:auto_discover()` 在引擎启动时扫描 `strategies/*/strategy.py`，
任何继承 `BaseStrategy` 的子类自动注册到 `StrategyRegistry` 单例。

每个策略目录的标准结构：

```
strategies/<name>/
├── __init__.py
├── strategy.py        # 实现 BaseStrategy 的子类
└── (可选) signals.py / params.py
```

要禁用某个策略，在 `config/strategy.yaml` 里把对应 key 设为 `enabled: false`，
不要删代码（保持回归测试可运行）。

### 2.2 当前内置策略

| 策略名 | 方向 | 触发逻辑 | 默认 enabled |
|---|---|---|---|
| `short_overbought` | SHORT | 日线 RSI ≥ 75 + 4h RSI 回落 / 弃盘点 | ✅ |
| `long_oversold` | LONG | 日线 RSI ≤ 25 + 反弹确认 | ✅ |
| `prepump_sniffer` | LONG | OI 异动 + 资金费率回归 + 成交量预热 | ✅ |

### 2.3 多策略与风控

- 每个 Trade 记录 `strategy` 字段，weekly_report 按策略分别评级 A~F
- 风控 `can_open_trade(strategy=...)` 已支持按策略隔离（保留参数，目前共用账户级阈值）
- `is_in_cooldown` 按 symbol 不区分方向：同币止损后冷却期内**两个方向都不开**
  （避免反向"伪突破"陷阱）

---

## 3. 关键数据语义（v4.1 修订，v5.x 沿用）

### 3.1 Trade 的盈亏字段

| 字段 | 含义 | 关闭后 |
|---|---|---|
| `stake` | 开仓时保证金 | 不变 |
| `stake_remaining` | 当前剩余仓位保证金 | 关闭时保持关闭前的值 |
| `tp1_locked_pnl` | TP1 已锁定的盈利（独立记账） | 不变 |
| `tp1_stake_released` | TP1 半仓 stake 是否已从 risk 扣减（H1 防双扣） | 不变 |
| `pnl` | **剩余仓位**的盈亏（浮动或实现） | 剩余仓位的实现盈亏 |
| **总盈亏** | `tp1_locked_pnl + pnl` | 同左 |

### 3.2 LONG vs SHORT 的盈亏方向

```python
# tracker._compute_pnl_pct（简化）
if trade.direction == 'LONG':
    pnl_pct = (current_price - entry) / entry * 100
else:  # SHORT
    pnl_pct = (entry - current_price) / entry * 100
```

止盈止损价方向也对应翻转：LONG 的 `take_profit_1 = entry × 1.05`，
SHORT 的 `take_profit_1 = entry × 0.95`。

### 3.3 错误示范（v4.0 曾犯的 bug）

```python
# ❌ 错误：关闭时把合计写进 pnl，后续累计处又 + tp1_locked_pnl，等于重复计算一次 TP1
trade.pnl = trade.tp1_locked_pnl + remaining_pnl
total = trade.tp1_locked_pnl + trade.pnl   # TP1 被算两次
```

### 3.4 正确用法

```python
# ✅ 正确：pnl 只记剩余仓位，合计时才加 tp1_locked_pnl
trade.pnl = remaining_pnl
total = trade.tp1_locked_pnl + trade.pnl   # 一次性就对
```

### 3.5 CloseType 枚举

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

## 4. 并发与数据一致性

### 4.1 锁顺序

所有"开仓 / 平仓"副作用严格按以下顺序：

1. 持锁读 `altcoin_shadow_trades.json`（**默认锁超时 10s**）
2. 修改内存中的 Trade 对象
3. 持锁原子写回
4. **出锁** 后才 `record_trade_closed()` / `record_trade_opened()`
5. 最后 `send_tg()`

这样即使 4/5 崩溃，下一次 `reconcile_risk_state()` 也能从 trades 反推，
不会出现"风控记了账但交易没落盘"的幽灵亏损。

### 4.2 Journal + 启动对账 + 开仓预扫（H2）

`common.journal_*` 把"下单意图"先写入 in-flight journal（`trades_inflight.json`），
下单成功后 confirm，失败则 mark_failed。三层防御：

1. **启动时**：`journal_recovery.recover_inflight()` 反查所有 pending，
   交易所有真实成交但 trades.json 没有 → 推 TG 告警让人工处理
2. **开仓前**：`altcoin_scanner._open_position` 在写 journal 前**先扫一次同币 pending**，
   避免重启窗口期重复下单造成双倍仓位（H2 修复）
3. **运行中**：`async_engine._journal_recovery_loop` 每 30 分钟兜底扫描

### 4.3 平仓失败重试（H4）

`_perform_exchange_close` 失败 3 次后会标记 `close_retry_pending=True`，
`async_engine._close_retry_loop` 每 5 分钟扫描这类标记并主动重试，
直到成功或人工干预（推 TG 告警）。

### 4.4 TP1 半仓释放保证金幂等（H1）

`release_partial_stake` 现在配合 `trade.tp1_stake_released=True` 标记：

- evaluate_trade 触发 TP1 → 在锁内先 set 标记，再返回 `pending_risk_partial`
- 调用方出锁后 `release_partial_stake(stake)` 扣减 `total_open_stake`
- 同一笔 trade 二次进入 evaluate（极端并发 / 重启）时，标记已存在 → 跳过返回

防止 `realtime_monitor` 和 `tracker` 同时触发 TP1 时双重扣减 stake。

### 4.5 启动对账

`async_engine._run_startup_sequence()` 启动时调用 `reconcile_risk_state(notify=True)`，
从 `altcoin_shadow_trades.json` 反推 `daily_loss / daily_trades_opened /
total_open_stake`，与 `risk_state.json` 对比，有漂移就自动修正并 TG 告警。

### 4.6 候选池并发

`altcoin_scanner.scan_daily` 和 `check_candidates` 都通过
`LockedJsonFile(CANDIDATES_FILE, default={})` 原子更新，防止两条扫描
同时运行时互相覆盖。

---

## 5. 参数配置（当前实际值）

| 参数 | 值 | 注释 |
|---|---|---|
| `DEFAULT_STAKE` | 33 | 单笔保证金 (U)，自动 = ACCOUNT_BALANCE / MAX_OPEN_TRADES |
| `LEVERAGE` | 10 | 杠杆 |
| `HARD_STOP_LOSS_PCT` | 5.0 | 硬止损 %（ATR 模式时按动态值持久化到 `trade.hard_stop_pct`） |
| `TP1_MULTIPLIER` | 0.95 | SHORT TP1 = 入场 × 0.95（-5%）；LONG 镜像为 1.05 |
| `TP2_MULTIPLIER` | 0.92 | SHORT TP2 = 入场 × 0.92（-8%）；LONG 镜像为 1.08 |
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
| `WS_DISCONNECT_FALLBACK_SEC` | 30 | WS 断线降级到主动轮询的延迟（H5） |

完整清单见 [config/_defaults.py](config/_defaults.py)。

---

## 6. 数据文件

| 文件 | 路径 | 用途 |
|---|---|---|
| `altcoin_shadow_trades.json` | 项目根 | 所有交易记录（开仓+平仓） |
| `altcoin_candidates.json` | 项目根 | 候选池（多策略，每条带 `strategy` 字段） |
| `risk_state.json` | 项目根 | 风控状态（按账号隔离） |
| `trades_inflight.json` | 项目根 | In-flight journal（下单意图，启动/周期反查） |
| `altcoin_trades_archive.json` | 项目根 | 30 天前的归档交易 |
| `weekly_report.json` | 项目根 | 最近一期周报数据 |
| `backtest_cache/` | 项目根 | 回测 K 线缓存 |
| `runtime_config.json` | 项目根 | admin 面板运行时覆盖 |
| `admin_secrets.json` | 项目根 | 加密 API 凭证（0600） |

**这些文件不应该提交到 Git**，`.gitignore` 已排除。

---

## 7. 实盘切换

1. 配置 `.env` 或 admin 面板中的 `BINANCE_API_KEY` / `BINANCE_SECRET` / `OKX_*`
2. 进入 admin 面板 → Live Control → 把目标交易所 `live_mode` 切到 True
3. 系统在写入 trade 之前会调用 `live_executor.execute_open_short/long()` 实际下单，
   使用返回的成交均价作为 `entry_price`，而不是下单前的 ticker 价
4. 下单使用 `client_order_id` 幂等键，网络抖动重试不会重复下单
5. 写盘前先写 in-flight journal，崩溃后启动可反查

---

## 8. 测试

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -x -q
```

测试夹具在 `tests/conftest.py`，全局 mock `ccxt`。
如果修改了 Trade.pnl 语义，所有断言应保持"合计盈亏 = tp1_locked_pnl + pnl"。

CI 工作流额外运行 `ruff check .` 和带 coverage gate 的 pytest。

---

## 9. 变更历史

- **v5.x（当前）—— 多策略 + 系统稳定性强化**
  - 多策略架构正式回归：short_overbought + long_oversold + prepump_sniffer 并行
  - 文档与代码对齐（不再宣称"仅做空"）
  - H1：`release_partial_stake` 配合 `tp1_stake_released` 标记防双扣
  - H2：开仓前 journal 预扫，重启窗口期不重复下单
  - H4：`close_retry_pending` 主动重试 worker（每 5 分钟）
  - H5：WS 断线 30s 后降级主动 REST 轮询，5 分钟才告警的旧时序仅作 escalation
  - H7：`LockedJsonFile` 默认锁超时统一为 10s
  - M3：启动配置 ERROR 触发 SAFE_MODE，禁止开仓但保持系统其他功能在线
  - M4：ATR 动态止损值持久化到 `trade.hard_stop_pct`
  - M7：`requirements.txt` 全部 `>=` 改成 `==`
  - M8：删除 Gate.io 死代码
  - M11：Redis 加 appendonly + maxmemory 调整
  - CI：加 ruff lint + coverage gate（80%）

- **v4.1**
  - 修复 TP1 双计数 bug（`trade.pnl` 语义从"合计"改为"剩余仓位"）
  - 新增 `CloseType` 枚举替代字符串匹配
  - `RISK_MAX_POSITION_PCT` 从 0.9 回退到 0.5
  - `RISK_MAX_DAILY_TRADES` 从 2 改为 3
  - `realtime_monitor` 改用内存快速过滤，减少磁盘 IO
  - scheduler 改用"上次执行时间 + 周期"判断，避免跨分钟丢任务
  - Dockerfile 使用 `requirements.txt` 并固定版本
  - `docker-compose` 只挂载 data/，代码通过 COPY 打入镜像
  - RSI 计算丢弃最后一根未收盘 K 线，避免 intra-bar 噪声

- **v4.0** - 多策略版本（funding_arb + low_risk + long_scanner），后被 v4.1 精简掉
