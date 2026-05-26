# Altcoin Shadow System - 完整系统文档

**版本**: v5.x (多策略架构)  
**最后更新**: 2026-05-26  
**维护者**: Jeffrey-done  

---

## 目录

1. [系统概述](#1-系统概述)
2. [整体架构](#2-整体架构)
3. [核心进程](#3-核心进程)
4. [多策略框架](#4-多策略框架)
5. [信号评分系统](#5-信号评分系统)
6. [风控体系](#6-风控体系)
7. [交易所管理与执行](#7-交易所管理与执行)
8. [实时监控](#8-实时监控)
9. [数据层与持久化](#9-数据层与持久化)
10. [事件总线](#10-事件总线)
11. [配置管理](#11-配置管理)
12. [回测子系统](#12-回测子系统)
13. [宏观数据与过滤](#13-宏观数据与过滤)
14. [机器学习模块](#14-机器学习模块)
15. [Dashboard 与管理面板](#15-dashboard-与管理面板)
16. [监控与可观测性](#16-监控与可观测性)
17. [部署架构](#17-部署架构)
18. [运维手册](#18-运维手册)
19. [数据文件清单](#19-数据文件清单)
20. [故障排查](#20-故障排查)
21. [安全模型](#21-安全模型)
22. [版本历史](#22-版本历史)

---


## 1. 系统概述

**Altcoin Shadow System** 是一个全自动化的小币种多空双向影子交易系统，7x24 小时运行。系统并行运行做空与做多策略，自动完成信号扫描、候选确认、仓位开平、止盈止损、风控管理全流程。

### 核心能力

| 能力 | 说明 |
|------|------|
| 多策略并行 | short_overbought / long_oversold / prepump_sniffer 三策略同时运行 |
| 双交易所 | Binance + OKX 实盘路由，支持分仓对冲 |
| 实时监控 | WebSocket 100ms 级止盈止损，断线自动降级 REST 轮询 |
| 多层风控 | 单笔风控 + 组合风控(VaR/Kelly/相关性) + 宏观过滤 |
| 自动复利 | 按盈利阶梯自动放大仓位 |
| 影子并行 | 实盘同时保留影子记录用于数据对比 |
| 多账户隔离 | 每个账户独立风控额度，互不干扰 |

### 技术栈

- **语言**: Python 3.11+
- **异步框架**: asyncio + aiohttp（引擎层）
- **交易所 API**: ccxt (Binance/OKX)
- **实时数据**: websocket-client (Binance WebSocket)
- **Web 框架**: Flask + Flask-SocketIO + Eventlet
- **数据库**: SQLAlchemy (SQLite WAL) + JSON 双写兼容
- **缓存/消息**: Redis (Pub/Sub + AOF 持久化)
- **监控**: Prometheus metrics 端点
- **部署**: Docker Compose (3 服务 + Redis)
- **CI/CD**: GitHub Actions (pytest + ruff lint + coverage gate)

---


## 2. 整体架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     AsyncStrategyEngine v2.0 (调度层)                      │
│                                                                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │scan_loop │  │confirm   │  │tracker   │  │reconcile │  │daily     │  │
│  │(每小时)   │  │_loop     │  │_loop     │  │_loop     │  │_tasks    │  │
│  │          │  │(每5分钟)  │  │(每分钟)   │  │(每10分钟) │  │_loop     │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────────┘  └──────────┘  │
│       │              │              │                                      │
│  ┌────▼──────────────▼──────────────▼─────────────────┐                   │
│  │         StrategyEngine + StrategyRegistry           │                   │
│  │  short_overbought | long_oversold | prepump_sniffer │                   │
│  └────────────────────────────────────────────────────┘                   │
├─────────────────────────────────────────────────────────────────────────┤
│                        数据 & 执行层                                       │
│                                                                           │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐         │
│  │AsyncData   │  │scoring     │  │risk_control│  │live_       │         │
│  │Feed        │  │(统一评分)   │  │(多账户风控) │  │executor    │         │
│  │(aiohttp)   │  │ml>mf>linear│  │+portfolio  │  │(Binance/OKX)│         │
│  └────────────┘  └────────────┘  └────────────┘  └────────────┘         │
├─────────────────────────────────────────────────────────────────────────┤
│                        基础设施层                                          │
│                                                                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │EventBus  │  │config    │  │db/compat │  │common    │  │monitoring│  │
│  │(Redis/Mem)│  │(4级合并)  │  │(双写)     │  │(锁/日志)  │  │(Prometheus)│ │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘  │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────┐     ┌─────────────────────────────────┐
│   realtime_monitor.py           │     │   dashboard.py + dashboard_app/ │
│   WebSocket 实时止盈止损          │     │   Flask + SocketIO + Admin Panel │
│   断线30s降级REST | 5min告警     │     │   8层安全防御 | 配置热加载        │
└─────────────────────────────────┘     └─────────────────────────────────┘
```

### 数据流概览

```
全市场 Tickers (Binance API)
        │
        ▼
  scan_loop: 日线RSI + 涨幅 + 成交量过滤
        │
        ▼
  候选池 (altcoin_candidates.json)
        │
        ▼
  confirm_loop: 4h RSI回落 / 弃盘点 → 信号评分 → 风控检查
        │
        ▼
  开仓 (altcoin_shadow_trades.json + 交易所下单)
        │
        ▼
  tracker_loop + realtime_monitor: 止盈止损评估
        │
        ▼
  平仓 → risk_control 记账 → TG 推送 → EventBus 广播
```

---


## 3. 核心进程

系统由 3 个独立进程组成，通过共享 JSON 文件 + Redis EventBus 协作：

### 3.1 AsyncStrategyEngine (`async_engine.py`)

**生产级异步调度器**，完全替代旧版 `scheduler.py`。

| 循环 | 周期 | 职责 |
|------|------|------|
| `_scan_loop` | 每小时 | 全市场扫描，产生候选 |
| `_confirm_loop` | 每5分钟 | 候选确认 + 信号评分 + 风控 + 开仓 |
| `_tracker_loop` | 每分钟 | 持仓止盈止损评估 |
| `_config_reload_loop` | 每30秒 | runtime_config 热加载 |
| `_reconcile_loop` | 每10分钟 | 风控状态与交易记录对账 |
| `_exchange_sync_loop` | 每5分钟 | 交易所条件单状态同步 |
| `_health_audit_loop` | 每15分钟 | 全账号健康审计 |
| `_macro_collection_loop` | 每2小时 | 宏观数据采集 |
| `_journal_recovery_loop` | 每30分钟 | In-flight journal 幽灵仓位检测 |
| `_close_retry_loop` | 每5分钟 | 平仓失败主动重试 |
| `_daily_tasks_loop` | 每分钟检查 | 日报(08:00)/归档(00:01)/周优化(周一09:00) |

**启动序列**（`_run_startup_sequence()`）：
1. 事件系统初始化（EventBus + YAML 配置注入）
2. runtime_config 首次加载
3. Journal 恢复（幽灵仓位检测）
4. 风控对账（所有账户）
5. 配置一致性校验（ERROR → SAFE_MODE）
6. 快速预筛 WebSocket 启动
7. TG Bot 启动

**Fallback 机制**：新引擎任何异常自动回退旧路径（`altcoin_scanner.py`）。

### 3.2 实时监控器 (`realtime_monitor.py`)

通过 Binance WebSocket 7x24 小时监控持仓价格，触及止盈止损位时 ~100ms 延迟执行平仓。

**内存快照机制**（v2.1 优化）：
- 每30秒从 trades.json 提炼"触发阈值"到内存 dict
- WS tick 回调只做内存快速判断（零磁盘IO）
- 只有价格进入触发区间才抢锁做完整 evaluate_trade

**价格事件队列**（B6 优化）：
- WS 回调只往 dict 里存最新价格（O(1)，永不阻塞）
- 独立 worker 线程消费，避免平仓动作卡住 WS 消息处理

**断线降级**（H5 修复）：
- 阶段1（30s）：WS 断线 → 立即启动 REST 轮询（持仓不裸奔）
- 阶段2（5min）：仍然断 → TG 升级告警 + 继续尝试重连
- WS 恢复后自动退出降级模式

### 3.3 Dashboard (`dashboard.py`)

Flask + SocketIO 实时仪表盘，含管理面板。

**子模块拆分**（M1 修复，从 1535 行降到 905 行）：
- `dashboard_app/auth.py` — Token 认证
- `dashboard_app/data.py` — 数据读取
- `dashboard_app/events.py` — 事件抽取 + mtime 缓存
- `dashboard_app/live_prices.py` — 实时价格推送
- `dashboard_app/etag.py` — ETag/Last-Modified 帮手

---


## 4. 多策略框架

### 4.1 架构设计

```
strategies/
├── base.py              # BaseStrategy 抽象 + Signal/Candidate/TradeContext DTO
├── registry.py          # StrategyRegistry(单例) + StrategyEngine(调度)
├── short_overbought/    # RSI 超买做空策略
│   ├── __init__.py      # 导出 strategy_class
│   └── strategy.py      # ShortOverboughtStrategy 实现
├── long_oversold/       # RSI 超卖做多策略
│   ├── __init__.py
│   └── strategy.py
└── prepump_sniffer/     # Pre-pump 做多策略
    ├── __init__.py
    └── strategy.py
```

### 4.2 BaseStrategy 接口

所有策略必须继承 `BaseStrategy` 并实现：

| 方法 | 职责 | 调用频率 |
|------|------|---------|
| `scan(data_feed, market)` | 扫描市场 → 候选列表 | 每小时 |
| `confirm(candidate, data_feed)` | 确认候选 → 开仓信号 | 每5分钟 |
| `evaluate_exit(trade, data_feed)` | 评估持仓 → 平仓信号 | 实时/每分钟 |
| `get_params()` / `set_params()` | 参数管理 | 按需 |

**策略不负责**：连接交易所、风控检查、下单执行、数据持久化。这些由 Engine 层代理。

### 4.3 StrategyRegistry

- 单例模式管理所有策略实例
- `auto_discover()` 启动时扫描 `strategies/*/` 目录自动注册
- 通过 `config/strategy.yaml` 的 `enabled: false` 禁用策略（不删代码）
- 支持装饰器 `@registry.auto_register` 自动注册

### 4.4 StrategyEngine

调度所有活跃策略的 scan/confirm/exit 循环：
- 错误隔离：单个策略异常不影响其他策略
- 自动注入 `strategy_name` 和 `direction` 到候选/信号对象
- 信号汇总后统一进入风控审批 → 执行路由

### 4.5 内置策略

| 策略 | 方向 | 触发逻辑 | 版本 |
|------|------|---------|------|
| `short_overbought` | SHORT | 日线RSI≥75 + 4h RSI回落/弃盘点 | 5.0 |
| `long_oversold` | LONG | 日线RSI≤25 + 反弹确认 | 1.0 |
| `prepump_sniffer` | LONG | OI异动 + 资金费率回归 + 成交量预热 | 1.0 |

### 4.6 数据传输对象 (DTO)

- **Signal**: 策略产生的开仓信号（含 score/stake/止盈止损参数/metadata）
- **ExitSignal**: 平仓信号（含 reason/close_ratio/pnl_estimate）
- **Candidate**: 候选对象（扫描阶段粗筛，需进一步确认）
- **MarketSnapshot**: 市场快照（全市场 tickers）
- **TradeContext**: 持仓上下文（评估退出时传给策略）
- **DataFeed**: 数据馈送抽象接口（实盘/回测各自实现）

---


## 5. 信号评分系统

### 5.1 统一入口 (`scoring/__init__.py`)

S3 修复后，唯一对外评分入口为 `scoring.score_signal()`。内部按优先级自动 fallback：

```
ml (XGBoost) → multifactor (IC加权) → linear (4×25硬编码)
```

配置项 `SCORING_BACKEND`（默认 `'auto'`）控制全局偏好。

### 5.2 Linear 评分 (`signal_score.py`)

经典 4×25 评分维度（满分100）：

| 维度 | 分值 | 计算逻辑 |
|------|------|---------|
| RSI 强度 | 0~25 | RSI 越高分越高 |
| 妖币评分 | 0~25 | yao_score 0/1/2/3 → 0/8/16/25 |
| 触发方式 | 0~25 | 弃盘点25 > 4h RSI回落15 |
| 热度指标 | 0~25 | OI + 资金费率 + BTC趋势 |

额外 Bonus：OKX 交叉验证 (+8)、鲸鱼预警、情绪指标、量价背离。

### 5.3 多因子评分 (`signals/factor_scorer.py`)

基于 30+ 量化因子的 IC 加权评分：

**因子分类**（`signals/factors.py`）：
1. **动量** (8个): RSI_14, RSI_7, ROC, Williams%R, CCI, MACD, Stochastic
2. **成交量** (6个): OBV slope, volume ratio, VWAP deviation, MFI, A/D, divergence
3. **波动率** (6个): ATR, Bollinger bandwidth, realized vol, Keltner, range ratio
4. **趋势** (5个): ADX, Aroon, EMA cross, linear regression, Supertrend
5. **微观结构** (5个): 资金费率z-score, OI变化率, OI-价格背离, 大单失衡, 清算压力

所有因子通过 `FactorRegistry` 统一管理，支持：
- z-score 滚动标准化
- 按类别过滤计算
- 外部数据注入（资金费率、OI 等链上数据）

### 5.4 ML 评分 (`ml/scorer.py`)

XGBoost 概率模型（实验阶段）：
- `ml/features.py` — 特征工程
- `ml/dataset.py` — 训练数据集构建
- `ml/model.py` — 模型训练/加载
- `ml/ab_test.py` — A/B 测试框架
- `ml/models/` — 序列化模型文件

### 5.5 仓位分级

| 评分范围 | 评级 | 仓位 |
|---------|------|------|
| ≥70 分 | A | 全仓 (DEFAULT_STAKE) |
| 40~69 分 | B | 半仓 (DEFAULT_STAKE × 0.5) |
| <40 分 | SKIP | 跳过不开仓 |

---

## 6. 风控体系

### 6.1 单笔风控 (`risk_control.py`)

**多账户隔离** (v3.0)：每个账户拥有独立的 `RiskState`。

核心检查（`can_open_trade()`）：
1. **SAFE_MODE 检查** — 配置 ERROR 触发全局禁止开仓
2. **暂停状态** — 连亏触发的暂停（含过期自动解除）
3. **单日最大亏损** — `RISK_MAX_DAILY_LOSS`（默认 30U）
4. **单日最大开仓** — `RISK_MAX_DAILY_TRADES`（默认 3笔，支持方向子限额）
5. **持仓占比** — `total_open_stake + new_stake ≤ 已实现余额 × 50%`
6. **Portfolio VaR** — 尾部风险检查（锁外执行，降级为警告）

**冷却期**（`is_in_cooldown()`）：
- 同一币种止损后 24h 内两个方向都不开（避免反向伪突破）
- `COOLDOWN_SCOPE='global'`：任一账户止损 → 全局冷却
- `COOLDOWN_SCOPE='per_account'`：仅当前账户冷却

**对账**（`reconcile_risk_state()`）：
- 从 trades 文件反算真实 daily_loss / daily_trades / total_open_stake
- 与 risk_state.json 对比，有偏差自动修正 + TG 告警

### 6.2 组合风控 (`risk/portfolio.py`)

超越单笔的整体暴露监控：

| 检查 | 阈值 | 说明 |
|------|------|------|
| 总暴露 | ≤本金80% | 同一时刻所有持仓保证金之和 |
| 单笔集中度 | ≤本金40% | 单笔保证金不超过账户余额的40% |
| 板块暴露 | 同板块≤2笔 | meme/inscription/ai/governance 分类 |
| 相关性 | <0.85 | 基于收益率相关系数，超限拒绝 |
| Kelly 仓位 | 半Kelly | 基于历史胜率和盈亏比计算最优仓位 |

### 6.3 VaR 风控 (`risk/portfolio_var.py`)

组合层面的尾部风险度量，置信度 95%，限制组合 VaR 不超过本金 20%。

### 6.4 ATR 动态止损 (`risk/atr_stop.py`)

根据 ATR 计算动态硬止损百分比（替代固定 5%），持久化到 `trade.hard_stop_pct`。

### 6.5 宏观过滤 (`macro/filter.py`)

- BTC 24h 跌幅 > 5% → 暂停做空山寨（反弹容易打止损）
- BTC 24h 涨幅 > 8% → 信号加分（牛市山寨更容易冲高回落）
- 支持 stake 乘数和 score bonus 动态调节

---


## 7. 交易所管理与执行

### 7.1 交易所管理 (`exchange_manager.py`)

**统一工厂** `make_exchange()`：所有 ccxt 实例必须通过此工厂创建，保证 timeout 必填（默认 8000ms）。

**实例管理**：
- 公共数据实例：无认证单例（节省资源，读行情/费率）
- 认证实例：按 `(exchange_name, account_id)` 缓存，支持多账户并行

**凭证优先级**：`admin_secrets(指定账户)` > `admin_secrets(活跃账户)` > `.env 环境变量`

**交叉验证**：
- `cross_validate_funding()` — Binance vs OKX 费率对比
- `cross_validate_oi()` — 双所 OI 变化对比
- `cross_validate_price()` — 价格偏差检查（>2% 拒绝开仓）
- `find_cross_exchange_arb_opportunities()` — 跨所费率套利机会发现

**品种覆盖**：`okx_has_swap()` 带 4h TTL 缓存，检查 OKX 是否有某币永续合约。

### 7.2 实盘执行 (`live_executor.py`)

**支持模式**：

| LIVE_MODE | OKX_LIVE_MODE | 效果 |
|-----------|---------------|------|
| False | False | 影子交易（纸上模拟） |
| True | False | 仅 Binance 实盘 |
| False | True | 仅 OKX 实盘 |
| True | True | 按 PRIMARY_EXCHANGE 路由 |

**关键特性**：
- **幂等键**: `client_order_id` (Binance) / `clOrdId` (OKX) 防重复下单
- **滑点告警**: 成交均价 vs ticker 偏差 > `SLIPPAGE_ALERT_PCT` 触发 TG 告警
- **reduceOnly 平仓**: 强制 `reduceOnly=True`，计算错误也不会反向开仓
- **数量精度**: `_amount_to_precision()` 自动裁剪到交易所 stepSize
- **失败不污染**: 下单失败不调 `record_trade_opened()`，无幽灵亏损

**统一接口**：
- `execute_open(symbol, direction, stake, exchange_name)` — 路由到对应交易所
- `execute_close(symbol, direction, amount, exchange_name)` — 统一平仓
- `check_live_balance()` / `check_okx_balance()` — 余额查询

**错误码**：
- `EXCHANGE_UNAVAILABLE` — 连接不可用
- `INVALID_QUANTITY` — 数量裁剪后为 0
- `NETWORK_TIMEOUT` — 网络超时
- `RATE_LIMITED` — 触发限速
- `AUTH_FAILED` — 鉴权失败
- `EXCHANGE_FILTER_REJECTED` — 交易所过滤器拒绝
- `INSUFFICIENT_MARGIN` — 保证金不足
- `EXCHANGE_ERROR` — 其他未归类错误

### 7.3 高级执行 (`execution/`)

- `executor.py` — 执行器抽象（含重试逻辑）
- `smart_order.py` — 智能委托（冰山单/TWAP 拆单）
- `orderbook_monitor.py` — 订单簿深度监控（WS，max_symbols=50）
- `ws_order.py` — WebSocket 下单（低延迟路径）

---

## 8. 实时监控

### 8.1 WebSocket 监控器 (`BinanceWSMonitor`)

- 连接 `wss://stream.binance.com:9443` 的 miniTicker 流
- 每30秒检查持仓变化，自动更新订阅列表
- 消息解析错误累计超阈值(50次) → TG 告警

### 8.2 内存快照优化

| 字段 | 缓存 | 说明 |
|------|------|------|
| 止盈止损阈值 | 每30s刷新 | tp1/tp2/hard_stop/trail_stop |
| best_pnl_pct | 批量flush | 纯统计字段，不阻塞判断 |
| 跨进程同步 | mtime比对 | 每秒最多1次 stat，检测其他进程修改 |

### 8.3 鲸鱼预警 (`signals/whale_alert.py`)

检测大额转账/大单交易，作为信号加分项。

### 8.4 快速预筛 (`hot_scanner.py`)

全市场 WebSocket 标记热门币种，scan_loop 优先处理这些币。

---


## 9. 数据层与持久化

### 9.1 双写兼容层 (`db/compat.py`)

**DB_WRITE_MODE** 三态配置：

| 模式 | 写DB | 写JSON | 适用场景 |
|------|------|--------|---------|
| `dual` | ✓ | ✓ | **默认**，向后兼容老 dashboard |
| `db-canonical` | ✓ | ✗ | 单一真源，JSON 仅周期 export |
| `json-only` | ✗ | ✓ | 兜底，无 SQLAlchemy 时也能跑 |

读路径：DB 优先 → JSON fallback。

### 9.2 SQLAlchemy 模型 (`db/models.py`)

ORM 模型定义，对应 `altcoin_shadow_trades` / `candidates` / `risk_state` 表。

### 9.3 数据库迁移 (`alembic/`)

- `alembic/versions/2026_05_25_001_initial_schema.py` — 初始 schema
- `alembic/versions/2026_05_26_002_candidate_strategy_direction.py` — 候选增加策略方向字段

运行迁移：`alembic upgrade head`

### 9.4 仓储层 (`db/repositories.py`)

提供 `TradeRepository` / `CandidateRepository` 等高层 CRUD 接口。

### 9.5 数据模型 (`models.py`)

**Trade** 核心字段：

| 字段 | 类型 | 语义 |
|------|------|------|
| `stake` | float | 开仓保证金（不变） |
| `stake_remaining` | float | 当前剩余仓位保证金（TP1后减半） |
| `tp1_locked_pnl` | float | TP1 已锁定盈利（独立记账） |
| `pnl` | float | **剩余仓位**的盈亏（不含 tp1_locked_pnl） |
| `total_realized_pnl` | property | `tp1_locked_pnl + pnl`（总盈亏） |
| `close_type` | CloseType | 机器可读关闭原因枚举 |
| `exchange` | str | `'binance'` / `'okx'` / `'shadow'` |
| `account_id` | str | 所属账户 ID |
| `client_order_id` | str | 幂等键 |

**CloseType** 枚举：

| 值 | 含义 | 触发冷却 |
|---|---|---|
| `hard_stop` | 硬止损 | ✅ |
| `trail_stop` | 移动止损 | ✅ |
| `time_stop` | 时间止损 | ✅ |
| `breakeven_stop` | TP1后保本止损 | ❌ |
| `tp2` | TP2 全仓止盈 | ❌ |
| `manual` | 手动平仓 | ❌ |

### 9.6 并发安全

所有 JSON 文件读写通过 `common.LockedJsonFile`：
- fcntl 排他锁（默认 10s 超时）
- 原子写（写临时文件 → rename）
- Windows 兼容 shim（`msvcrt.locking` + `os.lseek(0)`）

**锁顺序规则**：
1. 持锁读 trades.json
2. 修改内存 Trade 对象
3. 持锁原子写回
4. **出锁后** 才执行 `record_trade_closed()` / `send_tg()`

---

## 10. 事件总线

### 10.1 架构 (`event_bus.py`)

发布-订阅模式，解耦 scheduler / realtime_monitor / dashboard。

**后端自动选择**：
- Redis 可用 → `RedisBackend`（跨进程/跨机器，Pub/Sub）
- Redis 不可用 → `InMemoryBackend`（单进程，线程安全）

### 10.2 事件类型

| Channel | 触发时机 |
|---------|---------|
| `trade.opened` | 开仓成功 |
| `trade.closed` | 平仓完成 |
| `trade.updated` | 持仓字段更新 |
| `candidate.added` | 新候选加入 |
| `candidate.triggered` | 候选确认触发 |
| `risk.alert` | 风控告警 |
| `risk.state_changed` | 风控状态变更 |
| `signal.scored` | 信号评分完成 |
| `system.health` | 系统健康状态 |
| `config.changed` | 运行时配置变更 |

### 10.3 使用方式

```python
from event_bus import get_event_bus, emit_trade_opened

bus = get_event_bus()
bus.subscribe('trade.*', on_trade_event)  # 通配符订阅
emit_trade_opened(trade_id='...', symbol='PEPE/USDT', ...)
```

---


## 11. 配置管理

### 11.1 四级配置层叠

优先级从高到低：

| 层级 | 来源 | 修改方式 |
|------|------|---------|
| 1. 环境变量 | `os.environ` | `.env` 文件或 Docker 环境 |
| 2. runtime_config | `runtime_config.json` | Admin Panel 实时修改 |
| 3. YAML | `config/*.yaml` | 项目级默认值 |
| 4. 代码默认 | `config/_defaults.py` | 最低优先级兜底 |

### 11.2 统一查询 API (S5 修复)

```python
from config import resolve, explain

# 直接拿值
stake = resolve('DEFAULT_STAKE')                    # → 33

# 拿值 + 来源
stake, src = resolve('LEVERAGE', with_source=True)  # → (10, 'yaml')

# 完整透视
print(explain('TP1_MULTIPLIER'))
# {'final_value': 0.95, 'final_source': 'yaml', 'layers': {...}}
```

### 11.3 YAML 配置文件

| 文件 | 内容 |
|------|------|
| `config/system.yaml` | 账户参数、交易所配置、调度周期、回测参数 |
| `config/strategy.yaml` | 各策略参数（扫描/RSI/止盈止损/评分） |
| `config/risk.yaml` | 风控全局/账户级参数 |

### 11.4 关键参数一览

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `DEFAULT_STAKE` | 33 | 单笔保证金 (USDT) |
| `LEVERAGE` | 10 | 杠杆倍数 |
| `TP1_MULTIPLIER` | 0.95 | SHORT TP1 = 入场×0.95 (-5%) |
| `TP2_MULTIPLIER` | 0.92 | SHORT TP2 = 入场×0.92 (-8%) |
| `HARD_STOP_LOSS_PCT` | 5.0 | 硬止损百分比 |
| `TRAIL_STOP_RETRACE_RATIO` | 0.4 | 移动止损回撤比例 |
| `MAX_HOLD_DAYS` | 1 | 最大持仓天数 |
| `RISK_MAX_DAILY_LOSS` | 30 | 日亏上限 (U) |
| `RISK_MAX_DAILY_TRADES` | 3 | 日开仓上限 |
| `COOLDOWN_HOURS` | 24 | 止损后冷却期 |
| `CANDIDATE_EXPIRE_HOURS` | 12 | 候选池超时 |

### 11.5 仓位模式 (v5.1)

| 模式 | `POSITION_MODE` | 行为 |
|------|----------------|------|
| 手动 | `'manual'` | 金额参数用手填值 |
| 比例 | `'proportional'` | 按 `实际余额/100` 自动缩放 4 个金额字段 |

比例模式缩放字段：`DEFAULT_STAKE`、`RISK_MAX_DAILY_LOSS`、`COMPOUND_STEP`、`COMPOUND_INCREASE`。  
不缩放：`COMPOUND_MAX_STAKE`、杠杆、止盈止损百分比、RSI 阈值。

### 11.6 配置热加载

`_config_reload_loop` 每 30 秒调用 `runtime_config.apply_overrides()`，Admin Panel 修改后 30 秒内所有进程生效。

---

## 12. 回测子系统

### 12.1 统一入口 (M2 修复)

```python
from backtesting import run, UnifiedBacktestResult

result = run('PEPE/USDT', days=90)                    # 默认 legacy 引擎
result = run('PEPE/USDT', days=90, engine='vectorized', ohlcv_df=df)
```

### 12.2 模块结构

| 模块 | 职责 |
|------|------|
| `backtesting/__init__.py` | 统一入口 + `UnifiedBacktestResult` |
| `backtesting/engine.py` | 向量化回测引擎 (`VectorizedBacktester`) |
| `backtesting/strategy_runner.py` | 事件驱动策略运行器 |
| `backtesting/metrics.py` | 性能指标计算（Sharpe/MDD/PF） |
| `backtesting/slippage.py` | 滑点模拟模型 |
| `backtesting/monte_carlo.py` | 蒙特卡洛模拟（路径随机化） |
| `backtesting/walk_forward.py` | Walk-Forward 优化 |
| `backtesting/stress_test.py` | 压力测试（极端行情模拟） |
| `backtesting/parallel.py` | 多进程并行回测 |
| `backtesting/factor_ic.py` | 因子 IC 分析 |
| `backtest.py` | 旧版入口（事件驱动，含全特性） |

### 12.3 回测参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `BACKTEST_SLIPPAGE_PCT` | 0.1 | 每笔交易滑点 % |
| `BACKTEST_FEE_PCT` | 0.04 | Taker 手续费(每边) % |
| `BATCH_BACKTEST_DAYS` | 90 | 批量回测默认天数 |
| `BATCH_CORRELATION_THRESHOLD` | 0.7 | 高相关性币对避免同时开仓 |

### 12.4 参数优化 (`optimization/`)

- `optimizer.py` — Optuna 贝叶斯优化框架
- `cython_paths.py` — 关键路径 Cython 加速（可选）

---


## 13. 宏观数据与过滤

### 13.1 模块结构 (`macro/`)

| 模块 | 职责 |
|------|------|
| `collector.py` | 宏观数据采集（BTC走势/恐贪指数/DXY） |
| `filter.py` | 宏观环境过滤（是否允许开仓、stake 乘数调节） |
| `ms_runner.py` | 宏观策略运行器 |
| `sources/` | 数据源适配器 |

### 13.2 过滤逻辑

`check_macro_filter()` 返回：
- `allowed: bool` — 是否允许开仓
- `reason: str` — 暂停原因
- `stake_multiplier: float` — 仓位调节系数（如 0.5 = 减半）
- `score_bonus: int` — 评分额外加减分

---

## 14. 机器学习模块

### 14.1 模块结构 (`ml/`)

| 模块 | 职责 |
|------|------|
| `features.py` | 特征工程（从 OHLCV + 链上数据构建特征矩阵） |
| `dataset.py` | 训练数据集构建（标注正负样本） |
| `model.py` | XGBoost 模型训练 / 加载 / 推理 |
| `scorer.py` | ML 评分器（输出概率 → 转换为 0~100 评分） |
| `ab_test.py` | A/B 测试框架（ML vs Linear 对照实验） |
| `models/` | 序列化模型文件（.joblib/.pkl） |

### 14.2 使用条件

- 需要安装 `xgboost`、`scikit-learn`
- 模型文件不存在时自动 fallback 到 linear 评分
- 通过 `SCORING_BACKEND='ml'` 强制使用（模型不可用则降级）

---

## 15. Dashboard 与管理面板

### 15.1 Web 仪表盘

- **路由**: Flask + SocketIO (Eventlet)
- **实时推送**: 价格更新通过 WebSocket 推送到前端
- **API 端点**: `/api/trades`、`/api/candidates`、`/api/risk`、`/api/events`
- **ETag 缓存**: trades.json mtime 未变时直接返回 304

### 15.2 管理面板 (Admin Panel)

**8 层安全防御**：

| 层 | 防御 |
|----|------|
| L1 | IP 白名单 (`ADMIN_ALLOWED_IPS`) |
| L2 | Secret URL 前缀（32+ 字符随机串） |
| L3 | IP 失败锁定（5次失败 → 锁30分钟，返回404） |
| L4 | 双因子（PBKDF2-SHA256 密码 + TOTP） |
| L5 | Session（30min 空闲/4h 绝对过期） |
| L6 | CSRF（所有 POST 带 `X-Admin-CSRF` 头） |
| L7 | 审计日志 + TG 告警 |
| L8 | 响应头（noindex/DENY/CSP/无缓存） |

**面板功能**：
- 实盘开关切换（LIVE_MODE / OKX_LIVE_MODE / PRIMARY_EXCHANGE）
- API 凭证管理（存 `admin_secrets.json`，0600 权限）
- 风控参数调整（含硬上下界校验）
- 止盈止损参数修改
- 配置一致性硬阻塞（`validate_cross_field_consistency`）

---

## 16. 监控与可观测性

### 16.1 Prometheus (`monitoring/prometheus.py`)

暴露 `/metrics` 端点，指标包括：
- `trades_opened_total` — 开仓总数（按策略/方向/交易所分）
- `trades_closed_total` — 平仓总数（按原因分）
- `pnl_total` — 累计盈亏
- `risk_daily_loss_gauge` — 当日亏损
- `ws_disconnect_total` — WS 断线次数
- `task_duration_seconds` — 任务耗时直方图

### 16.2 任务指标 (`task_metrics.py`)

记录每个调度任务的：执行时长、成功/失败次数、最后执行时间。

### 16.3 健康检查 (`health_check.py` / `health_audit.py`)

- 基础健康检查：进程存活、文件可读写、Redis 连通
- 深度审计：全账号风控状态一致性、交易所 API 连通性

### 16.4 TG 通知

所有关键事件通过 Telegram Bot 推送：
- 开仓/平仓通知
- 风控告警（日亏达限/连亏/暂停）
- WebSocket 断线告警
- 配置变更通知
- SAFE_MODE 激活告警

---


## 17. 部署架构

### 17.1 Docker Compose 服务

```yaml
services:
  redis:         # Redis 7 (事件总线 + 缓存)
  dashboard:     # Flask + SocketIO 仪表盘
  scheduler:     # AsyncStrategyEngine 主调度
  realtime-monitor:  # WebSocket 实时止盈止损
```

**共享机制**：
- 数据文件通过 bind mount 共享（trades/candidates/risk_state）
- 进程间通信通过 Redis Pub/Sub
- 配置热加载通过 runtime_config.json

### 17.2 Docker 镜像

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENTRYPOINT ["/app/docker-entrypoint.sh"]
```

### 17.3 启动脚本 (`docker-entrypoint.sh`)

1. 确保 JSON 数据文件存在（自愈：空目录 → rmdir → 写默认值）
2. `python3 -m compileall` 语法校验（防定时任务因语法错误静默失效）
3. 敏感文件权限加固（`.env`/`admin_secrets.json` → chmod 600）

### 17.4 快速部署

```bash
# 1. 克隆仓库
git clone https://github.com/Jeffrey-done/altcoin-shadow-system.git
cd altcoin-shadow-system

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 TG_BOT_TOKEN / TG_CHAT_ID

# 3. 初始化数据文件
for f in altcoin_shadow_trades altcoin_candidates risk_state altcoin_trades_archive weekly_report; do
  [ -f "./${f}.json" ] || echo '[]' > "./${f}.json"
done
echo '{}' > ./risk_state.json
echo '{}' > ./weekly_report.json
mkdir -p ./backtest_cache

# 4. 启动
docker-compose up -d

# 5. 查看日志
docker-compose logs -f scheduler
```

### 17.5 手动启动（开发环境）

```bash
pip install -r requirements.txt
cp .env.example .env

# 启动主调度器（自动启动 hot_scanner + tg_bot 线程）
python3 async_engine.py

# 独立启动实时监控器
python3 realtime_monitor.py

# 独立启动仪表盘
python3 dashboard.py --port 8080
```

### 17.6 CI/CD (`.github/workflows/tests.yml`)

每次 push / PR 自动运行：
- `ruff check .` — 代码风格检查
- `pytest tests/ --cov=. --cov-fail-under=70` — 测试 + 覆盖率门禁

---

## 18. 运维手册

### 18.1 日常运维

| 操作 | 命令 / 方式 |
|------|------------|
| 查看服务状态 | `docker-compose ps` |
| 查看日志 | `docker-compose logs -f scheduler` |
| 重启单个服务 | `docker-compose restart scheduler` |
| 热加载配置 | 通过 Admin Panel 修改（30s 生效） |
| 手动扫描 | `python3 altcoin_scanner.py scan` |
| 手动确认 | `python3 altcoin_scanner.py check` |
| 查看风控状态 | TG `/risk` 指令 |
| 查看持仓 | TG `/positions` 指令 |

### 18.2 数据备份

```bash
# 定时备份（建议每日 cron）
cp altcoin_shadow_trades.json backups/trades_$(date +%Y%m%d).json
cp risk_state.json backups/risk_$(date +%Y%m%d).json
cp admin_secrets.json backups/secrets_$(date +%Y%m%d).json
```

### 18.3 升级流程

```bash
# 1. 停服
docker-compose down

# 2. 备份
cp altcoin_shadow_trades.json altcoin_shadow_trades.json.bak

# 3. 拉代码
git pull

# 4. 重建镜像
docker-compose build

# 5. 运行迁移（如有）
docker-compose run --rm scheduler alembic upgrade head

# 6. 启动
docker-compose up -d
```

### 18.4 SAFE_MODE 处理

当启动配置校验发现 ERROR 时，系统进入 SAFE_MODE（禁止所有开仓）：

1. 查看 TG 告警确认错误原因
2. 通过 Admin Panel 修复配置
3. 在 Admin Panel 清除 SAFE_MODE 标记
4. 或删除 `.safe_mode.json` 文件后重启

### 18.5 幽灵仓位处理

启动时 `journal_recovery.recover_inflight()` 检测到 pending 条目：

1. 查看 TG 告警中的订单详情
2. 登录交易所确认订单是否成交
3. 如已成交：手动在 trades.json 补录
4. 如未成交：删除 `trades_inflight.json` 中的对应条目

### 18.6 平仓失败处理

`close_retry_loop` 会每5分钟自动重试，超过12次（1h）升级 critical 告警：

1. 查看 TG 告警中的失败原因
2. 登录交易所手动平仓
3. 在 trades.json 中将该笔标记为 `status: closed`

### 18.7 诊断工具 (`tools/`)

| 工具 | 用途 |
|------|------|
| `tools/diagnose.py` | 系统诊断（文件状态/API连通/配置一致） |
| `tools/diagnose_dashboard.py` | Dashboard 专项诊断 |
| `tools/diagnose_timeout.py` | 超时问题排查 |
| `tools/config_lint.py` | 配置文件语法/一致性检查 |
| `tools/migrate_v41.py` | v4.0→v4.1 数据迁移 |
| `tools/backtest_multifactor.py` | 多因子回测脚本 |
| `tools/backtest_prepump.py` | Pre-pump 策略回测 |

---


## 19. 数据文件清单

| 文件 | 格式 | 用途 | Git |
|------|------|------|-----|
| `altcoin_shadow_trades.json` | Array | 所有交易记录（开仓+平仓） | ❌ |
| `altcoin_candidates.json` | Object | 候选池（多策略） | ❌ |
| `risk_state.json` | Object(v2) | 风控状态（按账号隔离） | ❌ |
| `trades_inflight.json` | Array | In-flight journal（下单意图） | ❌ |
| `altcoin_trades_archive.json` | Array | 30天前的归档交易 | ❌ |
| `weekly_report.json` | Object | 最近周报数据 | ❌ |
| `runtime_config.json` | Object | admin 面板运行时覆盖 | ❌ |
| `admin_secrets.json` | Object | 加密 API 凭证 (0600) | ❌ |
| `admin_audit.log` | Text | 管理面板操作审计日志 | ❌ |
| `.admin_ratelimit.json` | Object | IP 失败锁定状态 | ❌ |
| `.safe_mode.json` | Object | SAFE_MODE 标记文件 | ❌ |
| `backtest_cache/` | Dir | 回测 K 线缓存 | ❌ |

---

## 20. 故障排查

### 20.1 常见问题

| 症状 | 可能原因 | 解决方案 |
|------|---------|---------|
| 不开仓 | SAFE_MODE 激活 | 检查 TG 告警，修复配置，清除标记 |
| 不开仓 | 日亏达限 | 等明天自动重置，或通过 admin 调大 |
| 不开仓 | 连亏暂停中 | 等暂停到期（默认 24h） |
| 不开仓 | 候选池为空 | 检查 RSI 阈值是否过高、市场是否冷清 |
| 止损不触发 | WS 断线 | 检查网络；已有 REST 降级兜底 |
| 平仓失败 | API 权限不足 | 检查 Binance/OKX API key 权限 |
| 启动报错 | 数据文件是目录 | docker-entrypoint.sh 自动修复 |
| 风控数字偏差 | 并发写入竞争 | reconcile_loop 每10分钟自动修正 |

### 20.2 日志位置

- Docker: `docker-compose logs -f <service>`
- 手动: 标准输出（配合 `LOG_LEVEL` 环境变量）
- 审计: `admin_audit.log`

### 20.3 Smoke Test (`smoke_test.py`)

快速验证系统核心模块可正常导入和基础功能：

```bash
python3 smoke_test.py
```

---

## 21. 安全模型

### 21.1 凭证管理

| 存储 | 内容 | 权限 |
|------|------|------|
| `.env` | TG Token / 基础 API Key | 0600 |
| `admin_secrets.json` | 加密的交易所凭证 + TOTP 种子 | 0600 |
| `runtime_config.json` | 运行时配置覆盖 | 0600 |

### 21.2 网络安全

- Dashboard 绑定 `127.0.0.1`（不直接暴露公网）
- Redis 绑定 `127.0.0.1:6379`
- 必须通过反向代理（nginx/caddy）+ HTTPS 对外服务
- Admin URL Secret 在 access log 中脱敏（参见 README NF-2 runbook）

### 21.3 实盘安全

- **幂等键**：所有下单带 `client_order_id`，网络重试不重复下单
- **reduceOnly**：平仓订单强制 `reduceOnly=True`
- **失败不污染**：下单失败不改 risk_state
- **Journal 反查**：启动时检测幽灵仓位，推 TG 告警

### 21.4 反向代理配置

参见 README.md 中的"反向代理 access log 脱敏"章节，含 nginx/caddy/Cloudflare 配置示例。

---

## 22. 版本历史

### v5.x（当前）— 多策略 + 系统稳定性强化

- 多策略架构正式回归：short_overbought + long_oversold + prepump_sniffer
- AsyncStrategyEngine v2.0（asyncio + aiohttp 并发 IO）
- S2: 调度器统一入口 (`scheduler.py` → `async_engine`)
- S3: 评分系统统一 (`scoring.score_signal()`)
- S4: DB 写模式可配 (`DB_WRITE_MODE`)
- S5: 4级配置统一查询 (`config.resolve()`)
- M1: Dashboard 拆分（1535→905行）
- M2: 回测引擎统一入口
- H1: TP1 半仓释放幂等（`tp1_stake_released` 标记）
- H2: 开仓前 journal 预扫（防重复下单）
- H4: 平仓失败重试 worker
- H5: WS 断线 30s 降级 REST 轮询
- 事件总线 (Redis Pub/Sub + InMemory fallback)
- 多账户独立风控 (v3.0)
- 组合风控 (correlation/Kelly/VaR)
- 30+ 量化因子库
- Prometheus 监控端点

### v4.1 — 核心 Bug 修复

- TP1 双计数 bug 修复（`trade.pnl` 语义从"合计"改为"剩余仓位"）
- `CloseType` 枚举替代字符串匹配
- 风控参数回退（`RISK_MAX_POSITION_PCT` 0.9→0.5）
- realtime_monitor 内存快速过滤
- Docker 代码打入镜像（不再 bind mount 源码）
- RSI 计算丢弃最后一根未收盘 K 线

### v4.0 — 多策略版本（已精简）

- funding_arb + low_risk + long_scanner（后被 v4.1 移除）

---

## 附录 A: 文件结构总览

```
altcoin-shadow-system/
├── async_engine.py          # 异步策略引擎 v2.0（主入口）
├── scheduler.py             # 薄壳入口（委托 async_engine）
├── realtime_monitor.py      # WebSocket 实时止盈止损
├── dashboard.py             # Flask 仪表盘
├── altcoin_scanner.py       # 全市场扫描 + 候选确认（旧路径 fallback）
├── altcoin_tracker.py       # 持仓追踪 + 止盈止损评估
├── engine_adapter.py        # 新/旧引擎桥接
├── risk_control.py          # 风控模块（多账户隔离）
├── live_executor.py         # 实盘下单执行器
├── exchange_manager.py      # 交易所管理（Binance + OKX）
├── signal_score.py          # 线性信号评分
├── common.py                # 公共工具（日志/TG/锁/journal）
├── models.py                # 数据模型（Trade/Candidate）
├── event_bus.py             # 事件总线（Redis/InMemory）
├── event_integration.py     # 事件系统集成胶水
├── runtime_config.py        # 运行时配置管理
├── safe_mode.py             # SAFE_MODE 全局禁止开仓
├── hot_scanner.py           # 快速预筛 WebSocket
├── tg_bot.py                # Telegram Bot 交互
├── auto_optimize.py         # 自动优化建议
├── weekly_report.py         # 策略周报
├── health_check.py          # 健康检查
├── health_audit.py          # 深度健康审计
├── journal_recovery.py      # In-flight journal 反查
├── task_metrics.py          # 任务指标收集
├── admin_panel.py           # Admin Panel Blueprint
├── admin_secrets.py         # 凭证管理
├── backtest.py              # 旧版回测入口
├── walk_forward.py          # Walk-Forward 优化
├── smoke_test.py            # 快速烟雾测试
│
├── strategies/              # 多策略 OOP 框架
├── signals/                 # 因子库 + 多因子评分
├── scoring/                 # 统一评分入口
├── ml/                      # XGBoost ML 评分
├── macro/                   # 宏观数据 + 过滤
├── risk/                    # 组合风控 + VaR + ATR
├── execution/               # 高级执行（智能委托/订单簿）
├── monitoring/              # Prometheus 端点
├── backtesting/             # 回测子系统
├── optimization/            # 参数优化（Optuna）
├── db/                      # 数据库层（SQLAlchemy + JSON兼容）
├── data/                    # 数据馈送抽象
├── config/                  # YAML 配置 + 默认值
├── dashboard_app/           # Dashboard 子模块
├── alembic/                 # DB 迁移
├── tools/                   # 诊断/迁移脚本
├── tests/                   # 测试用例（233+）
├── templates/               # Dashboard HTML
├── static/                  # Dashboard 前端资源
├── docs/                    # 审计报告 + 架构文档
│
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh
├── requirements.txt
├── requirements-dev.txt
├── pyproject.toml
├── .coveragerc
├── .github/workflows/tests.yml
└── .env.example
```

---

## 附录 B: Telegram Bot 指令

| 指令 | 功能 |
|------|------|
| `/status` | 持仓概览 + 风控状态 |
| `/balance` | 账户余额、今日/累计盈亏、胜率 |
| `/positions` | 所有持仓详情 |
| `/candidates` | 候选池列表 |
| `/risk` | 风控状态详情 |
| `/help` | 显示所有可用指令 |

---

## 附录 C: 环境变量

```bash
# 必须
TG_BOT_TOKEN=your_telegram_bot_token
TG_CHAT_ID=your_chat_id

# Binance 实盘（可选）
BINANCE_API_KEY=...
BINANCE_SECRET=...

# OKX 实盘（可选）
OKX_API_KEY=...
OKX_SECRET=...
OKX_PASSPHRASE=...

# Dashboard
DASHBOARD_TOKEN=...
DASHBOARD_SECRET_KEY=...
ADMIN_URL_SECRET=...
ADMIN_ALLOWED_IPS=123.45.67.89

# Redis
REDIS_URL=redis://localhost:6379/0

# 系统
LOG_LEVEL=INFO
DB_WRITE_MODE=dual
USE_NEW_ENGINE=true
```

---

*文档结束 — 如有疑问请参阅源码注释或联系维护者*
