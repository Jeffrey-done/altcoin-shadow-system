# 影子做空交易系统（Shadow Short Trading System）

## 系统技术文档 v5.0

---

**文档版本**: 5.0  
**最后更新**: 2025年  
**系统名称**: 影子做空交易系统  
**英文名称**: Altcoin Shadow Trading System  

---

## 目录

1. [系统概述](#1-系统概述)
2. [架构设计](#2-架构设计)
3. [策略详解](#3-策略详解)
4. [风控体系](#4-风控体系)
5. [仪表盘功能](#5-仪表盘功能)
6. [回测系统](#6-回测系统)
7. [部署运维](#7-部署运维)
8. [参数配置表](#8-参数配置表)
9. [数据文件说明](#9-数据文件说明)
10. [使用指南](#10-使用指南)

---

## 1. 系统概述

### 1.1 系统定位

本系统是一个面向山寨币合约市场的**多策略并行影子交易系统**。"影子交易"意味着系统默认运行在模拟模式（纸上交易），完整执行从信号扫描到开仓、止盈止损、复利、周报的全部链路，但不产生真实交易。经过充分验证后，可一键切换至实盘模式。

### 1.2 核心参数

| 参数 | 值 | 说明 |
|------|------|------|
| 初始本金 | 100 USDT | 起始保证金账户余额 |
| 默认杠杆 | 10x | 100U保证金 = 1000U名义仓位 |
| 资金池分配 | 60/20/20 | 做空60% / 费率套利20% / 低风险20% |
| 默认保证金 | 100U / 单笔 | 做空策略单笔保证金 |
| 目标收益 | 日均20U+ | 通过多策略并行实现 |

### 1.3 策略矩阵

| 策略 | 方向 | 保证金 | 杠杆 | 风险等级 | 目标收益 |
|------|------|--------|------|----------|----------|
| 超买做空 | SHORT | 100U | 10x | 中高 | 25~50U/单 |
| 突破回踩做多 | LONG | 50U | 10x | 中 | 25~50U/单 |
| 插针抄底做多 | LONG | 50U | 10x | 中 | 25~50U/单 |
| 资金费率套利 | LONG | 50U | 20x | 低 | 1~3U/次 |
| 网格交易 | LONG/SHORT | 20U | 5x | 低 | 0.5~1U/次 |
| 均值回归 | LONG/SHORT | 30U | 5x | 低 | 1~2U/次 |
| 多币费率收割 | LONG | 20U | 5x | 极低 | 0.2~0.5U/次 |

### 1.4 自动化全链路

```
扫描信号 → 评分过滤 → 风控检查 → 影子开仓 → 实时追踪
    ↓                                           ↓
日线扫描(4h)                              止盈止损(1h)
候选确认(1h)                              移动止损更新
做多扫描(2h)                              时间止损检查
费率扫描(结算前1h)                         ↓
低风险扫描(4h)                         平仓记录 → 风控更新
    ↓                                           ↓
自动复利 ← 盈利累积 ← 周报汇总 ← 统计分析
```

---

## 2. 架构设计

### 2.1 模块关系图

```
┌──────────────────────────────────────────────────────────────────┐
│                        scheduler.py (调度中心)                      │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────┐ ┌──────────┐  │
│  │日线扫描  │ │候选确认  │ │做多扫描  │ │费率扫描   │ │低风险扫描 │  │
│  │每4小时   │ │每1小时   │ │每2小时   │ │结算前1h  │ │每4小时    │  │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬─────┘ └────┬─────┘  │
│       │           │           │           │            │         │
│       ▼           ▼           ▼           ▼            ▼         │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │              exchange_manager.py (多交易所管理)                 │ │
│  │         Binance + OKX 数据聚合 / 交叉验证 / 容错               │ │
│  └───────────────────────────┬─────────────────────────────────┘ │
│                              ▼                                    │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │                   signal_score.py (信号评分)                   │ │
│  │              + BTC趋势过滤 + 4维评分(0~100)                    │ │
│  └───────────────────────────┬─────────────────────────────────┘ │
│                              ▼                                    │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │                   risk_control.py (风控模块)                   │ │
│  │        日亏限额 + 开仓次数 + 连亏暂停 + 持仓占比                   │ │
│  └───────────────────────────┬─────────────────────────────────┘ │
│                              ▼                                    │
│  ┌──────────────────────┐  ┌──────────────────────┐             │
│  │  altcoin_tracker.py   │  │  live_executor.py    │             │
│  │  (影子持仓追踪)        │  │  (实盘执行器)         │             │
│  │  止盈止损/移动止损     │  │  Binance API下单     │             │
│  └──────────┬───────────┘  └──────────────────────┘             │
│             ▼                                                     │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │                      common.py (公共工具)                      │ │
│  │    原子JSON / TG推送 / 动态余额 / 复利计算 / 时间工具             │ │
│  └─────────────────────────────────────────────────────────────┘ │
│             ▼                                                     │
│  ┌──────────────────────┐  ┌──────────────────────┐             │
│  │  weekly_report.py     │  │  dashboard.py        │             │
│  │  (策略周报)           │  │  (实时仪表盘)         │             │
│  │  TG推送+JSON存储      │  │  Flask+SocketIO      │             │
│  └──────────────────────┘  └──────────────────────┘             │
└──────────────────────────────────────────────────────────────────┘
```

### 2.2 数据流

```
Binance API ──→ 行情/K线/OI/费率 ──┐
                                    ├──→ 扫描模块
OKX API ────→ 费率/OI/行情 ────────┘
                                         │
                    ┌────────────────────┤
                    ▼                    ▼
           altcoin_candidates.json    风控状态检查
                    │                    │
                    ▼                    ▼
           altcoin_shadow_trades.json  risk_state.json
           funding_arb_trades.json
           low_risk_trades.json
                    │
          ┌────────┼────────┐
          ▼        ▼        ▼
      Dashboard  周报生成  TG推送
```

### 2.3 技术栈

| 组件 | 技术选型 | 说明 |
|------|----------|------|
| 编程语言 | Python 3.11 | 主力语言 |
| 交易所接口 | ccxt | 统一交易所抽象层 |
| OKX接口 | ccxt + REST API | 辅助数据源 + 交叉验证 |
| HTTP请求 | requests | Binance REST API调用 |
| Web框架 | Flask + Flask-SocketIO | 仪表盘 |
| 实时推送 | WebSocket (SocketIO) | 前端实时数据更新 |
| 定时调度 | 自建调度器 | 替代crontab |
| 数据存储 | JSON文件 + fcntl锁 | 轻量级持久化 |
| 消息推送 | Telegram Bot API | 交易通知和告警 |
| 容器化 | Docker + docker-compose | 一键部署 |
| 环境管理 | python-dotenv | .env配置隔离 |

---

## 3. 策略详解

### 3.1 做空扫描器（altcoin_scanner.py）

#### 策略逻辑

采用**两阶段多时间框架**扫描机制：

**第一阶段：日线全市场扫描（每4小时执行）**

1. 获取全市场 USDT 交易对行情
2. 基础筛选：
   - 24h成交量 ≥ 500,000 USDT
   - 价格 ≤ 1.0 USDT（只做小币）
   - 24h涨幅 ≥ 10%
3. 计算日线RSI（Wilder平滑，14周期）
4. 日线RSI ≥ 78 进入候选池
5. 妖币识别（3分制评分）：
   - OI 24h变化 ≥ 30% → +1分
   - 资金费率 ≥ 0.03%/8h → +1分
   - 24h涨幅 ≥ 30% → +1分
6. 资金费率 > 0.05%/8h 跳过（空头成本太贵）

**OKX交叉验证（扫描阶段）**

当候选币在OKX也有永续合约时，自动查询OKX的费率和OI数据：
- 两所费率都高 → 妖币评分额外+1
- 两所OI都在涨 → 信号评分额外+4分
- OKX无合约或数据获取失败 → 不影响主流程（优雅降级）

**第二阶段：4H确认（每1小时执行）**

1. 遍历候选池中未触发的币种
2. BTC趋势过滤：BTC 24h跌幅 > 5% 暂停全部做空
3. 计算4H RSI及近期峰值
4. 触发条件（满足其一即可）：
   - **4H RSI回落**：当前4H RSI < 70 且 峰值回落 ≥ 10点
   - **弃盘点信号**：连续2根1H K线实体下跌 > 3% + OI同步下降

#### 弃盘点检测算法

```python
弃盘点条件：
  1. 最近3根1H K线中，连续2根实体下跌 > 3%
  2. OI同步下降（可选加分项）
  
判定逻辑：
  body_drop = (open - close) / open × 100
  连续2根 body_drop > 3% → 触发信号
  若OI下降 > 2% → 确认主力撤退
```

#### 开仓执行流程

```
触发信号 → 信号评分(0~100) → 评级判断
  → A级(≥70): 全仓开仓(100U保证金)
  → B级(40~69): 半仓开仓(50U保证金)
  → <40分: 跳过不开
  → 风控检查 → 获取最新价 → 创建影子空单
  → 计算止盈止损价 → 保存交易 → TG推送
```

#### 开仓参数

| 参数 | 做空值 | 计算公式 |
|------|--------|----------|
| 保证金 | 100U（复利后动态） | get_compound_stake() |
| 杠杆 | 10x | 固定 |
| 名义仓位 | 1000U | stake × leverage |
| TP1价格 | entry × 0.95 | 跌5%触发，平50%仓位 |
| TP2价格 | entry × 0.90 | 跌10%触发，全仓平 |
| 硬止损 | entry × 1.03 | 涨3%无条件平仓 |
| 时间止损 | 24小时 | 持仓超时强制平 |

---

### 3.2 做多扫描器（long_scanner.py）

#### 策略A：突破回踩做多

**适用场景**：价格突破前高后健康回踩确认支撑。

**入场条件**：
1. 近期（最近5根1H K线）曾突破前48小时最高点（突破 > 0.5%算有效）
2. 当前价格从突破高点回踩 1%~5%（浅回踩最佳）
3. 价格未跌破前高位1%以上（确认不是假突破）
4. 回踩时成交量缩量 < 突破时最大量 × 60%（卖压不足）
5. RSI在35~60区间（健康回调，非反转）

#### 策略B：插针抄底做多

**适用场景**：极度超卖后出现反转插针形态。

**入场条件**：
1. 最近2根1H K线出现长下影线（下影线 ≥ 实体 × 3倍）
2. 下影线 > 上影线 × 2（明确的做多力量）
3. RSI < 25（极度超卖）
4. OI同步增加 ≥ 10%（主力在低位建仓，非恐慌抛售）
5. 成交量 > 1,000,000 USDT（流动性充足）

#### 做多参数

| 参数 | 值 | 说明 |
|------|------|------|
| 保证金 | 50U | LONG_STAKE |
| 杠杆 | 10x | LONG_LEVERAGE |
| TP1 | +5% | entry × 1.05 |
| TP2 | +10% | entry × 1.10 |
| 止损 | -3% | entry × 0.97 |
| 最大持仓 | 24小时 | 超时强制平 |

#### BTC过滤（做多专用）

做多策略的BTC过滤逻辑与做空相反：
- BTC 24h暴跌 > 8% → 暂停做多（可能继续跌）
- BTC温和上涨3~8% → 做多环境最佳（加分）

---

### 3.3 持仓追踪器（altcoin_tracker.py）

#### 职责

每小时运行（:15分），检查所有持仓的止盈止损状态。同时支持做空和做多方向。

#### 止盈止损优先级

```
硬止损（最高优先级）
  ↓ 未触发
TP1 第一档止盈
  ↓ 已触发TP1
TP2 第二档止盈
  ↓ 未触发TP2
移动止损
  ↓ 未触发
时间止损（最低优先级）
```

#### 盈亏计算公式

```
做空盈亏：
  pnl_pct = (entry_price - current_price) / entry_price × 100
  pnl_usd = notional_remaining × pnl_pct / 100
  其中 notional_remaining = stake_remaining × leverage

做多盈亏：
  pnl_pct = (current_price - entry_price) / entry_price × 100
  pnl_usd = notional_remaining × pnl_pct / 100
```

#### TP1 分批止盈机制

```
TP1触发时：
  locked_notional = stake × 50% × leverage
  locked_pnl = locked_notional × pnl_pct / 100
  stake_remaining = stake × 50%（剩余仓位继续持有等TP2）
```

#### 移动止损机制

```
激活条件：最高盈利达到 3%（TRAIL_STOP_ACTIVATE_PCT）
止损价计算：
  做空：trail_stop = entry × (1 - (best_pnl_pct/100 - 0.10))
  做多：trail_stop = entry × (1 + best_pnl_pct/100 - 0.10)
触发条件：价格回到止损价
效果：从最高盈利回撤10%时自动平仓，锁住大部分利润
```

#### 时间止损

```
条件：持仓天数 ≥ MAX_HOLD_DAYS(1天) 且 盈利 < 3%
例外：如果盈利超过3%，即使超时也不平仓（让利润奔跑）
```

---

### 3.4 资金费率套利（funding_arb.py）

#### 策略原理

Binance合约每8小时结算一次资金费率。当费率为负时，空头付钱给多头。本策略通过在结算前做多负费率币种，跨过结算时间收取费率收入。

#### 执行流程

```
结算前1小时（7:00/15:00/23:00 UTC）
  ↓
扫描全市场合约资金费率
  ↓
筛选：费率 < -0.05%/8h 且 24h成交量 > 1,000,000U
  ↓
风控检查 + 今日次数限制(≤3次)
  ↓
做多最负费率的1个币
  ↓
结算后30分钟检查（0:30/8:30/16:30 UTC）
  ↓
持仓超9小时 → 平仓（已跨过结算）
价格跌破止损(-1.5%) → 立即止损
```

#### 收益预期

```
50U保证金 × 20x杠杆 = 1000U名义仓位
费率 -0.1%/8h → 收入 = 1000 × 0.1% = 1U/次
每日最多3次结算 → 最多3U/天
减去可能的方向性亏损 → 预期净收入 1~2U/天
```

#### 参数配置

| 参数 | 值 | 说明 |
|------|------|------|
| 保证金 | 50U | FUNDING_ARB_STAKE |
| 杠杆 | 20x | 风险低可用高杠杆 |
| 费率阈值 | -0.05%/8h | 低于此才开仓 |
| 成交量门槛 | 1,000,000U | 确保流动性 |
| 最大持仓 | 9小时 | 跨过1次结算 |
| 止损 | 1.5% | 方向错了快跑 |
| 每日上限 | 3次 | 控制频率 |

#### OKX交叉验证

费率套利扫描时自动查询OKX费率：
- 两所费率都为负 → 标记"OKX确认"，排序优先开仓
- OKX确认的币信号更可靠（两个独立市场一致性确认）

#### 跨交易所费率套利发现

新增 `cross` 命令（`python3 funding_arb.py cross`）：
- 聚合 Binance + OKX 全量费率数据
- 发现两种机会：
  1. 两所都极度负费率（做多信号极强）
  2. 两所费率差 > 0.1%（跨所对冲机会）
- 推送机会到TG，不自动开仓（需人工确认）

---

### 3.5 低风险日收策略（low_risk_strategy.py）

#### 目标

每日稳定收益1~3%，最大回撤控制在1%以内。通过小仓位+低杠杆+分散投资实现。

#### 子策略一：网格交易

**适用条件**：24h振幅在1%~3%的震荡币种（如BTC、ETH等大币）

**执行逻辑**：
1. 计算24h最高/最低价，等分为5个网格层级
2. 在当前价附近设置买入点
3. 目标价：当前价 × (1 + 0.5%)
4. 止损价：当前价 × (1 - 1.0%)
5. 最大持仓4小时

**参数**：20U保证金 × 5x杠杆 = 100U名义仓位

#### 子策略二：均值回归

**适用条件**：价格偏离近24小时均值超过1.5个标准差

**执行逻辑**：
1. 计算近24根1H K线的均值和标准差
2. 偏离 < -1.5σ → 做多（预期回归均值）
3. 偏离 > +1.5σ → 做空（预期回归均值）
4. 目标价 = 均值
5. 止损 = 当前价格 ± 2倍阈值 × 标准差
6. 最大持仓8小时

**参数**：30U保证金 × 5x杠杆 = 150U名义仓位

#### 子策略三：多币费率收割

**适用条件**：多个大币种同时出现负费率（阈值宽松：-0.03%）

**执行逻辑**：
1. 从配置的10个大币种中筛选负费率币
2. 最多同时开3个币的做多仓位
3. 每个仓位：20U × 5x = 100U
4. 止损1.5%，目标跨过结算

#### Kelly公式动态仓位

```python
kelly_pct = (win_rate × avg_win - (1-win_rate) × avg_loss) / avg_win
实际仓位 = kelly_pct × 0.25(四分之一Kelly) × 账户余额
上限：不超过账户余额的25%
```

#### 日度风控

| 参数 | 值 | 说明 |
|------|------|------|
| 日止盈 | 2% (2U) | 达目标停止交易 |
| 日止损 | 1% (1U) | 回撤超限停止交易 |
| 最大同时持仓 | 5个 | 分散风险 |

---

### 3.6 信号评分系统（signal_score.py）

#### 做空信号评分（0~100分）

**4维评分体系**：

| 维度 | 权重 | 评分逻辑 |
|------|------|----------|
| RSI强度 | 0~25分 | 日线RSI越高越强 + 4h回落深度加分 |
| 妖币特征 | 0~25分 | yao_score映射: 0→0, 1→8, 2→16, 3→25 |
| 触发方式 | 0~25分 | 弃盘点25 > 弃盘点(无OI)20 > 4h回落15 |
| 市场热度 | 0~25分 | OI涨幅(0~10) + 费率(0~8) + BTC趋势(0~7) |
| OKX交叉验证 | 0~8分(额外) | 两所费率/OI一致时加分 |

**评级规则**：

| 总分 | 评级 | 仓位 | 操作 |
|------|------|------|------|
| ≥ 70 | A级 | 全仓(100U) | 果断开仓 |
| 40~69 | B级 | 半仓(50U) | 谨慎开仓 |
| < 40 | SKIP | 0 | 跳过不开 |

#### 做多信号评分（0~100分）

| 维度 | 权重 | 评分逻辑 |
|------|------|----------|
| RSI强度 | 0~25分 | 插针：RSI越低越强；突破：40~50最佳 |
| 形态质量 | 0~25分 | 插针：下影线长度；突破：回踩深度1~3%最佳 |
| OI/成交量 | 0~25分 | OI增加+15，成交量放大+10 |
| 市场环境 | 0~25分 | BTC温和上涨3~8%最佳(25分) |

#### BTC趋势过滤

| BTC 24h变动 | 对做空影响 | 对做多影响 |
|-------------|-----------|-----------|
| 跌 > 5% | 暂停做空 | 无影响 |
| 跌 > 8% | 暂停做空 | 暂停做多 |
| 涨 > 8% | 信号加分+7 | 加分+20 |
| 涨 3~8% | 加分+4 | 最佳环境(+25) |

---

### 3.7 多交易所管理（exchange_manager.py）

#### 设计原则

- **Binance为主**：下单 + 主数据源
- **OKX为辅**：交叉验证 + 品种补充 + 数据容错
- **优雅降级**：任一交易所故障不影响系统运行

#### 功能列表

| 功能 | 说明 |
|------|------|
| 费率交叉验证 | 对比两所费率，方向一致时信号加强 |
| OI交叉验证 | 对比两所OI变化，同步增长确认主力动向 |
| 品种覆盖检查 | 检查OKX是否有某币永续合约 |
| 聚合费率数据 | 合并两所全量费率，计算平均 |
| 跨所套利发现 | 发现费率差异大的对冲机会 |
| BTC多源容错 | Binance挂了自动切OKX获取BTC价格 |

#### 交叉验证逻辑

费率交叉验证：
  Binance费率 ≥ 0.03% 且 OKX费率 ≥ 0.02% → signal_boost = True
  Binance费率 ≤ -0.05% 且 OKX费率 ≤ -0.04% → both_negative = True

OI交叉验证：
  Binance OI变化 ≥ 30% 且 OKX OI变化 ≥ 20% → signal_boost = True

#### OKX参数（独立于Binance）

OKX体量较小，阈值需独立设置：
| 参数 | Binance值 | OKX值 | 说明 |
|------|-----------|--------|------|
| 费率过热 | 0.03% | 0.02% | OKX费率波动相对小 |
| 费率套利 | -0.05% | -0.04% | OKX更容易出现极端费率 |
| OI变化 | 30% | 20% | OKX体量小，OI变化幅度不同 |

---


## 4. 风控体系

### 4.1 风控模块（risk_control.py）

#### 设计原则

- **保护本金优先**：宁可错过信号，不可扩大亏损
- **层层递进**：单笔止损 → 单日止损 → 连亏暂停 → 持仓限制
- **自动化执行**：所有风控规则自动执行，无需人工干预

#### 风控规则总览

| 规则 | 阈值 | 触发动作 |
|------|------|----------|
| 单笔硬止损 | 3% | 价格反弹3%无条件平仓 |
| 单日最大亏损 | 30U | 达到后当日禁止开仓 |
| 单日最大开仓 | 2次 | 达到后当日禁止开新仓 |
| 连续亏损暂停 | 3次 | 暂停24小时不交易 |
| 最大持仓占比 | 50% | 持仓保证金不超过对应资金池的50% |

#### 风控状态数据结构

```python
RiskState:
  date: str                    # 当前日期（新的一天自动重置）
  daily_loss: float            # 当日已实现亏损累计
  daily_trades_opened: int     # 当日已开仓次数
  consecutive_losses: int      # 连续亏损次数（跨日保留）
  paused_until: Optional[str]  # 暂停截止时间（ISO格式）
  total_open_stake: float      # 当前持仓总保证金
```

#### 风控检查流程

```
can_open_trade(stake, strategy) 被调用时：
  1. 检查暂停状态 → 暂停期间内拒绝
  2. 检查单日亏损 → 超30U拒绝
  3. 检查开仓次数 → 超2次拒绝
  4. 检查持仓占比 → 超过对应池50%拒绝
  5. 全部通过 → 允许开仓
```

#### 资金池隔离

风控按策略类型检查不同的资金池：

```
做空策略：可用资金 = 动态余额 × 60% × 50%
费率套利：可用资金 = 动态余额 × 20% × 50%
低风险策略：可用资金 = 动态余额 × 20% × 50%
```

### 4.2 止损体系（多层防护）

| 层级 | 类型 | 触发条件 | 说明 |
|------|------|----------|------|
| L1 | 硬止损 | 价格反弹3% | 最高优先级，无条件执行 |
| L2 | 移动止损 | 盈利3%后激活，从最高回撤10% | 锁住利润 |
| L3 | 时间止损 | 持仓>24h且盈利<3% | 避免资金占用 |
| L4 | 日度止损 | 当日累计亏损≥30U | 停止当日所有开仓 |
| L5 | 连亏暂停 | 连续亏损3次 | 暂停24小时冷静 |

### 4.3 风控告警

系统在以下情况自动推送TG告警：
- 单笔硬止损触发
- 当日亏损达到上限
- 连续亏损暂停交易
- 健康检查发现异常

---

## 5. 仪表盘功能

### 5.1 技术架构

| 组件 | 技术 | 说明 |
|------|------|------|
| 后端 | Flask | Python Web框架 |
| 实时推送 | Flask-SocketIO | WebSocket双向通信 |
| 价格源 | Binance REST API | 10秒轮询所有持仓价格 |
| 前端 | 原生HTML+JS+CSS | 无依赖，加载快 |
| 图表 | Canvas绘制 | 盈亏曲线 |

### 5.2 页面列表

| 页面 | 路径 | 功能 |
|------|------|------|
| 主面板 | `/` | 总览所有策略状态 |
| 低风险策略 | `/low-risk` | 低风险策略详情 |
| 周报 | `/weekly-report` | 周度统计报告 |
| 批量回测 | `/batch-backtest` | 多币种对比回测 |
| 单币回测 | `/backtest` | 单币种回测结果 |
| 策略评分 | `/signal-scores` | 信号评分详情 |

### 5.3 主面板功能

**顶部摘要卡片**：
- 今日盈亏（全策略合计）
- 累计盈亏
- 动态余额
- 总胜率

**资金池可视化**：
- 三色进度条：做空60% / 费率20% / 低风险20%
- 每个池的分配金额和已占用金额

**做空/做多持仓分离**：
- 左侧：做空持仓表（币种/入场/现价/盈亏%/盈亏U/TP1状态）
- 右侧：做多持仓表（币种/入场/现价/盈亏%/盈亏U/策略类型）

**风控状态**：
- 今日亏损进度条
- 开仓次数
- 连亏次数
- 暂停状态

**费率套利**：
- 今日/累计盈亏
- 持仓中的费率交易详情

**低风险策略摘要**：
- 今日/累计盈亏
- 持仓数量
- 持仓列表

**候选池**：
- 候选币种列表（RSI/涨幅/OI/妖币分/触发状态）

**历史盈亏曲线**：
- Canvas绘制的每日盈亏柱状图
- 累计盈亏折线图

**最近平仓记录**：
- 最近20笔平仓交易明细

### 5.4 实时价格更新

```
后台线程（每10秒）：
  1. 读取所有持仓中的币种列表
  2. 调用 Binance /api/v3/ticker/price 批量获取价格
  3. 更新价格缓存
  4. 注入到 dashboard 数据
  5. 通过 SocketIO emit('update') 推送到前端
```

### 5.5 REST API

| 端点 | 说明 |
|------|------|
| `GET /api/data` | 获取完整仪表盘数据 |
| `GET /api/backtest` | 获取单币回测结果 |
| `GET /api/batch-backtest` | 获取批量回测结果 |
| `GET /api/weekly-report` | 获取周报数据 |
| `GET /api/low-risk` | 获取低风险策略数据 |
| `GET /api/signal-scores` | 获取策略评分详情 |

---

## 6. 回测系统

### 6.1 回测引擎（backtest.py）

#### 功能列表

1. **单币回测**：对指定币种执行完整策略回放
2. **参数网格搜索**：遍历参数组合找最优配置
3. **批量回测**：10个币种同时回测对比
4. **相关性分析**：Pearson相关系数矩阵
5. **推荐组合**：低相关性+高评分的币种组合

#### 回测流程

```
1. 获取历史K线数据（Binance API，支持缓存）
2. 计算完整RSI序列（Wilder平滑）
3. 检测入场信号（RSI超买回落）
4. 模拟每笔交易执行：
   - 入场价 = 信号K线下一根的开盘价（避免未来数据偏差）
   - 应用滑点（0.1%）
   - 按优先级检查：硬止损 > TP1 > TP2 > 移动止损 > 时间止损
   - 应用手续费（0.04% × 2）
5. 统计分析：胜率、盈亏比、最大回撤、夏普率、连亏次数
```

#### 参数网格搜索

搜索维度：
- TP1: 3%, 5%, 7%
- TP2: 8%, 10%, 15%
- 硬止损: 2%, 3%, 5%
- RSI阈值: 72, 75, 78, 82
- 回落点数: 8, 10, 12

总组合数：3 × 3 × 3 × 4 × 3 = 324（排除TP2≤TP1的无效组合）

#### 批量回测币种

```python
默认10个小币种：
PEPE/USDT, DOGE/USDT, SHIB/USDT, FLOKI/USDT, 1000SATS/USDT,
BONK/USDT, WIF/USDT, PEOPLE/USDT, LUNC/USDT, ORDI/USDT
```

#### 相关性分析

计算每对币种权益曲线的Pearson相关系数：
- 相关性 > 0.7：标记为高相关对（避免同时持仓）
- 推荐组合：从排名靠前的币种中选出彼此相关性 < 0.7的组合

#### 评分公式

```
复合评分 = 0.4 × (win_rate/100) 
         + 0.3 × (profit_loss_ratio/5)
         + 0.2 × (1 - max_drawdown/100)
         + 0.1 × (sharpe_ratio/3)
```

#### 输出指标

| 指标 | 说明 |
|------|------|
| 总交易数 | 回测期间触发的信号数 |
| 胜率 | 盈利单数/总单数 |
| 平均盈利 | 盈利单的平均盈亏(U) |
| 平均亏损 | 亏损单的平均盈亏(U) |
| 盈亏比 | |平均盈利/平均亏损| |
| 总盈亏 | 累计盈亏(U) |
| 最大回撤 | 权益曲线最大回撤(%) |
| 最大连亏 | 最大连续亏损次数 |
| 夏普率 | 年化风险调整收益 |

#### 实盘建议标准

- ✅ 达标：胜率≥50% 且 盈亏比≥1.5
- ⚠️ 可尝试：胜率≥40% 且 盈亏比≥2.0
- ❌ 不建议：其他情况需继续调优

---

## 7. 部署运维

### 7.1 Docker部署

#### Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir ccxt python-dotenv requests flask flask-socketio
COPY . .
RUN mkdir -p /app/backtest_cache
EXPOSE 8080
CMD ["python3", "dashboard.py"]
```

#### docker-compose.yml

```yaml
version: '3.8'
services:
  dashboard:
    build: .
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
      - ./.env:/app/.env:ro
    restart: unless-stopped
    command: python3 dashboard.py

  scheduler:
    build: .
    volumes:
      - ./data:/app/data
      - ./.env:/app/.env:ro
    restart: unless-stopped
    command: python3 scheduler.py
    depends_on:
      - dashboard
```

#### 部署步骤

```bash
# 1. 克隆项目
git clone <repo_url>
cd altcoin-shadow-system

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 TG_BOT_TOKEN 和 TG_CHAT_ID

# 3. 构建并启动
docker-compose up -d

# 4. 查看日志
docker-compose logs -f scheduler
docker-compose logs -f dashboard

# 5. 访问仪表盘
open http://localhost:8080
```

### 7.2 健康检查（health_check.py）

每6小时自动执行一次，检查项：

| 检查项 | 说明 | 告警条件 |
|--------|------|----------|
| API连接 | Binance API可达性 | 超时或返回非200 |
| 信号频率 | 最近开仓时间 | 连续3天无新信号 |
| 累计盈亏 | 总体表现 | 累计亏损超本金50% |
| 文件健康 | 数据文件状态 | 文件损坏或超过10MB |
| 今日表现 | 日内表现 | 接近日亏上限(80%) |

### 7.3 任务调度器（scheduler.py）

| 任务 | 频率 | 时间点 | 说明 |
|------|------|--------|------|
| 日线扫描 | 每4小时 | 0:00/4:00/8:00/12:00/16:00/20:00 | 全市场RSI扫描 |
| 候选确认 | 每小时 | :30分 | 4H RSI回落/弃盘点检测 |
| 止盈止损 | 每小时 | :15分 | 所有持仓检查 |
| 费率扫描 | 结算前1h | 7:00/15:00/23:00 | 负费率币种扫描 |
| 费率检查 | 结算后30min | 0:30/8:30/16:30 | 费率持仓平仓 |
| 做多扫描 | 每2小时 | 奇数整点 | 突破回踩/插针信号 |
| 健康检查 | 每6小时 | :45分 | 系统状态监控 |
| 日报推送 | 每日 | 8:00 UTC | 持仓日报 |

---


## 8. 参数配置表

### 8.1 账户与杠杆

| 参数名 | 值 | 说明 |
|--------|------|------|
| ACCOUNT_BALANCE | 100 | 账户总本金(USDT) |
| LEVERAGE | 10 | 默认杠杆倍数 |
| DEFAULT_STAKE | 100 | 做空单笔保证金(USDT) |
| LIVE_MODE | False | 实盘开关(True=真实下单) |

### 8.2 扫描过滤

| 参数名 | 值 | 说明 |
|--------|------|------|
| VOL_MIN | 500,000 | 24h成交量下限(USDT) |
| PRICE_MAX | 1.0 | 价格上限(只做小币) |
| PCT_24H_MIN | 10 | 24h涨幅最低要求(%) |

### 8.3 RSI参数

| 参数名 | 值 | 说明 |
|--------|------|------|
| RSI_PERIOD | 14 | RSI计算周期 |
| DAILY_RSI_MIN | 78 | 日线RSI超买阈值 |
| H4_RSI_ENTER | 70 | 4h RSI进入阈值 |
| H4_RSI_DROP | 10 | 4h RSI回落点数要求 |
| H4_RSI_PEAK_LOOKBACK | 10 | RSI峰值回溯K线数 |

### 8.4 妖币识别

| 参数名 | 值 | 说明 |
|--------|------|------|
| OI_CHANGE_MIN | 0.30 | OI 24h涨幅下限(30%) |
| FUNDING_MAX | 0.05 | 资金费率上限(超过跳过) |
| FUNDING_HOT | 0.03 | 多头过热阈值(%/8h) |

### 8.5 弃盘点

| 参数名 | 值 | 说明 |
|--------|------|------|
| ABANDON_BODY_DROP_PCT | 3 | 单根1H实体下跌阈值(%) |
| ABANDON_CONSECUTIVE | 2 | 连续满足K线数 |
| ABANDON_OI_DROP_PCT | 0.02 | OI下降比例阈值 |

### 8.6 止盈止损

| 参数名 | 值 | 说明 |
|--------|------|------|
| TP1_MULTIPLIER | 0.95 | TP1价格=入场×0.95(跌5%) |
| TP2_MULTIPLIER | 0.90 | TP2价格=入场×0.90(跌10%) |
| TP1_CLOSE_RATIO | 0.5 | TP1平仓比例(50%) |
| HARD_STOP_LOSS_PCT | 3.0 | 硬止损(涨3%平仓) |
| TRAIL_STOP_ACTIVATE_PCT | 3 | 移动止损激活阈值(%) |
| TRAIL_STOP_DRAWDOWN_PCT | 0.10 | 移动止损回撤比例 |
| MAX_HOLD_DAYS | 1 | 最大持仓天数 |
| TIME_STOP_MIN_PROFIT_PCT | 3 | 超时免平仓的最低盈利 |

### 8.7 风控参数

| 参数名 | 值 | 说明 |
|--------|------|------|
| RISK_MAX_DAILY_LOSS | 30 | 单日最大亏损(U) |
| RISK_MAX_DAILY_TRADES | 2 | 单日最大开仓次数 |
| RISK_CONSECUTIVE_LOSS_PAUSE | 3 | 连亏暂停阈值 |
| RISK_PAUSE_HOURS | 24 | 暂停时长(小时) |
| RISK_MAX_POSITION_PCT | 0.5 | 最大持仓占比(50%) |

### 8.8 资金费率套利

| 参数名 | 值 | 说明 |
|--------|------|------|
| FUNDING_ARB_ENABLED | True | 是否开启 |
| FUNDING_ARB_MIN_RATE | -0.05 | 负费率阈值(%/8h) |
| FUNDING_ARB_STAKE | 50 | 单笔保证金(U) |
| FUNDING_ARB_LEVERAGE | 20 | 杠杆倍数 |
| FUNDING_ARB_MAX_HOLD_HOURS | 9 | 最大持仓时间 |
| FUNDING_ARB_STOP_LOSS_PCT | 1.5 | 止损(%) |
| FUNDING_ARB_VOL_MIN | 1,000,000 | 成交量门槛(U) |
| FUNDING_ARB_MAX_DAILY | 3 | 每日上限次数 |

### 8.9 信号评分

| 参数名 | 值 | 说明 |
|--------|------|------|
| SIGNAL_SCORE_ENABLED | True | 是否启用评分 |
| SCORE_FULL_THRESHOLD | 70 | A级全仓阈值 |
| SCORE_HALF_THRESHOLD | 40 | B级半仓阈值 |
| SCORE_SKIP_THRESHOLD | 40 | 跳过阈值 |

### 8.10 BTC趋势过滤

| 参数名 | 值 | 说明 |
|--------|------|------|
| BTC_FILTER_ENABLED | True | 是否启用 |
| BTC_CRASH_THRESHOLD | -5.0 | BTC暴跌暂停做空(%) |
| BTC_PUMP_THRESHOLD | 8.0 | BTC暴涨信号加分(%) |

### 8.11 自动复利

| 参数名 | 值 | 说明 |
|--------|------|------|
| AUTO_COMPOUND_ENABLED | True | 是否启用 |
| COMPOUND_STEP | 50 | 每盈利50U触发 |
| COMPOUND_INCREASE | 25 | 每步增加保证金 |
| COMPOUND_MAX_STAKE | 300 | 单笔保证金上限 |

**复利公式**：
```
effective_stake = DEFAULT_STAKE + (total_realized_pnl // 50) × 25
上限 = min(effective_stake, 300)

示例：
  盈利 0~49U  → 保证金 100U
  盈利 50~99U → 保证金 125U
  盈利 100~149U → 保证金 150U
  ...
  盈利 400U+ → 保证金 300U(上限)
```

### 8.12 资金池分配

| 参数名 | 值 | 说明 |
|--------|------|------|
| SHORT_STRATEGY_POOL_PCT | 60 | 做空策略池(%) |
| FUNDING_ARB_POOL_PCT | 20 | 费率套利池(%) |
| LOW_RISK_POOL_PCT | 20 | 低风险策略池(%) |

### 8.13 做多扫描器

| 参数名 | 值 | 说明 |
|--------|------|------|
| LONG_BREAKOUT_LOOKBACK | 48 | 突破回看K线数 |
| LONG_PULLBACK_DEPTH_MAX | 0.03 | 最大回踩深度 |
| LONG_PULLBACK_RSI_MIN | 35 | 回踩RSI下限 |
| LONG_PULLBACK_RSI_MAX | 60 | 回踩RSI上限 |
| LONG_PULLBACK_VOL_SHRINK | 0.6 | 缩量阈值 |
| LONG_PIN_SHADOW_RATIO | 3.0 | 插针影线/实体比 |
| LONG_PIN_RSI_MAX | 25 | 插针RSI上限 |
| LONG_PIN_OI_INCREASE_MIN | 0.10 | OI增加最低要求 |
| LONG_PIN_VOL_MIN | 1,000,000 | 成交量门槛 |
| LONG_STAKE | 50 | 做多保证金(U) |
| LONG_LEVERAGE | 10 | 做多杠杆 |
| LONG_TP1_PCT | 0.05 | TP1(+5%) |
| LONG_TP2_PCT | 0.10 | TP2(+10%) |
| LONG_STOP_LOSS_PCT | 3.0 | 止损(-3%) |

### 8.14 低风险策略

| 参数名 | 值 | 说明 |
|--------|------|------|
| LOW_RISK_ENABLED | True | 是否启用 |
| LOW_RISK_DAILY_TARGET_PCT | 2.0 | 日止盈目标(%) |
| LOW_RISK_MAX_DAILY_DRAWDOWN_PCT | 1.0 | 日最大回撤(%) |
| LOW_RISK_MAX_POSITIONS | 5 | 最大同时持仓 |
| LOW_RISK_KELLY_FRACTION | 0.25 | Kelly分数(1/4) |
| LOW_RISK_GRID_LEVELS | 5 | 网格层数 |
| LOW_RISK_GRID_SPACING_PCT | 0.5 | 网格间距(%) |
| LOW_RISK_GRID_STAKE | 20 | 网格保证金(U) |
| LOW_RISK_GRID_LEVERAGE | 5 | 网格杠杆 |
| LOW_RISK_MEAN_REVERSION_THRESHOLD | 1.5 | 均值回归阈值(σ) |
| LOW_RISK_MEAN_REVERSION_STAKE | 30 | 均值回归保证金 |
| LOW_RISK_FUNDING_MAX_COINS | 3 | 费率收割最大币数 |

### 8.15 周报配置

| 参数名 | 值 | 说明 |
|--------|------|------|
| WEEKLY_REPORT_ENABLED | True | 是否开启周报 |
| WEEKLY_ROI_GRADE_A | 15 | A级ROI阈值(%) |
| WEEKLY_ROI_GRADE_B | 5 | B级ROI阈值(%) |
| WEEKLY_ROI_GRADE_C | 0 | C级ROI阈值(%) |

### 8.16 OKX多交易所配置

| 参数名 | 值 | 说明 |
|--------|------|------|
| OKX_ENABLED | True | OKX辅助数据源总开关 |
| OKX_FUNDING_HOT | 0.02 | OKX多头过热阈值(%/8h) |
| OKX_FUNDING_ARB_MIN_RATE | -0.04 | OKX负费率套利阈值 |
| OKX_OI_CHANGE_MIN | 0.20 | OKX OI变化阈值(20%) |
| OKX_CROSS_ARB_MIN_DIVERGENCE | 0.10 | 跨所费率差套利阈值(%) |
| OKX_CROSS_ARB_ENABLED | True | 跨所套利发现开关 |
| OKX_CROSS_VALIDATE_ENABLED | True | 交叉验证开关 |
| OKX_CROSS_VALIDATE_BONUS | 8 | 交叉验证通过加分值 |
| OKX_LIVE_MODE | False | OKX实盘开关 |
| OKX_DEFAULT_LEVERAGE | 10 | OKX默认杠杆 |

---


## 9. 数据文件说明

### 9.1 文件列表

| 文件名 | 路径 | 说明 |
|--------|------|------|
| altcoin_candidates.json | 项目根目录 | 做空候选池 |
| altcoin_shadow_trades.json | 项目根目录 | 做空+做多交易记录 |
| funding_arb_trades.json | 项目根目录 | 费率套利交易记录 |
| low_risk_trades.json | 项目根目录 | 低风险策略交易记录 |
| risk_state.json | 项目根目录 | 风控状态 |
| weekly_report.json | 项目根目录 | 最近一期周报 |
| backtest_results.json | 项目根目录 | 单币回测结果 |
| batch_backtest_results.json | 项目根目录 | 批量回测结果 |
| backtest_cache/ | 项目根目录 | K线数据缓存目录 |
| .env | 项目根目录 | 环境变量配置 |

### 9.2 数据结构详解

#### altcoin_candidates.json

```json
[
  {
    "symbol": "PEPE/USDT",
    "price": 0.0000123,
    "vol24h": 1500000,
    "pct24h": 25.3,
    "rsi_1d": 82.5,
    "rsi_4h": 65.0,
    "rsi_4h_peak": 85.0,
    "oi_change": 35.2,
    "funding_rate": 0.032,
    "yao_score": 2,
    "added_at": "2025-01-01T12:00:00+00:00",
    "triggered": false,
    "trigger_type": null,
    "trigger_reason": null
  }
]
```

#### altcoin_shadow_trades.json

```json
[
  {
    "id": "SCAN-SHORT-PEPE-1704067200",
    "symbol": "PEPE/USDT",
    "direction": "SHORT",
    "entry_price": 0.0000123,
    "stake": 100,
    "leverage": 10,
    "notional": 1000,
    "shares": 81300813.0,
    "opened_at": "2025-01-01T12:00:00+00:00",
    "status": "open",
    "reason": "4h RSI从85回落至65",
    "strategy": "short_overbought",
    "take_profit_1": 0.0000117,
    "take_profit_2": 0.0000111,
    "tp1_triggered": false,
    "tp1_locked_pnl": 0.0,
    "stake_remaining": 100,
    "hard_stop_price": 0.0000127,
    "best_pnl_pct": 0.0,
    "trail_stop_price": null,
    "max_hold_days": 1,
    "pnl": 0.0,
    "current_price": 0.0000120,
    "closed_at": null,
    "close_reason": null
  }
]
```

#### funding_arb_trades.json

```json
[
  {
    "id": "FUND-LONG-DOGE-1704067200",
    "symbol": "DOGE/USDT",
    "direction": "LONG",
    "entry_price": 0.085,
    "stake": 50,
    "leverage": 20,
    "notional": 1000,
    "funding_rate": -0.08,
    "expected_income": 0.8,
    "opened_at": "2025-01-01T07:00:00+00:00",
    "status": "closed",
    "strategy": "funding_arb",
    "hard_stop_price": 0.08372,
    "max_hold_hours": 9,
    "pnl": 0.12,
    "funding_income": 0.8,
    "total_pnl": 0.92,
    "closed_at": "2025-01-01T09:00:00+00:00",
    "close_reason": "结算完成（持仓9.0h）"
  }
]
```

#### risk_state.json

```json
{
  "date": "2025-01-01",
  "daily_loss": 15.5,
  "daily_trades_opened": 1,
  "consecutive_losses": 0,
  "paused_until": null,
  "total_open_stake": 100
}
```

#### weekly_report.json

```json
{
  "metadata": {
    "generated_at": "2025-01-06T00:00:00+00:00",
    "week_start": "2024-12-30T00:00:00+00:00",
    "week_end": "2025-01-06T00:00:00+00:00",
    "grade": "B",
    "roi_pct": 8.5
  },
  "stats": {
    "total_pnl": 8.5,
    "short_pnl": 5.2,
    "funding_pnl": 2.1,
    "low_risk_pnl": 1.2,
    "total_trades": 8,
    "win_count": 5,
    "loss_count": 3,
    "win_rate": 62.5,
    "best_trade": {"symbol": "PEPE/USDT", "pnl": 4.2},
    "worst_trade": {"symbol": "SHIB/USDT", "pnl": -2.1},
    "daily_breakdown": {"2024-12-30": 1.5, "2024-12-31": 2.0},
    "strategy_breakdown": {
      "short_overbought": {"count": 3, "pnl": 5.2, "wins": 2},
      "funding_arb": {"count": 4, "pnl": 2.1, "wins": 3}
    }
  },
  "suggestions": ["费率套利贡献超过50%总收益，建议增加费率策略资金分配"]
}
```

### 9.3 并发安全机制

所有JSON文件读写都使用 `fcntl.flock` 确保并发安全：

```python
写入流程：
  1. 获取排他锁（LOCK_EX）
  2. 写入临时文件
  3. os.replace() 原子替换目标文件
  4. 释放锁

读取流程：
  1. 获取共享锁（LOCK_SH）
  2. 读取并解析JSON
  3. 释放锁
```

---

## 10. 使用指南

### 10.1 快速开始

#### 环境准备

```bash
# 安装依赖
pip install ccxt python-dotenv requests flask flask-socketio

# 创建 .env 文件
TG_BOT_TOKEN=your_telegram_bot_token
TG_CHAT_ID=your_chat_id
# 实盘模式需要（默认关闭）：
# BINANCE_API_KEY=your_api_key
# BINANCE_SECRET=your_secret
# OKX实盘需要（默认关闭）：
# OKX_API_KEY=your_okx_api_key
# OKX_SECRET=your_okx_secret
# OKX_PASSPHRASE=your_okx_passphrase
```

#### 手动运行各模块

```bash
# 做空扫描（日线全市场）
python3 altcoin_scanner.py scan

# 做空确认（候选池4H RSI检查）
python3 altcoin_scanner.py check

# 做多扫描
python3 long_scanner.py scan

# 止盈止损检查
python3 altcoin_tracker.py --check-only

# 日报推送
python3 altcoin_tracker.py

# 费率套利扫描
python3 funding_arb.py scan

# 费率套利平仓检查
python3 funding_arb.py check

# 跨交易所费率套利发现
python3 funding_arb.py cross

# 低风险策略扫描
python3 low_risk_strategy.py scan
python3 low_risk_strategy.py scan --mode grid
python3 low_risk_strategy.py scan --mode mean
python3 low_risk_strategy.py scan --mode funding

# 低风险持仓检查
python3 low_risk_strategy.py check

# 周报生成
python3 weekly_report.py

# 回测
python3 backtest.py --symbol PEPE/USDT --days 90
python3 backtest.py --grid --symbol PEPE/USDT
python3 backtest.py --batch

# 仪表盘
python3 dashboard.py --port 8080

# 健康检查
python3 health_check.py

# 自动调度（包含所有任务）
python3 scheduler.py
```

### 10.2 实盘切换指南

#### 前置条件（必须全部满足）

1. **纸上交易验证至少2周**
2. **胜率 > 50%**
3. **盈亏比 > 1.5**
4. **回测验证参数有效**
5. **Binance合约账户已开通**
6. **API密钥已配置**

#### 切换步骤

```python
# config.py 中修改：
LIVE_MODE = True

# 建议先从小仓位开始：
DEFAULT_STAKE = 20  # 先用20U测试
```

#### 安全建议

- 初期保持 DEFAULT_STAKE = 20（最小仓位）
- 运行1周无异常后逐步增加
- 始终保持风控参数不变
- 定期检查周报评级

#### OKX实盘切换

如果使用OKX账户实盘交易：

```python
# config.py 中修改：
LIVE_MODE = False           # 关闭Binance实盘
OKX_LIVE_MODE = True        # 开启OKX实盘
OKX_DEFAULT_LEVERAGE = 10   # OKX杠杆

# .env 中添加：
OKX_API_KEY=your_key
OKX_SECRET=your_secret
OKX_PASSPHRASE=your_passphrase
```

注意：数据分析仍使用两所公共API，执行下单走OKX。

### 10.3 策略调优建议

#### 信号太少

- 降低 DAILY_RSI_MIN（如 75 → 72）
- 降低 H4_RSI_DROP（如 10 → 8）
- 扩大 PRICE_MAX（如 1.0 → 2.0）
- 降低 VOL_MIN（如 500000 → 300000）

#### 止损太频繁

- 增大 HARD_STOP_LOSS_PCT（如 3% → 5%）
- 注意：增大止损也增大单笔最大亏损

#### 利润太少

- 增大 TP2_MULTIPLIER（如 0.90 → 0.85）
- 增大 TRAIL_STOP_ACTIVATE_PCT 让利润多跑
- 启用自动复利增加仓位

#### 连亏频率高

- 提高评分阈值（如 SCORE_FULL_THRESHOLD = 75）
- 加强BTC过滤
- 减少单日开仓次数限制

### 10.4 周报解读

#### ROI评级

| 评级 | 条件 | 含义 |
|------|------|------|
| A | 周ROI ≥ 15% | 策略表现优秀 |
| B | 周ROI ≥ 5% | 策略表现良好 |
| C | 周ROI ≥ 0% | 策略持平 |
| F | 周ROI < 0% | 策略亏损，需检查 |

#### 建议触发规则

| 条件 | 自动建议 |
|------|----------|
| 胜率 < 40% | 收紧入场条件 |
| 单日亏损 > 本金20% | 降低杠杆或仓位 |
| 平均持仓 > 20h | 检查时间止损参数 |
| 费率收入占比 > 50% | 增加费率策略资金 |
| 连续亏损 | 审查风控参数 |
| 总体亏损 | 回顾信号质量 |
| 零交易 | 检查扫描器运行状态 |

### 10.5 常见问题

**Q: 系统启动后没有任何交易？**
A: 检查以下几点：
1. 市场是否有符合条件的币（24h涨幅>10%，RSI>78）
2. 风控是否处于暂停状态（连亏或日亏达限）
3. BTC过滤是否触发（BTC跌>5%）
4. 查看日志：`docker-compose logs scheduler`

**Q: TG推送不工作？**
A: 检查 .env 文件中的 TG_BOT_TOKEN 和 TG_CHAT_ID 是否正确配置。

**Q: 如何查看实时状态？**
A: 访问仪表盘 http://localhost:8080，数据每10秒自动更新。

**Q: 回测和实盘结果差异大？**
A: 回测已包含0.1%滑点和0.04%手续费模拟，但实际市场流动性可能导致更大滑点。建议从小仓位开始验证。

**Q: 如何重置系统？**
A: 删除所有 .json 数据文件即可重新开始：
```bash
rm -f altcoin_candidates.json altcoin_shadow_trades.json
rm -f funding_arb_trades.json low_risk_trades.json
rm -f risk_state.json weekly_report.json
```

---

## 附录

### A. 模块文件清单

| 文件 | 行数 | 功能 |
|------|------|------|
| altcoin_scanner.py | ~300 | 做空扫描器（日线+4H两阶段） |
| long_scanner.py | ~280 | 做多扫描器（突破回踩+插针） |
| altcoin_tracker.py | ~250 | 持仓追踪（止盈止损执行） |
| funding_arb.py | ~230 | 资金费率套利 |
| low_risk_strategy.py | ~420 | 低风险日收（网格+均值回归+费率） |
| signal_score.py | ~280 | 信号评分+BTC过滤 |
| risk_control.py | ~180 | 风控模块 |
| backtest.py | ~550 | 回测引擎（单币+网格+批量） |
| weekly_report.py | ~300 | 策略周报 |
| dashboard.py | ~1600 | 实时仪表盘（Flask+SocketIO） |
| scheduler.py | ~100 | 任务调度器 |
| config.py | ~180 | 参数配置中心 |
| common.py | ~170 | 公共工具（JSON/TG/时间/复利） |
| models.py | ~180 | 数据模型（Trade/FundingTrade） |
| live_executor.py | ~300 | 实盘执行器（Binance+OKX双交易所） |
| exchange_manager.py | ~320 | 多交易所管理（Binance+OKX数据/验证/执行） |
| health_check.py | ~130 | 健康检查 |

### B. 关键算法说明

#### Wilder RSI 计算

```python
# 与 TradingView 一致的 RSI 实现
# 第一段用 SMA 初始化，后续用 EMA(Wilder平滑) 递推
avg_gain = SMA(gains[:period])
avg_loss = SMA(losses[:period])
for i in range(period, len):
    avg_gain = (avg_gain × (period-1) + gains[i]) / period
    avg_loss = (avg_loss × (period-1) + losses[i]) / period
RSI = 100 - 100/(1 + avg_gain/avg_loss)
```

#### 动态余额计算

```python
动态余额 = 初始本金(100U) + 做空已实现盈亏 + 费率已实现盈亏 + 低风险已实现盈亏
```

### C. Telegram推送消息类型

| 事件 | 消息格式 | 频率 |
|------|----------|------|
| 做空开仓 | 含评分/入场价/止盈止损/触发原因 | 信号触发时 |
| 做多开仓 | 含策略/入场价/止盈止损 | 信号触发时 |
| TP1止盈 | 含锁定利润/剩余仓位 | 触发时 |
| TP2全仓平 | 含总盈亏 | 触发时 |
| 硬止损 | 含亏损金额 | 触发时 |
| 移动止损 | 含最高盈利→当前 | 触发时 |
| 时间止损 | 含持仓天数 | 触发时 |
| 费率开仓 | 含费率/预期收入 | 开仓时 |
| 费率平仓 | 含方向PnL/费率收入/总计 | 平仓时 |
| 风控暂停 | 含连亏次数/暂停时间 | 触发时 |
| 日亏达限 | 含累计亏损 | 触发时 |
| 日报 | 含所有持仓浮盈/风控状态 | 每日8:00 UTC |
| 周报 | 含完整统计/建议 | 每周一 |
| 健康告警 | 含异常项目列表 | 检测到异常时 |
| BTC过滤 | 含BTC跌幅/暂停原因 | 触发时 |

---

*文档结束*

*本文档基于系统代码v5.0自动生成，如有参数变更请同步更新。*
