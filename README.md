# 小币种影子做空系统 v3.0

自动扫描超买小币种并管理影子做空单（纸上交易），通过 Telegram 推送信号和日报。

## 架构

```
common.py        ← 公共工具（TG推送、原子写JSON、日志、时间）
config.py        ← 策略参数集中配置
models.py        ← 数据模型（Trade / Candidate dataclass）
altcoin_scanner.py  ← 扫描器：发现候选 + 触发开仓
altcoin_tracker.py  ← 追踪器：止盈/止损/日报
```

## 策略逻辑

### 扫描器（altcoin_scanner.py）

**第一阶段 — 日线扫描（每4小时）：**
- 遍历 Binance 全市场 USDT 交易对
- 过滤：成交量 > 50万U、价格 < 1U、24h 涨幅 > 10%
- 日线 RSI（Wilder）> 78 → 进入候选池
- 妖币评分（0~3）：OI涨>30% / 资金费率>0.03% / 24h涨>30%

**第二阶段 — 确认触发（每1小时）：**
- 条件A：4h RSI 从峰值回落 ≥ 10 点且低于 70
- 条件B：1H 弃盘点信号（连续2根K线实体下跌>3% + OI下降>2%）
- 任一满足 → 自动开影子空单 + TG 推送

### 追踪器（altcoin_tracker.py）

**止盈规则：**
- 第一档（TP1）：价格跌 20%（entry × 0.80）→ 锁定 50% 仓位利润
- 第二档（TP2）：价格跌 35%（entry × 0.65）→ 全仓平仓

**止损规则：**
- 移动止损：最高盈利 ≥ 5% 后激活，回撤 10% 触发平仓
- 时间止损：持仓 > 7 天且盈利 < 5%，强制平仓

**盈亏计算：**
- 总 PnL = TP1 锁定利润 + 剩余仓位浮盈/亏损

## 使用方法

### 环境准备

```bash
pip install ccxt python-dotenv requests
```

### 配置

创建 `.env` 文件：
```
TG_BOT_TOKEN=your_bot_token
TG_CHAT_ID=your_chat_id
```

> ⚠️ 如果未配置 TG 环境变量，推送将被跳过（不会发送到错误地址）。

### 启动

```bash
# 扫描器
python3 altcoin_scanner.py scan    # 日线扫描（建议 cron 每4小时）
python3 altcoin_scanner.py check   # 候选确认（建议 cron 每1小时）
python3 altcoin_scanner.py both    # 完整流程

# 追踪器
python3 altcoin_tracker.py                # 检查触发 + 推送日报
python3 altcoin_tracker.py --check-only   # 只检查止盈/止损（建议 cron 每小时）
```

### 推荐 Crontab

```cron
# 每4小时日线扫描
0 */4 * * * cd /path/to/project && python3 altcoin_scanner.py scan

# 每小时候选确认
30 * * * * cd /path/to/project && python3 altcoin_scanner.py check

# 每小时止盈检查
15 * * * * cd /path/to/project && python3 altcoin_tracker.py --check-only

# 每天8:00推送日报
0 8 * * * cd /path/to/project && python3 altcoin_tracker.py
```

## 参数调优

所有策略参数集中在 `config.py`，修改后无需改动逻辑代码：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| DAILY_RSI_MIN | 78 | 日线 RSI 超买阈值 |
| H4_RSI_ENTER | 70 | 4h RSI 回落确认阈值 |
| H4_RSI_DROP | 10 | 4h RSI 需回落的点数 |
| TP1_MULTIPLIER | 0.80 | 第一档止盈（-20%） |
| TP2_MULTIPLIER | 0.65 | 第二档止盈（-35%） |
| TRAIL_STOP_ACTIVATE_PCT | 5 | 移动止损激活门槛（%） |
| TRAIL_STOP_DRAWDOWN_PCT | 0.10 | 移动止损回撤比例 |
| MAX_HOLD_DAYS | 7 | 最大持仓天数 |
| DEFAULT_STAKE | 200 | 单笔虚拟仓位（USDT） |

## 数据文件

- `altcoin_candidates.json` — 候选池（扫描器写入）
- `altcoin_shadow_trades.json` — 交易记录（扫描器/追踪器共用）

> 文件使用原子写入（先写临时文件再 rename），崩溃时不会损坏数据。

## 日志

支持 `LOG_LEVEL` 环境变量（DEBUG / INFO / WARNING / ERROR），默认 INFO。

```bash
LOG_LEVEL=DEBUG python3 altcoin_scanner.py both
```
