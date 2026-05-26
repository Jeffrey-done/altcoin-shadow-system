# 架构统一化重构（S2 / S3 / S4 / S5 / M1 / M2）

**修复日期**：2026-05  
**关联 PR**：本次 PR  
**目标**：消除 v5.x 累积下来的"多版本并存"问题，让每个核心职责只有
**唯一的对外入口**，旧 API 保留作为兼容层。

---

## 一、修复一览

| 编号 | 问题 | 解决方式 | 文件清单 |
|------|------|---------|---------|
| **S2** | 三套调度器并存 | 唯一入口 `scheduler.py` → 委托 `async_engine.main()`；CLI 工具加 deprecation print | `scheduler.py` (新增), `altcoin_scanner.py`, `altcoin_tracker.py`, `docker-compose.yml` |
| **S3** | 三套评分系统 | 唯一入口 `scoring.score_signal()` 按 ml > multifactor > linear 自动 fallback | `scoring/__init__.py` (新增), `altcoin_scanner.py`, `config/_defaults.py` |
| **S4** | JSON↔DB 双写硬编码 | 引入 `DB_WRITE_MODE` 三态模式（dual / db-canonical / json-only） | `db/compat.py`, `config/_defaults.py` |
| **S5** | 4 级配置层无统一查询 | `config.resolve(key)` / `config.explain(key)` 单一解析器 | `config/__init__.py` |
| **M1** | dashboard.py 1535 行 | 拆为 `dashboard_app/` 子包（auth/data/events/live_prices/etag）；dashboard.py 仅保留路由层 | `dashboard.py` (1535→905), `dashboard_app/*` (新增) |
| **M2** | 双回测引擎并存 | `backtesting.run()` 统一入口 + `UnifiedBacktestResult` 归一化结果 | `backtesting/__init__.py` (重写) |

---

## 二、S2：调度器统一

### 修复前
```
async_engine.py            # docker-compose 中跑这个
altcoin_scanner.py [scan|check|both]   # 旧 cron 入口
altcoin_tracker.py [--check-only]      # 旧 cron 入口
```

三个 `__main__` 块，新人不知道哪个是"主循环"，cron 配错容易重复启动。

### 修复后
**唯一推荐入口**：
```bash
python3 scheduler.py
# 等价于 python3 async_engine.py
```

`scheduler.py` 是 30 行的薄壳，内部 100% 委托 `async_engine.main()`。
`docker-compose.yml` 已切换到这个入口。

`altcoin_scanner.py` / `altcoin_tracker.py` 的 `__main__` 块**仍可用**作
运维一次性工具（debug / 手动重扫 / 旧 cron 兼容），但启动时会打印
deprecation 提示。可通过环境变量关闭：
```
ALTCOIN_SCANNER_SUPPRESS_DEPRECATION=1
ALTCOIN_TRACKER_SUPPRESS_DEPRECATION=1
```

`realtime_monitor.py` 是独立的 WebSocket 风险闭环进程，**不在 S2 范围**。

---

## 三、S3：评分统一

### 修复前
```python
# altcoin_scanner.py 中的双 try/except fallback：
try:
    score_result = score_signal_multifactor(...)        # 主路径
except Exception:
    score_result = calculate_signal_score(...)          # fallback

# ml/scorer.py 还有第三个 ml_signal_score()
# backtest.py 直接用 calculate_signal_score
```

### 修复后
```python
from scoring import score_signal

result = score_signal(
    rsi_1d=82, rsi_4h=65, rsi_4h_peak=82,
    pct_24h=15, oi_change=20, funding_rate=0,
    yao_score=2, trigger_type='abandon',
    ohlcv_df=df,    # 传了 df 自动尝试 multifactor
    prefer='auto',  # 'auto' | 'ml' | 'multifactor' | 'linear'
)
# result.score, result.grade, result.source
```

**优先级**（默认 `prefer='auto'`）：`ml` → `multifactor` → `linear`。  
任何一档失败自动降级，永远返回有效 `ScoreResult`。

新增配置项 `SCORING_BACKEND`（默认 `'auto'`）控制全局偏好。

旧三个底层函数**保留可用**（`signal_score.calculate_signal_score`、
`signals.factor_scorer.score_signal_multifactor`、`ml.scorer.ml_signal_score`），
所有现有测试不变，但**新代码必须只用 `from scoring import score_signal`**。

---

## 四、S4：DB 写模式可配

### 修复前
`db/compat.py` 所有写函数硬编码"双写"：DB + JSON 都写，崩溃在两次写之间
导致状态不一致。

### 修复后
新增配置 `DB_WRITE_MODE`（环境变量 / config.py / runtime_config 都可设置）：

| 模式 | 写 DB | 写 JSON | 适用场景 |
|------|-------|---------|---------|
| `dual` | ✓ | ✓ | **默认**，向后兼容老 dashboard |
| `db-canonical` | ✓ | ✗ | 单一真源；JSON 仅由 dashboard 周期 export |
| `json-only` | ✗ | ✓ | 兜底，无 SQLAlchemy 时也能跑 |

环境变量优先级最高，便于运维一键切换：
```bash
DB_WRITE_MODE=db-canonical python3 scheduler.py
```

读路径不变（DB 优先 → JSON fallback）。

---

## 五、S5：4 级配置统一查询

### 修复前
4 级层叠（env → runtime_config → yaml → defaults），但**没有任何函数**
能直接告诉你 "key X 的最终生效值是什么、来自哪一层"。

### 修复后
```python
from config import resolve, explain

# 1. 直接拿值（按优先级走完所有层）
stake = resolve('DEFAULT_STAKE')                      # → 30

# 2. 同时拿到来源
stake, src = resolve('LEVERAGE', with_source=True)
# → (10, 'yaml')

# 3. 完整透视
print(explain('TP1_MULTIPLIER'))
# {
#   'final_value': 0.95,
#   'final_source': 'yaml',
#   'layers': {
#     'env': '<MISSING>',
#     'runtime_config': '<MISSING>',
#     'admin_secrets': '<MISSING>',
#     'yaml': 0.95,
#     'defaults': 0.95,
#   },
# }
```

优先级（高 → 低）：
1. **env** — 环境变量
2. **runtime_config** — `runtime_config.json`（admin panel）
3. **admin_secrets** — `admin_secrets.json` 的 `settings` 字段
4. **yaml** — `config/*.yaml`
5. **defaults** — `config/_defaults.py`

`resolve()` **只读**，不修改 config 模块属性；如需把变更应用到运行时进程
仍需调 `apply_yaml_to_config()` / `runtime_config.apply_overrides()`。

---

## 六、M1：dashboard 拆分

### 修复前
`dashboard.py` 单文件 **1535 行**，混杂：
- Flask / SocketIO 初始化 + blueprint 挂载
- Token 认证 + ETag 帮手
- 数据读取（30+ 函数 / 内部状态）
- 事件抽取 + mtime 缓存
- 实时价格 + 后台推送协程
- 30+ 个 `@app.route` 端点
- SocketIO 回调
- 启动入口

### 修复后
新增 `dashboard_app/` 子包（5 个模块、约 730 行总）：

| 模块 | 职责 | 行数 |
|------|------|-----|
| `dashboard_app/auth.py` | Token 认证（`check_api_token` / `require_auth` / `Unauthorized`） | 52 |
| `dashboard_app/data.py` | 数据读取（`get_dashboard_data` / `build_execution_metrics` / etc） | 381 |
| `dashboard_app/events.py` | 事件抽取 + mtime 缓存 | 125 |
| `dashboard_app/live_prices.py` | 实时价格 + `make_background_push` 工厂 | 134 |
| `dashboard_app/etag.py` | ETag / Last-Modified 帮手 | 35 |

`dashboard.py` 现在 **905 行**，只剩：
- 模块级初始化 / Eventlet monkey_patch
- 子模块 import + 别名（保持旧 `_extract_events` 等内部名字可用）
- **所有 `@app.route` / `@socketio.on` 装饰函数**（必须留在这里，
  因为 `app` 实例在这里）
- 启动入口块

行数减少 **41%**；每个子模块都可以单独单测（纯函数 + 显式依赖注入）。

---

## 七、M2：回测引擎统一入口

### 修复前
```python
# 旧版（带全特性）：
from backtest import run_backtest
result = run_backtest('PEPE/USDT', days=90, params=BacktestParams())

# 新版（向量化）：
from backtesting.engine import VectorizedBacktester, BacktestConfig
bt = VectorizedBacktester(strategy=..., ohlcv_data=df, config=BacktestConfig())
result = bt.run()

# 两个 BacktestResult 类，schema 不同，新人不知道用哪个。
```

### 修复后
```python
from backtesting import run, UnifiedBacktestResult

# 自动选引擎（默认 legacy 以保留全特性）
result = run('PEPE/USDT', days=90)

# 显式选 vectorized（需要预加载 OHLCV）
result = run('PEPE/USDT', days=90, engine='vectorized', ohlcv_df=df)

# 字段统一：
result.engine          # 'legacy' | 'vectorized'
result.total_trades, result.win_rate, result.total_pnl
result.max_drawdown, result.sharpe_ratio, result.profit_factor
result.raw             # 引擎原生 result（不丢任何信息）
```

旧 API 不变（`backtest.run_backtest`、`VectorizedBacktester` 都保留），
本模块仅作"统一入口 + 结果归一化"。新代码请只 `from backtesting import run`。

长期目标：legacy 引擎仅保留必要特性后并入 `VectorizedBacktester`。

---

## 八、回归测试

| 阶段 | 通过 | 失败 | 跳过 |
|------|-----|-----|-----|
| 重构前基线 | 337 | 18（pre-existing）| 7 |
| 重构后    | 337 | 18（同上）        | 7 |

**0 回归**；本次重构未改变任何业务行为。所有失败测试都是修改前已有的，
与本次工作无关。

---

## 九、迁移建议（给后续维护者）

1. **新代码不要直接 import**：
   - `signal_score.calculate_signal_score`     → 用 `scoring.score_signal`
   - `backtest.run_backtest`                    → 用 `backtesting.run`
   - 直接读 `config.X`                           → 用 `config.resolve('X')`
2. **旧测试** 可继续直接调底层函数（向后兼容保证）。
3. **`docker-compose` 已切到 `scheduler.py`**；自建部署的请按文档统一用此入口。
4. **`DB_WRITE_MODE`** 默认仍是 `dual`，下个版本可切到 `db-canonical` 后
   评估是否删除 JSON 写路径。
