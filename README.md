# 小币种影子做空系统 v5.0

100U 本金杠杆做空 + 风控系统，目标周赚 $100+。

## 架构

```
common.py           ← 公共工具（TG推送、原子写JSON、日志、时间）
config.py           ← 策略参数集中配置（杠杆/止盈/止损/风控）
models.py           ← 数据模型（Trade / Candidate）
risk_control.py     ← 每日风控模块（亏损限制/开仓限制/连亏暂停）
altcoin_scanner.py  ← 做空扫描器：发现候选 + 触发开仓
altcoin_tracker.py  ← 做空追踪器：止盈/止损/日报
exchange_manager.py ← 交易所管理（Binance + OKX数据交叉验证）
realtime_monitor.py ← WebSocket 实时止盈止损监控
scheduler.py        ← 定时任务调度器
dashboard.py        ← Web 仪表盘
weekly_report.py    ← 策略周报
health_check.py     ← 系统健康检查
signal_score.py     ← 信号评分系统
backtest.py         ← 回测引擎
```

## 策略：超买做空

| 项目 | 参数 |
|------|------|
| 本金/单 | 100U 保证金 × 10x = 1000U 名义仓位 |
| TP1 | 价格跌 5% → 锁定 50% 仓位利润（≈25U） |
| TP2 | 价格跌 8% → 全仓平仓（≈40U）（回测优化：10%→8%提升触发率） |
| 硬止损 | 价格反弹 5% → 无条件平仓（亏≈50U）（回测优化：3%→5%减少假突破） |
| 移动止损 | 盈利 ≥3% 后激活，回撤 10% 触发 |
| 时间止损 | 持仓 >24h 且盈利 <3% 强制平 |

**触发条件：**
- 日线 RSI > 80（超买）（回测优化：78→80减少假信号）
- 4h RSI 从峰值回落 ≥10 点 或 1H 弃盘点信号
- 过滤：成交量 >50万U、价格 <1U、24h 涨幅 >10%

## 风控系统

| 规则 | 参数 |
|------|------|
| 单日最大亏损 | 30U（达到后当日停止开仓） |
| 单日最大开仓 | 2次 |
| 连续亏损暂停 | 连亏3次 → 暂停24小时 |
| 最大持仓占比 | 本金的 50%（最多同时1~2单） |

## 使用方法

### 环境准备

```bash
pip install -r requirements.txt
```

### 配置

创建 `.env` 文件：
```
TG_BOT_TOKEN=your_bot_token
TG_CHAT_ID=your_chat_id
```

### 启动

```bash
# ── 做空扫描器 ──
python3 altcoin_scanner.py scan    # 日线扫描（每4小时）
python3 altcoin_scanner.py check   # 候选确认（每1小时）
python3 altcoin_scanner.py both    # 完整流程

# ── 做空追踪器 ──
python3 altcoin_tracker.py                # 检查 + 推送日报
python3 altcoin_tracker.py --check-only   # 只检查止盈/止损

# ── 实时监控 ──
python3 realtime_monitor.py        # WebSocket 实时止盈止损

# ── 仪表盘 ──
python3 dashboard.py               # 启动 Web 仪表盘

# ── 调度器（Docker 模式）──
python3 scheduler.py               # 替代 crontab
```

### 推荐 Crontab

```cron
# ── 做空策略 ──
0 */4 * * *   cd /path && python3 altcoin_scanner.py scan
30 * * * *    cd /path && python3 altcoin_scanner.py check
15 * * * *    cd /path && python3 altcoin_tracker.py --check-only
0 8 * * *     cd /path && python3 altcoin_tracker.py
```

## 参数调优

所有参数集中在 `config.py`：

### 核心参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| ACCOUNT_BALANCE | 100 | 账户总本金（USDT） |
| LEVERAGE | 10 | 做空杠杆倍数 |
| DEFAULT_STAKE | 100 | 单笔保证金 |
| TP1_MULTIPLIER | 0.95 | 第一档止盈（-5%） |
| TP2_MULTIPLIER | 0.92 | 第二档止盈（-8%，回测优化：10%→8%提升触发率） |
| HARD_STOP_LOSS_PCT | 5.0 | 硬止损（+5%无条件平，回测优化：3%→5%减少假突破） |
| TRAIL_STOP_ACTIVATE_PCT | 3 | 移动止损激活门槛 |
| MAX_HOLD_DAYS | 1 | 最大持仓天数 |

### 风控参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| RISK_MAX_DAILY_LOSS | 30 | 单日最大亏损 |
| RISK_MAX_DAILY_TRADES | 2 | 单日最大开仓次数 |
| RISK_CONSECUTIVE_LOSS_PAUSE | 3 | 连亏暂停阈值 |
| RISK_PAUSE_HOURS | 24 | 暂停时长 |
| RISK_MAX_POSITION_PCT | 0.5 | 最大持仓占比（50%） |

## 数据文件

| 文件 | 说明 |
|------|------|
| `altcoin_candidates.json` | 做空候选池 |
| `altcoin_shadow_trades.json` | 交易记录 |
| `risk_state.json` | 风控状态（当日计数/连亏等） |
| `weekly_report.json` | 周报数据 |

> 所有文件使用原子写入，崩溃不会损坏数据。

## 日志

```bash
LOG_LEVEL=DEBUG python3 altcoin_scanner.py both
```

## 风险提示

⚠️ **这是纸上交易/影子交易系统**，用于验证策略。

- 10x 杠杆意味着 5% 止损 = 本金亏 50%，风控系统会在连亏3次后暂停24h
- 100U 本金日赚 20U = 日均 20% 收益率，**不可长期持续**
- 务必先用此系统纸上交易至少2周验证胜率
- 实盘前确认：胜率 >50%、盈亏比 >1.5:1、最大回撤 <50%
