# 🔴 Altcoin Shadow Short System

全自动小币种做空影子交易系统 — 7×24 小时运行，自动扫描超买信号、开仓、止盈止损、风控管理。

## 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        Scheduler (调度器)                         │
│  每小时整点: scan_daily()  │  每小时30分: check_candidates()      │
│  每小时15分: tracker()     │  每天8:00: 日报  │  周一9:00: 优化   │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  [实时] hot_scanner ──→ 标记热门币 ──→ scan_daily 优先处理        │
│  [实时] realtime_monitor ──→ WebSocket 止盈止损（<100ms延迟）      │
│  [实时] tg_bot ──→ Telegram 交互指令                              │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

## 核心流程

```
1. 全市场扫描 → 日线 RSI>80 + 涨幅>10% + 成交量>50万U → 加入候选池
2. 候选确认   → 4h RSI 回落 或 弃盘点信号 → 信号评分
3. 开仓决策   → 评分≥40分 + 风控通过 + 价格确认 + 冷却期检查 → 开仓
4. 持仓管理   → 硬止损/移动止损/分批止盈/时间止损 → 实时监控
5. 风控保护   → 单日亏损上限/最大开仓次数/连亏暂停/持仓占比限制
```

## 策略参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 杠杆 | 10x | 100U 保证金 = 1000U 名义仓位 |
| 硬止损 | 5% | 价格反弹 5% 无条件平仓 |
| TP1 | -5% | 价格跌 5% 锁定 50% 仓位利润 |
| TP2 | -8% | 价格跌 8% 全仓平仓 |
| 移动止损 | 盈利 3% 激活，回撤 10% 触发 | |
| 时间止损 | 24h 持仓且盈利 < 3% 强制平 | |
| 最大持仓 | 余额的 90% | |
| 单日亏损上限 | 30U | 达到后当日停止开仓 |
| 单日最大开仓 | 2 笔 | |
| 连亏暂停 | 连亏 3 次暂停 24h | |

## 文件结构

```
├── scheduler.py           # 定时任务调度器（入口）
├── altcoin_scanner.py     # 全市场扫描 + 候选确认 + 开仓
├── altcoin_tracker.py     # 持仓追踪 + 止盈止损评估 + 日报
├── realtime_monitor.py    # WebSocket 实时止盈止损
├── hot_scanner.py         # 全市场快速预筛（标记热门币）
├── risk_control.py        # 风控模块（冷却期/对账/仓位限制）
├── signal_score.py        # 信号评分系统
├── exchange_manager.py    # 交易所管理（Binance + OKX 交叉验证）
├── tg_bot.py              # Telegram Bot 交互指令
├── dashboard.py           # Web 仪表盘（Flask + SocketIO）
├── auto_optimize.py       # 自动优化建议（周报）
├── health_check.py        # 健康检查
├── weekly_report.py       # 策略周报
├── backtest.py            # 回测引擎
├── config.py              # 所有策略参数集中配置
├── common.py              # 公共工具（日志/TG推送/原子写/锁）
├── models.py              # 数据模型（Trade/Candidate）
├── live_executor.py       # 实盘下单执行器（LIVE_MODE=True 时）
│
├── altcoin_shadow_trades.json   # 交易记录（主数据文件）
├── altcoin_candidates.json      # 候选池
├── risk_state.json              # 风控状态
│
├── templates/             # Dashboard HTML 模板
├── static/                # Dashboard 前端资源
├── tests/                 # 测试用例（45个）
├── Dockerfile             # Docker 构建文件
└── docker-compose.yml     # Docker 编排
```

## 启动方式

### Docker（推荐）

```bash
docker-compose up -d
```

### 手动启动

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入 TG_BOT_TOKEN / TG_CHAT_ID / BINANCE_API_KEY（可选）

# 启动调度器（自动启动所有后台服务）
python3 scheduler.py
```

调度器会自动启动：
- 快速预筛 WebSocket 线程
- TG Bot 轮询线程
- 所有定时任务

### 独立启动 Dashboard

```bash
python3 dashboard.py --port 8080
```

## Telegram Bot 指令

| 指令 | 功能 |
|------|------|
| `/status` | 持仓概览 + 风控状态 |
| `/balance` | 账户余额、今日/累计盈亏、胜率 |
| `/positions` | 所有持仓详情（入场价、浮盈、止损位） |
| `/candidates` | 候选池列表（等待触发的币） |
| `/risk` | 风控状态详情 |
| `/help` | 显示所有可用指令 |

## 风控机制

### 多层防护

1. **开仓前检查**：单日亏损/开仓次数/持仓占比/冷却期
2. **持仓中保护**：硬止损(5%) + 移动止损 + 时间止损(24h) + 保本止损(TP1后)
3. **连亏暂停**：连续亏损 3 次 → 暂停 24h
4. **冷却期**：同一币种止损平仓后 24h 内不再开仓
5. **启动对账**：每次重启自动校验 risk_state 与 trades 一致性
6. **多交易所确认**：开仓前检查 OKX 价格偏差 > 2% 则跳过

### 并发安全

- 所有写入使用 `LockedJsonFile`（fcntl 排他锁）
- 写入顺序：trades 先落盘 → risk_state 后改 → TG 最后推送
- 防止"幽灵亏损"（风控扣了账但交易没记录）

## 信号评分系统

| 维度 | 分值 | 说明 |
|------|------|------|
| RSI 强度 | 0~25 | RSI 越高越强 |
| 妖币评分 | 0~25 | OI涨+资金费率高+涨幅>30% |
| 触发方式 | 0~25 | 弃盘点(25) > 4h RSI 回落(15) |
| 热度指标 | 0~25 | OI + 资金费率 + BTC趋势 + OKX交叉验证 |

- **≥70 分 (A级)**：全仓开仓
- **40~69 分 (B级)**：半仓开仓
- **<40 分**：跳过

## 自动优化

每周一 9:00 UTC 自动分析：
- 止损触发率（过高建议放宽）
- 胜率（过低建议提高 RSI 门槛）
- TP1/TP2 触发率（TP1 高但 TP2 低建议收紧 TP2）

建议通过 TG 推送，不会自动修改参数。

## 定时任务调度

| 时间 | 任务 | 超时 |
|------|------|------|
| 每小时 :00 | 全市场日线扫描 | 10min |
| 每小时 :15 | 持仓止盈止损检查 | 10min |
| 每小时 :30 | 候选池确认 + 开仓 | 10min |
| 每 6h :45 | 健康检查 | 10min |
| 每天 8:00 | 日报推送 | 10min |
| 每天 0:01 | 过期交易归档（>30天） | 10min |
| 每周一 9:00 | 自动优化建议 | 10min |

所有任务有 10 分钟超时保护，卡住会跳过并推 TG 告警。

## 候选池管理

- 日线 RSI>80 的币加入候选池
- **已触发开仓**：立即从候选池移除
- **超过 12 小时未触发**：自动移除（超买窗口过期）
- 快速预筛 WebSocket 标记的热门币优先处理

## 环境变量（.env）

```bash
TG_BOT_TOKEN=your_telegram_bot_token
TG_CHAT_ID=your_chat_id

# 实盘模式（可选，默认影子交易）
BINANCE_API_KEY=your_api_key
BINANCE_SECRET=your_api_secret

# Dashboard 认证（可选）
DASHBOARD_TOKEN=your_dashboard_token

# 日志级别（可选，默认 INFO）
LOG_LEVEL=INFO
```

## 技术栈

- **语言**: Python 3.9+
- **交易所 API**: ccxt (Binance + OKX)
- **实时数据**: websocket-client (Binance WebSocket)
- **Web 框架**: Flask + Flask-SocketIO
- **并发控制**: fcntl 文件锁 + threading
- **部署**: Docker / Docker Compose
- **通知**: Telegram Bot API

## 开发

```bash
# 运行测试
python3 -m pytest tests/ -x -q

# 单币回测
python3 backtest.py PEPE/USDT --days 90

# 批量回测
python3 backtest.py --batch

# 手动扫描
python3 altcoin_scanner.py scan    # 全市场扫描
python3 altcoin_scanner.py check   # 候选确认
python3 altcoin_scanner.py both    # 扫描+确认
```
