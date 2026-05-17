# 🔴 Altcoin Shadow Short System

全自动小币种做空影子交易系统 — 7×24 小时运行，自动扫描超买信号、开仓、止盈止损、风控管理。

## 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        Scheduler (调度器)                         │
│  每小时整点: scan_daily()  │  每小时30分: check_candidates()      │
│  每小时15分: tracker()     │  每天8:00 UTC: 日报  │  周一9:00: 优化 │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  [实时] hot_scanner ──→ 标记热门币 ──→ scan_daily 优先处理        │
│  [实时] realtime_monitor ──→ WebSocket 止盈止损（<100ms延迟）      │
│  [实时] tg_bot ──→ Telegram 交互指令                              │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

> 系统当前仅做空（`short_overbought` 策略）。文档中出现的 funding_arb / low_risk 已在 v4.1 移除，仅保留做空策略。

## ⚠️ 从 v4.0 升级到 v4.1

v4.1 修复了 `trade.pnl` 的 TP1 双计数 bug（详见 `SYSTEM_DOCUMENTATION.md` §2）。
如果你的 `altcoin_shadow_trades.json` 里有 v4.0 时代的已平仓记录（`tp1_triggered=true
且 status='closed'`），升级后 dashboard / TG 里的累计盈亏数字会显得"变小"——
这是因为 v4.1 不再把 TP1 利润算两次，**新数字才是真实总盈亏**。

建议运行迁移脚本一次性修正：

```bash
# 先停掉 scheduler / realtime_monitor / dashboard 三个进程
docker-compose down  # 或手动 kill

# 预览将修改哪些记录（不写盘）
python3 migrate_v41.py --dry-run

# 确认无误后执行
python3 migrate_v41.py
# 原文件会被备份到 altcoin_shadow_trades.json.bak.v40.*

# 重启服务
docker-compose up -d
```

脚本是幂等的（`_v41_migrated` 标记），重复运行不会重复扣减。

## 核心流程

```
1. 全市场扫描 → 日线 RSI≥75 + 涨幅>10% + 成交量>50万U → 加入候选池
2. 候选确认   → 4h RSI 回落 或 弃盘点信号 → 信号评分
3. 开仓决策   → 评分≥40分 + 风控通过 + 价格确认 + 冷却期检查 → 开仓
4. 持仓管理   → 硬止损/移动止损/分批止盈/时间止损 → 实时监控
5. 风控保护   → 单日亏损上限/最大开仓次数/连亏暂停/持仓占比限制
```

> 阈值以 `config.py` 为准（`DAILY_RSI_MIN`、`SCORE_HALF_THRESHOLD`、`RISK_MAX_DAILY_TRADES` 等）。本节描述若与 `config.py` 不同，请以 `config.py` 为准。

## 策略参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 杠杆 | 10x | 100U 保证金 = 1000U 名义仓位 |
| 硬止损 | 5% | 价格反弹 5% 无条件平仓 |
| TP1 | -5% | 价格跌 5% 锁定 50% 仓位利润 |
| TP2 | -8% | 价格跌 8% 全仓平仓 |
| 移动止损 | 盈利 3% 激活，回撤 10% 触发 | |
| 时间止损 | 24h 持仓且盈利 < 3% 强制平 | |
| 最大持仓占比 | 余额的 50% | 同一时刻最多压 50% 本金 |
| 单日亏损上限 | 30U | 达到后当日停止开仓 |
| 单日最大开仓 | 3 笔 | |
| 连亏暂停 | 连亏 3 次暂停 24h | |
| 同币冷却期 | 止损后 24h 不再开仓 | 按 `close_type` 判断 |

## 仓位模式（v5.1）

策略参数表里的金额（`DEFAULT_STAKE=30U`、`COMPOUND_STEP=50U` 等）都是按 100U 本金校准的。
如果你的实际本金是 500U/1000U，不想每个金额都手算，可以切到**比例模式**让系统自动缩放。

| 模式 | `config.POSITION_MODE` | 行为 |
|---|---|---|
| 手动（默认） | `'manual'` | `DEFAULT_STAKE / RISK_MAX_DAILY_LOSS / COMPOUND_STEP / COMPOUND_INCREASE` 用 admin 面板手填的值 |
| 按比例自动 | `'proportional'` | 以 `BASELINE_BALANCE=100U` 为基准，按 `实际余额 ÷ 100` 自动缩放上述 4 个字段 |

### 比例模式的余额来源

- `LIVE_MODE=True` 单所实盘 → 从该所 `fetch_balance` 拉真实余额（带 60s 缓存）
- `PRIMARY_EXCHANGE='both'` 双所实盘 → 两所余额相加
- `PRIMARY_EXCHANGE='auto'` → 取较大者
- 影子模式 → 用 admin 面板填的 `ACCOUNT_BALANCE`

> 📌 **多账号 + 影子模式的边界**：`POSITION_MODE` 是**全局开关**，`scale` 是个**全局值**。影子模式下多个账号的 `ACCOUNT_BALANCE` 各不相同时，系统会用**当前活跃账号**（`admin_secrets.get_active_account_id()`）的 `ACCOUNT_BALANCE` 作为缩放基准；其他非活跃账号的 `ACCOUNT_BALANCE` 在 proportional 模式下被忽略。要让不同账号有不同的 stake，请用 `manual` 模式。

### 不参与缩放的字段

- **`COMPOUND_MAX_STAKE`**：保持绝对值（默认 300U），即使账户涨到 1000U，单笔保证金也不会超过 300U
- 杠杆、止盈止损百分比、移动止损、RSI 阈值、`PCT_24H_MIN` 等"非金额"参数都不变
- `RISK_MAX_DAILY_TRADES`、`RISK_MAX_POSITION_PCT` 是笔数/比例，不是金额，也不变

### 切换方式

打开 admin 面板（详见下文），左侧 `Live Control` 标签页里有 **仓位模式（金额缩放）** 卡片：

- 下拉框选 `manual` / `proportional`
- 切到 `proportional` 后，"仓位与风控"标签页里的 4 个金额字段会变灰只读，旁边 tooltip 会说明"由 PRISTINE × scale 计算"
- 实时状态卡显示当前 `scale`、`实际余额 / 来源`、缩放后的字段值

写盘后 30 秒内所有进程（scheduler / realtime_monitor / dashboard）同步生效。

### 例子

实盘余额 500U → scale = 5：

| 字段 | manual 默认 | proportional (500U) |
|---|---|---|
| `DEFAULT_STAKE` | 30 | **150** |
| `RISK_MAX_DAILY_LOSS` | 30 | **150** |
| `COMPOUND_STEP` | 50 | **250** |
| `COMPOUND_INCREASE` | 25 | **125** |
| `COMPOUND_MAX_STAKE` | 300 | **300** ← 不变 |
| `LEVERAGE` | 10 | **10** ← 不变 |
| `HARD_STOP_LOSS_PCT` | 5.0 | **5.0** ← 不变 |

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

# Binance 合约实盘（可选，默认影子交易）
BINANCE_API_KEY=your_api_key
BINANCE_SECRET=your_api_secret

# OKX 合约实盘（可选，默认关闭）
OKX_API_KEY=your_okx_api_key
OKX_SECRET=your_okx_secret
OKX_PASSPHRASE=your_okx_passphrase

# Dashboard 认证（可选）
DASHBOARD_TOKEN=your_dashboard_token

# 日志级别（可选，默认 INFO）
LOG_LEVEL=INFO
```

## 实盘交易（Binance + OKX 双交易所路由）

系统默认运行在**影子交易模式**（纸上模拟）。开启任一交易所的实盘前，请务必：
1. 纸上交易验证至少 2 周，胜率 > 50%，盈亏比 > 1.5
2. 运行 `python3 check_live.py` 自检通过
3. `DEFAULT_STAKE` 先降到 20U 小仓位实测

### 开关与路由模式

| 配置项 | 取值 | 效果 |
|------|------|------|
| `LIVE_MODE=False` + `OKX_LIVE_MODE=False` | 默认 | 影子模式，不下任何真实单 |
| `LIVE_MODE=True` + `OKX_LIVE_MODE=False` | 单所 | 所有信号只在 Binance 下单 |
| `LIVE_MODE=False` + `OKX_LIVE_MODE=True` | 单所 | 所有信号只在 OKX 下单 |
| 两个都 `True`，`PRIMARY_EXCHANGE='binance'` | 双所 | 信号只在 Binance 下单（OKX 仅数据源）|
| 两个都 `True`，`PRIMARY_EXCHANGE='okx'` | 双所 | 信号只在 OKX 下单 |
| 两个都 `True`，`PRIMARY_EXCHANGE='both'` | 双所 | **每笔信号在两所各开半仓**（保证金 50/50 分散对手方风险）|
| 两个都 `True`，`PRIMARY_EXCHANGE='auto'` | 双所 | 按币种覆盖决定；`PRIMARY_EXCHANGE_FALLBACK` 决定冲突时的选择 |

### 开启币安实盘（推荐首选）

**1. Binance 账户准备**
- 合约账户持仓模式切换为 **对冲模式 (Hedge Mode)**
  `合约 → 偏好设置 → 持仓模式 → 对冲模式`
  代码里用了 `positionSide=SHORT/LONG`，单向模式会直接报错。
- USDT 本位永续合约已开通

**2. API Key 权限**
- ✅ 允许合约交易 (Enable Futures)
- ❌ **不要**勾选"允许提现"
- ❌ **不要**勾选"允许现货交易"（除非你有其他用途）
- 建议绑定 IP 白名单

**3. 配置 `.env`**
```bash
BINANCE_API_KEY=...
BINANCE_SECRET=...
```

**4. 改 `config.py`**
```python
LIVE_MODE = True
DEFAULT_STAKE = 20    # ⚠️ 先用 20U 小仓位验证
```

**5. 自检**
```bash
python3 check_live.py            # 检查两所
python3 check_live.py binance    # 只检查币安
```
全部显示 ✓ 后重启调度器。

### 开启 OKX 实盘

**1. OKX 账户准备**
- 合约账户持仓模式切换为 **双向持仓 (long_short_mode)**
  `交易 → 设置 → 合约持仓模式 → 双向持仓`
- USDT 本位永续合约已开通

**2. API Key 权限**
- ✅ 允许交易（含合约）
- ❌ 不要勾选"允许提现"
- 绑定 IP 白名单

**3. 配置 `.env`**
```bash
OKX_API_KEY=...
OKX_SECRET=...
OKX_PASSPHRASE=...
```

**4. 改 `config.py`**
```python
OKX_LIVE_MODE = True
OKX_DEFAULT_LEVERAGE = 10
```

### 两所同时下单（`PRIMARY_EXCHANGE='both'`）

适合想分散交易对手方风险的场景。每笔信号在两所**分别开一笔独立的 Trade**，
保证金各占一半。止盈止损是**两所独立执行**的（价格在 A 所触发止盈不会自动关 B 所），
各自走 `reduceOnly` 的真实平仓单。

风控层面：`record_trade_opened` 会被调用两次（一次 Binance、一次 OKX），
所以 `RISK_MAX_DAILY_TRADES` 在 both 模式下会更快被消耗。如果不希望这样，
把 `DEFAULT_STAKE` 降一半，或把 `RISK_MAX_DAILY_TRADES` 调大。

### 实盘安全特性

- **幂等键**：所有开仓/平仓都带 `client_order_id` (Binance) / `clOrdId` (OKX)，
  网络重试不会导致重复下单。
- **滑点告警**：成交均价与下单前 ticker 偏差 > `SLIPPAGE_ALERT_PCT`（默认 0.5%）
  自动推 TG 告警，但不回滚（防止止损单在极端行情下被拒）。
- **reduceOnly 平仓**：平仓订单强制 `reduceOnly=True`，即使计算错误也不会反向开仓。
- **失败不污染状态**：交易所下单失败时**不记账**（`record_trade_opened` 不调用），
  不会产生"幽灵亏损"。
- **平仓失败告警**：如果系统已把 JSON 标记为 closed 但交易所下单失败（网络/权限问题），
  会推 TG 警告让你**手动去交易所平仓**，避免仓位裸奔。

### TG 实盘指令

- `/balance` — 显示动态余额 + 各交易所实盘余额 + 当前路由模式
- `/positions` — 每笔持仓带 `[BINANCE]` / `[OKX]` 标签
- 触发信号开仓时推送会带 `[BINANCE 实盘]` / `[OKX 实盘]` / `[双所对冲]` 前缀

## 🔐 管理员面板（实盘开关与 API 密钥 web UI）

一个"高安全、扫不到"的配置面板，让你不用 SSH 上服务器改 `config.py` / `.env`。
所有开关和 API key 都能在浏览器里改，修改后 **30 秒内跨所有进程生效**
（scheduler / realtime_monitor / dashboard 都会热加载）。

### 安全模型（8 层防御）

| 层级 | 防御 |
|------|------|
| **L1 IP 白名单** | `ADMIN_ALLOWED_IPS` 设置后，非白名单 IP 全部 404 |
| **L2 Secret URL 前缀** | 面板挂在 `/<ADMIN_URL_SECRET>/`；secret 未设则 blueprint 不加载，扫描器根本扫不到 |
| **L3 IP 失败锁定** | 每 IP 5 次登录失败 → 锁 30 分钟；锁定期**返回 404**（不是 401），攻击者连"路径存在"都判断不了 |
| **L4 双因子** | 密码（PBKDF2-SHA256, 600k 迭代）**+** TOTP（Google Authenticator 兼容） |
| **L5 Session** | 30 分钟空闲 / 4 小时绝对过期；写操作额外要求 5 分钟内有新鲜 TOTP |
| **L6 CSRF** | 所有 POST 必须带 `X-Admin-CSRF` 头 |
| **L7 审计 + TG** | 所有写操作记到 `admin_audit.log` 并推 TG |
| **L8 响应头** | `X-Robots-Tag: noindex, X-Frame-Options: DENY, CSP: strict`，无缓存 |

### 启用面板

**1. 生成一个足够长的 secret URL 前缀**
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# 输出类似: Kx3mQ8-pLz2yH9rW5vNcE7bDgJ6fSu4AtT1oIkXzM0s
```

**2. 写 `.env`**
```bash
# 必须，面板挂载路径的 secret
ADMIN_URL_SECRET=Kx3mQ8-pLz2yH9rW5vNcE7bDgJ6fSu4AtT1oIkXzM0s

# 强烈推荐（只让你的出口 IP 访问）
ADMIN_ALLOWED_IPS=123.45.67.89

# 推荐（反向代理强制 HTTPS 后开）
DASHBOARD_FORCE_HTTPS_COOKIE=1

# 推荐（跨重启保留登录态）
DASHBOARD_SECRET_KEY=<另一个 32 字节随机串>
```

**3. 重启 dashboard**
启动时会看到：
```
🔐 Admin Panel 已挂载: /<ADMIN_URL_SECRET>/  (secret 长度=43)
```

**4. 首次访问：浏览器打开 `https://你的域名/Kx3mQ8-.../setup`**

> 🔐 **M-8 安全加固**：从 v5.x 起，访问 `/setup` 端点要求宿主机存在
> `.admin_setup_token` 文件作为运维明确授权。首次部署时执行：
> ```bash
> ssh 服务器
> touch /path/to/altcoin-shadow-system/.admin_setup_token
> ```
> setup 成功后此文件会被自动删除。如果忘记 setup 凭证需重做，先 `rm admin_secrets.json`
> 再 `touch .admin_setup_token`。

- 用 Google Authenticator / 1Password 扫 TOTP 二维码
- 设置 ≥12 字符的密码
- 输入 Authenticator 当前 6 位码确认绑定

之后每次登录都需要**密码 + 6 位动态码**。

### 面板能做什么

- **实盘开关**：`LIVE_MODE` / `OKX_LIVE_MODE` / `PRIMARY_EXCHANGE` 一键切换
- **API 凭证管理**：Binance/OKX 的 key、secret、passphrase 都在这儿改；
  存到独立的 `admin_secrets.json`（0600 权限），和 `.env` 解耦
- **风控参数**：单日亏损上限、最大开仓次数、持仓占比等都能调（都有硬上下界校验）
- **止盈止损**：TP1/TP2 乘数、硬止损、TP1 平仓比例
- **审计日志**：所有变更带时间戳、IP、操作类型

每次写操作都会：
1. 前端弹出 TOTP 输入框，6 位数字输完自动提交
2. 审计日志新增一条 JSON 记录
3. TG 推送告警（`⚙️ Admin 修改运行时配置` 或 `🔑 Admin 更新 API 凭证`）

### 如果出事了

- **忘密码或 TOTP**：SSH 上服务器 `rm admin_secrets.json`，重新 setup
- **凭证被泄露**：面板里点「清除凭证」按钮，会同时关掉对应的 LIVE_MODE
- **账户被爆破**：查看 `admin_audit.log`，看 IP 和失败记录；
  必要时 `rm .admin_ratelimit.json` 手动解锁
- **面板被人找到了**：换 `ADMIN_URL_SECRET`，重启 dashboard；
  同时把 `DASHBOARD_SECRET_KEY` 也换掉，踢掉所有现有 session

### 不要做的事

- ❌ 不要把 `ADMIN_URL_SECRET` 写到任何公开地方（聊天记录 / 截图 / git commit）
- ❌ 不要用短密码（强制要求 ≥12 字符）
- ❌ 不要在公网上跑 HTTP（必须 nginx/caddy 强制 HTTPS，否则登录密码会明文传输）
- ❌ 不要把 dashboard 绑到 0.0.0.0 公网直接暴露；应该只绑 localhost，反向代理过来

### 反向代理 access log 脱敏（NF-2 必读 runbook）

> ⚠️ **强烈建议**：上线前完成本节配置，否则**反向代理的 access log 会原样记录完整 admin URL，间接泄露 `ADMIN_URL_SECRET`**。
>
> 即使你把 dashboard 自身的启动日志做了脱敏，nginx / caddy / Cloudflare 的访问日志仍会把这种请求记下来：
>
> ```
> 1.2.3.4 - - [16/May/2026:10:30] "GET /Kx3mQ8-pLz2yH9rW5vNcE7bDgJ6fSu4AtT1oIkXzM0s/login HTTP/2" 200
> ```
>
> 一旦日志被同步到 ELK / Loki / 云厂商日志服务，secret 就被多名运维 / 工单系统 / 备份系统看到。

#### nginx 配置示例

把 `ADMIN_URL_SECRET` 那一段在写日志前替换成 `/admin-redacted/`：

```nginx
http {
    # 1. 把所有形如 /<32+ 字符 base64-url> 的前缀替换为占位符
    map $request_uri $loggable_uri {
        "~^/[A-Za-z0-9_-]{32,}/"  "/admin-redacted/";
        default                    $request_uri;
    }

    # 2. 自定义 access log 格式，使用 $loggable_uri 而不是 $request_uri
    log_format secure '$remote_addr - $remote_user [$time_local] '
                      '"$request_method $loggable_uri $server_protocol" '
                      '$status $body_bytes_sent '
                      '"$http_referer" "$http_user_agent"';

    access_log /var/log/nginx/access.log secure;

    # 3. error_log 不会自动脱敏，建议保持 warn 级别避免记下完整 URI
    error_log  /var/log/nginx/error.log warn;

    server {
        listen 443 ssl http2;
        server_name your-domain.example;

        # ... ssl 配置 ...

        location / {
            proxy_pass http://127.0.0.1:5000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }
}
```

#### Caddy v2 配置示例

```caddy
your-domain.example {
    # 自定义日志，把 admin secret 段替换为占位符
    log {
        output file /var/log/caddy/access.log
        format transform `{request>uri}` {
            replace `/[A-Za-z0-9_-]{32,}/` `/admin-redacted/`
        }
    }

    reverse_proxy 127.0.0.1:5000
}
```

> Caddy 老版本不支持 `transform` formatter，可以改用 `filter` + 正则 sub 模块；
> 也可以选择 `format json` 后用日志收集端（vector / fluentbit）做脱敏。

#### Cloudflare / CDN 用户

Cloudflare 自身保留 7 天访问日志，免费版无法关闭也无法过滤 URI 字段。**不要**把启用了 admin 面板的域名直接暴露到 Cloudflare 后面，应该：

1. Admin 面板单独走一个**未接 CF 的子域名**，DNS 直连到回源服务器（`A` 记录灰色云朵）
2. 或者 admin 面板走**纯内网/VPN**（推荐）— 比如 Tailscale / WireGuard，反向代理只监听内网网卡

#### 如何验证脱敏生效

```bash
# 触发一次正常的 admin 访问（替换成你自己的 secret）
curl -k "https://your-domain.example/Kx3mQ8.../login"

# 查看 access log，应该只看到 /admin-redacted/，不应看到 Kx3mQ8...
tail -n 5 /var/log/nginx/access.log | grep -E "Kx3mQ8|admin-redacted"
# 期望输出：…"GET /admin-redacted/ HTTP/2"…  ← 只有占位符
# 失败输出：…"GET /Kx3mQ8…/login HTTP/2"…   ← 仍然有 secret，说明 map 没生效
```

#### 已经泄露怎么办

如果检查后发现历史 access log 已经写过 secret：

1. **立刻轮换 secret**：生成新的 `ADMIN_URL_SECRET`、`DASHBOARD_SECRET_KEY`，重启 dashboard
2. **清理已写入的日志**：本机 `truncate -s 0`，远端日志系统（ELK/Loki/CloudWatch）走管理后台删除
3. **检查日志备份**：S3 / 备份磁带里的副本一并清理或加密归档
4. **审计 admin_audit.log**：确认没有未授权的访问记录

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
