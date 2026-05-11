# 小币种影子做空系统

## 组件

### altcoin_scanner.py
小币种超买做空扫描器 v2.0

**策略：**
- 日线 RSI>78 入选候选池
- 4小时 RSI 从峰值回落≥10点触发
- 或 1H 出现"弃盘点"信号（连续2根K线实体下跌>3% + OI下降）

**过滤：**
- 市值：500万~5000万U
- 成交量：>50万U/24h
- 价格：<1U

**启动：**
```bash
python3 altcoin_scanner.py scan   # 日线扫描（每4小时）
python3 altcoin_scanner.py check  # 候选确认（每1小时）
python3 altcoin_scanner.py both   # 完整流程
```

### altcoin_tracker.py
影子空单追踪器

**功能：**
- 追踪持仓盈亏
- 分批止盈（-20%锁50%仓位，-35%全仓平）
- 移动止损（最高盈利回撤10%）
- 时间止损（7天强制平）

**启动：**
```bash
python3 altcoin_tracker.py         # 推送日报
python3 altcoin_tracker.py --check-only  # 只检查止盈触发
```

## 配置
创建 `.env` 文件：
```
TG_BOT_TOKEN=your_bot_token
TG_CHAT_ID=your_chat_id
```
