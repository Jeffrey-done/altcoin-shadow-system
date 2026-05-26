#!/usr/bin/env python3
"""
策略参数默认值（代码层兜底）

⚠️ 本文件为"最低优先级兜底"。实际生效值的合并顺序（高→低）：
  1. runtime_config.json（admin panel 实时修改）
  2. admin_secrets.json settings（凭证附带配置）
  3. config/*.yaml（项目级用户配置）
  4. 本文件 config/_defaults.py

请勿直接修改本文件来调整参数！
  - 临时调整 → admin panel (runtime_config.json)
  - 永久调整 → config/system.yaml 或 config/strategy.yaml

本文件仅在 YAML 中某个字段未定义时作为兜底值。
"""

# ══════════════════════════════════════════════════════════════════
#  全局默认值（向后兼容，同时作为 Binance 默认）
# ══════════════════════════════════════════════════════════════════
ACCOUNT_BALANCE = 100          # 默认账户本金（USDT）— 向后兼容
LEVERAGE = 10                  # 默认杠杆倍数（向后兼容，对应 Binance）
MAX_OPEN_TRADES = 3            # 最大同时持仓数（用于自动计算 base_stake）
DEFAULT_STAKE = 33             # 自动计算: ACCOUNT_BALANCE / MAX_OPEN_TRADES（向后兼容保留）
# B+D 仓位模式：base_stake = account_balance / max_open_trades
# 实际开仓 = base_stake × 评分系数(A=1.0, B=0.5) × regime系数(牛市0.3~熊市1.5)
# 注意：总敞口永远 ≤ 可用保证金（3 笔 × 33U = 99U 上限）

# ── 仓位模式（v5.1 新增）──────────────────────────────────────────
# 'manual':       手动模式（向后兼容）。DEFAULT_STAKE / RISK_MAX_DAILY_LOSS /
#                 COMPOUND_STEP / COMPOUND_INCREASE 用上面/下面写死的值或 admin
#                 面板手填的值。
# 'proportional': 比例模式。以 BASELINE_BALANCE (100U) 为基准，按
#                 实际余额 / BASELINE_BALANCE 的比例自动缩放上述 4 个金额参数。
#                 实际余额来源：LIVE_MODE 时从交易所 fetch_balance 拉取（60s 缓存），
#                 否则用 ACCOUNT_BALANCE 手填值。
#                 COMPOUND_MAX_STAKE 不参与缩放，保持绝对值上限（默认 300U）。
#                 杠杆、止盈止损百分比、风控笔数、信号阈值等"非金额"参数也不变。
POSITION_MODE = 'manual'
BASELINE_BALANCE = 100         # 比例模式的基准本金（即默认 4 个金额参数对应的本金）

# ══════════════════════════════════════════════════════════════════
#  实盘模式（危险！确认策略验证通过后再开启）
# ══════════════════════════════════════════════════════════════════
LIVE_MODE = False              # False=影子交易（纸上模拟） True=真实下单
# ⚠️ 开启前确认：
#   1. 纸上交易至少2周，胜率>50%，盈亏比>1.5
#   2. .env 里配置了 BINANCE_API_KEY 和 BINANCE_SECRET
#   3. Binance 账户已开通合约并设置好杠杆
#   4. 从小仓位开始（先改 account_balance 或 max_open_trades 调节）

# ══════════════════════════════════════════════════════════════════
#  扫描过滤
# ══════════════════════════════════════════════════════════════════
VOL_MIN = 500_000              # 24h 成交量下限（USDT）
PRICE_MAX = 50.0                # 价格上限（扩展至中盘币 <50U，覆盖更多品种）
PCT_24H_MIN = 10               # 24h 涨幅最低要求（%）

# ══════════════════════════════════════════════════════════════════
#  RSI 参数
# ══════════════════════════════════════════════════════════════════
RSI_PERIOD = 14                # RSI 计算周期
DAILY_RSI_MIN = 75             # 日线 RSI 超买阈值（降低至75扩大候选池覆盖）
H4_RSI_ENTER = 70              # 4h RSI 回落进入阈值
H4_RSI_DROP = 10               # 4h RSI 需从峰值回落的点数
H4_RSI_PEAK_LOOKBACK = 10     # 4h RSI 峰值回溯 K 线数

# ══════════════════════════════════════════════════════════════════
#  妖币识别
# ══════════════════════════════════════════════════════════════════
OI_CHANGE_MIN = 0.30           # OI 24h 涨幅下限（30%）
FUNDING_MAX = 0.05             # 资金费率上限（超过跳过，空头成本太贵）
# M-2 修复：极端负费率（多头付空头反向，做空者要付费）→ 持仓 24h 资金成本过高，
# 直接跳过，避免依赖 signal_score 仅扣 5 分却被其他 bonus 抵消的隐患。
# 基准：默认每 8h 扣一次费率，做空亏损 ≈ -funding × 仓位倍数 × 持仓时长/8h
# -0.03%/8h × 24h = -0.09%/天 × 10x ≈ 0.9% 名义仓位资金成本（约 5U / 50U 保证金）
FUNDING_MIN = -0.03            # 资金费率下限（低于此值跳过，做空持仓成本过高）
FUNDING_HOT = 0.03             # 多头过热阈值（%/8h）

# ══════════════════════════════════════════════════════════════════
#  弃盘点
# ══════════════════════════════════════════════════════════════════
ABANDON_BODY_DROP_PCT = 3      # 单根 1H K 线实体下跌阈值（%）
ABANDON_CONSECUTIVE = 2        # 连续满足的 K 线数
ABANDON_OI_DROP_PCT = 0.02     # OI 下降比例阈值

# ══════════════════════════════════════════════════════════════════
#  止盈档位 — 短线模式（做空：价格下跌 = 盈利）
# ══════════════════════════════════════════════════════════════════
# 短线止盈（50刀本金 × 10x，目标日赚10~25U）
# TP1: 价格跌 5% → entry * 0.95 → 盈利 = 500 * 5% * 50% = 12.5U
# TP2: 价格跌 8% → entry * 0.92 → 盈利 = 250 * 8% = 20U
TP1_MULTIPLIER = 0.95          # 第一档止盈价 = 入场价 × 此值（-5%）
TP2_MULTIPLIER = 0.92          # 第二档止盈价 = 入场价 × 此值（-8%，回测优化：10%→8%提升触发率4.4倍）
TP1_CLOSE_RATIO = 0.5          # 第一档平仓比例（50% 仓位）

# ══════════════════════════════════════════════════════════════════
#  硬止损（无条件止损）
# ══════════════════════════════════════════════════════════════════
HARD_STOP_LOSS_PCT = 5.0       # 价格反弹 5% 无条件平仓（回测优化：3%→5%减少假突破洗盘）
# 做空硬止损价 = entry * (1 + HARD_STOP_LOSS_PCT/100)
# 最大亏损 = 500U * 5% = 25U（本金的 25%）

# ══════════════════════════════════════════════════════════════════
#  移动止损（M5 语义明确化：相对回撤比例）
# ══════════════════════════════════════════════════════════════════
TRAIL_STOP_ACTIVATE_PCT = 3    # 最高盈利达到此 % 后激活移动止损（短线降低门槛）
TRAIL_STOP_RETRACE_RATIO = 0.4 # 从最高盈利回撤此比例触发平仓（0.4 = 40%）
# 触发条件示例（做空）：
#   best_pnl=5%, ratio=0.4 → trigger at pnl_pct = 5% * (1-0.4) = 3%
#   → trail_stop_price = entry * (1 - 0.03) = entry * 0.97
#   比硬止损 entry*1.05 先触发 → 保护已有浮盈
#
# 历史注记：旧字段 TRAIL_STOP_DRAWDOWN_PCT（"绝对点数回撤"语义）已于 M5 移除，
# 因为该语义存在 bug：best=3%, drawdown=10% → trail_stop = entry*1.07 > 硬止损
# entry*1.05，导致移动止损被硬止损截胡永远不生效。新字段 TRAIL_STOP_RETRACE_RATIO
# 采用"相对回撤比例"语义，与硬止损解耦。

# ══════════════════════════════════════════════════════════════════
#  时间止损
# ══════════════════════════════════════════════════════════════════
MAX_HOLD_DAYS = 1              # 最大持仓天数（短线改为24小时）
TIME_STOP_MIN_PROFIT_PCT = 3   # 超时但盈利超过此 % 则不平

# ══════════════════════════════════════════════════════════════════
#  每日风控
# ══════════════════════════════════════════════════════════════════
RISK_MAX_DAILY_LOSS = 30       # 单日最大亏损上限（USDT），达到后当日停止开仓
RISK_MAX_DAILY_TRADES = 3      # 单日最大开仓次数（留出同日补位空间，避免第1笔止损后一天报废）
RISK_CONSECUTIVE_LOSS_PAUSE = 3  # 连续亏损 N 次后暂停 24 小时
RISK_PAUSE_HOURS = 24          # 暂停时长（小时）
RISK_MAX_POSITION_PCT = 0.5    # 最大持仓占余额比例（50%），同一时刻最多只把一半本金压在持仓里

# ══════════════════════════════════════════════════════════════════
#  信号评分 & 动态仓位
# ══════════════════════════════════════════════════════════════════
# 评分维度（满分100）：
#   RSI 强度（0~25）: RSI越高越强
#   妖币评分（0~25）: yao_score 0/1/2/3 → 0/8/16/25
#   触发方式（0~25）: 弃盘点25 > 4hRSI回落15
#   OI+资金费率（0~25）: OI涨幅+费率热度
SIGNAL_SCORE_ENABLED = True    # 是否启用信号评分

# 仓位分级（根据评分决定保证金）
SCORE_FULL_THRESHOLD = 70      # ≥70分：全仓（DEFAULT_STAKE）
SCORE_HALF_THRESHOLD = 40      # 40~69分：半仓（DEFAULT_STAKE × 0.5）
SCORE_SKIP_THRESHOLD = 40      # <40分：跳过不开仓

# ══════════════════════════════════════════════════════════════════
#  BTC 趋势过滤（全局开关）
# ══════════════════════════════════════════════════════════════════
BTC_FILTER_ENABLED = True      # 是否启用 BTC 趋势过滤
BTC_CRASH_THRESHOLD = -5.0     # BTC 24h 跌幅超过此值时暂停做空山寨（%）
# 原因：BTC暴跌时山寨超跌严重，但反弹也猛，此时做空容易被反弹打止损
BTC_PUMP_THRESHOLD = 8.0       # BTC 24h 涨幅超过此值时信号加分（牛市山寨更容易冲高回落）

# ══════════════════════════════════════════════════════════════════
#  影子并行模式
# ══════════════════════════════════════════════════════════════════
SHADOW_PARALLEL = True         # 实盘开启后是否同时保留影子交易记录
# 开启后每个信号会同时产生两笔 Trade：
#   - exchange='shadow'（纸上模拟，用 ticker 价格）
#   - exchange='binance'/'okx'（真实下单，用成交均价）
# 影子交易不占风控额度，仅用于数据对比

# ══════════════════════════════════════════════════════════════════
#  自动复利
# ══════════════════════════════════════════════════════════════════
AUTO_COMPOUND_ENABLED = True   # 是否开启自动复利
COMPOUND_STEP = 50             # 每累计盈利 50U，保证金加 25U
COMPOUND_INCREASE = 25         # 每步增加的保证金
COMPOUND_MAX_STAKE = 300       # 单笔保证金上限（防止过度集中）
# 逻辑：effective_stake = DEFAULT_STAKE + (total_realized_pnl // COMPOUND_STEP) * COMPOUND_INCREASE
#        但不超过 COMPOUND_MAX_STAKE

# ══════════════════════════════════════════════════════════════════
#  OKX 交叉验证（仅用作数据源，不做交易）
# ══════════════════════════════════════════════════════════════════
OKX_ENABLED = True             # 是否启用 OKX 作为辅助数据源
# OKX 费率参数（OKX 费率计算公式不同，阈值需独立设置）
OKX_FUNDING_HOT = 0.02        # OKX 多头过热阈值（%/8h，比币安略低）
OKX_OI_CHANGE_MIN = 0.20      # OKX OI 变化阈值（20%，比币安低因为OKX体量小）

# 交叉验证加分（两所数据一致时，信号评分额外加分）
OKX_CROSS_VALIDATE_ENABLED = False    # hotfix: 暂时关闭 OKX 交叉验证，避免候选确认阻塞
OKX_CROSS_VALIDATE_BONUS = 8         # 交叉验证通过时额外加分（满分100中）

# OKX 实盘交易（多策略架构下空头/多头都通过 OKX_LIVE_MODE 控制；默认关闭）
OKX_LIVE_MODE = False          # True=通过 OKX API 真实下单（需配置 OKX_API_KEY/SECRET/PASSPHRASE）
OKX_DEFAULT_LEVERAGE = 10      # OKX 默认杠杆倍数

# ══════════════════════════════════════════════════════════════════
#  实盘路由（当两个 LIVE_MODE 都开启时生效）
# ══════════════════════════════════════════════════════════════════
# PRIMARY_EXCHANGE 控制信号触发时往哪家交易所下单：
#   'binance' → 只在币安开仓（推荐，流动性深、API 成熟）
#   'okx'     → 只在 OKX 开仓
#   'both'    → 两所同时开仓（保证金 stake 各一半，等效于分散交易对手风险）
#   'auto'    → 按币种在哪家有合约决定；两所都有则按 PRIMARY_EXCHANGE_FALLBACK
# 注意：如果只打开一个 LIVE_MODE，本字段被忽略，直接走那一家。
PRIMARY_EXCHANGE = 'binance'
PRIMARY_EXCHANGE_FALLBACK = 'binance'   # 'auto' 模式下两所都有合约时选谁

# 滑点告警阈值（成交均价 vs ticker 偏差 %，超过推 TG 但不回滚）
# M-4 修复：从 0.5% 上调到 1.0%，原因：
#   - 策略目标币 PRICE_MAX < 1U，VOL_MIN = 50万 U，本质是低流动性小币
#   - 名义仓位 500U（DEFAULT_STAKE 50 × 10x）vs 24h 50万 ≈ 0.1% 量比
#   - 市价单冲击 0.5% 是常态，0.5% 阈值会产生告警疲劳
#   - 真正异常（>1.5%）才告警，配合 weekly_report 滑点统计审计
SLIPPAGE_ALERT_PCT = 1.0

# ══════════════════════════════════════════════════════════════════
#  候选池管理
# ══════════════════════════════════════════════════════════════════
CANDIDATE_EXPIRE_HOURS = 12    # 未触发候选过期时间（小时），超时说明超买窗口已过

# H11: 候选确认（check_candidates）耗时控制
# 候选池一旦扩大（比如降低 DAILY_RSI_MIN 后从 30 涨到 150+），原来的串行循环
# 会累积成 4-5 分钟，叠加 Binance rate limit 把外层 600s 任务超时打爆。三层防御：
#   1) 整轮预算 480s：留 120s 余量给收尾（写盘 + 推送）
#   2) 单候选硬超时 30s：超过就跳过下一个，避免某个慢币种拖死整轮
#   3) 评估阶段并发：评估期纯只读，可线程池并行（实际开仓仍串行加锁）
CHECK_CANDIDATES_BUDGET_SEC = 480     # 整轮 evaluate 预算（秒）；到点优雅退出
CHECK_CANDIDATES_PER_CANDIDATE_SEC = 30  # 单个候选硬超时（秒）；超过跳下一个
CHECK_CANDIDATES_PARALLELISM = 4      # 候选评估的并发线程数（Binance 限速 ~10 RPS，4 比较安全）
CHECK_CANDIDATES_OPEN_EXEC_TIMEOUT_SEC = 45  # 开仓执行阶段总超时（秒）；避免单路由卡死拖垮整轮
CHECK_CANDIDATES_HARD_TIMEOUT_SEC = 120   # 候选确认函数级硬超时（秒），兜底防止任何阶段卡死
CHECK_CANDIDATES_INTERVAL_MINUTES = 5  # 候选确认轮询间隔（分钟）；准实时建议 5，稳态可回调到 15

# ══════════════════════════════════════════════════════════════════
#  回测滑点 & 手续费
# ══════════════════════════════════════════════════════════════════
BACKTEST_SLIPPAGE_PCT = 0.1    # 滑点模拟（每笔交易 %）
BACKTEST_FEE_PCT = 0.04        # taker 手续费（每边 %）

# ══════════════════════════════════════════════════════════════════
#  批量回测
# ══════════════════════════════════════════════════════════════════
BATCH_BACKTEST_SYMBOLS = [
    # 小盘 Meme 币（原始品种宇宙）
    'PEPE/USDT',
    'DOGE/USDT',
    'SHIB/USDT',
    'FLOKI/USDT',
    'BONK/USDT',
    'WIF/USDT',
    'PEOPLE/USDT',
    'ORDI/USDT',
    # 中盘币（$1~$50，扩展品种宇宙 — Phase 6）
    'FET/USDT',
    'RNDR/USDT',
    'INJ/USDT',
    'SEI/USDT',
    'SUI/USDT',
    'APT/USDT',
    'ARB/USDT',
    'OP/USDT',
    'NEAR/USDT',
    'FIL/USDT',
    'RUNE/USDT',
    'TIA/USDT',
]
BATCH_BACKTEST_DAYS = 90       # 批量回测默认天数
BATCH_CORRELATION_THRESHOLD = 0.7  # 相关性阈值（高于此值的币对避免同时开仓）

# ══════════════════════════════════════════════════════════════════
#  周报配置
# ══════════════════════════════════════════════════════════════════
WEEKLY_REPORT_ENABLED = True   # 是否开启周报
WEEKLY_ROI_GRADE_A = 15        # 周ROI >= 15% 评级 A
WEEKLY_ROI_GRADE_B = 5         # 周ROI >= 5%  评级 B
WEEKLY_ROI_GRADE_C = 0         # 周ROI >= 0%  评级 C（< 0% 为 F）

# ══════════════════════════════════════════════════════════════════
#  冷却期 & 任务超时
# ══════════════════════════════════════════════════════════════════
COOLDOWN_HOURS = 24            # 同一币种止损平仓后冷却期（小时）
# M-7 修复：冷却作用域，决定多账户场景下"某账户止损"是否影响"其他账户"开同币
#   'global'      → 任一账户止损 → 全局冷却（保守，原默认行为）
#   'per_account' → 仅当前账户止损影响当前账户（多账户对冲场景必需）
# 单账户用户保持 'global' 即可；多账户对冲请改 'per_account'。
COOLDOWN_SCOPE = 'global'
TASK_TIMEOUT_SECONDS = 600     # 任务超时秒数（10分钟）

# ══════════════════════════════════════════════════════════════════
#  自动优化建议
# ══════════════════════════════════════════════════════════════════
AUTO_OPTIMIZE_ENABLED = True   # 是否开启自动回测优化建议
AUTO_OPTIMIZE_DAY = 0          # 周几运行（0=周一）

# ══════════════════════════════════════════════════════════════════
#  多交易所价格确认
# ══════════════════════════════════════════════════════════════════
PRICE_DIVERGENCE_MAX_PCT = 2.0  # 开仓前Binance/OKX价格偏差上限（%）

# ══════════════════════════════════════════════════════════════════
#  交易归档
# ══════════════════════════════════════════════════════════════════
TRADES_ARCHIVE_DAYS = 30       # 已平仓交易归档天数
TRADES_ARCHIVE_FILE = 'altcoin_trades_archive.json'

# ══════════════════════════════════════════════════════════════════
#  WebSocket 断线告警
# ══════════════════════════════════════════════════════════════════
WS_DISCONNECT_ALERT_MINUTES = 5  # WebSocket断线超过N分钟告警
WS_DISCONNECT_FALLBACK_SEC = 30  # H5: WS 断线超过 N 秒立即降级到主动 REST 轮询模式
                                 # （5 分钟才告警的旧时序仅作 escalation,不再让持仓裸奔）
WS_FALLBACK_POLL_INTERVAL_SEC = 10  # 降级模式下的 REST 轮询间隔（秒）

# ══════════════════════════════════════════════════════════════════
#  OKX 费率相关（用于交叉验证，仅数据源，非交易参数）
# ══════════════════════════════════════════════════════════════════
# exchange_manager.cross_validate_funding / find_cross_exchange_arb_opportunities
# 用这些阈值判断"两所费率都极端负"或"两所费率差过大"的数据信号（不下单）。
FUNDING_ARB_MIN_RATE = -0.03          # Binance 极端负费率阈值（%/8h）
OKX_FUNDING_ARB_MIN_RATE = -0.02      # OKX 极端负费率阈值
OKX_CROSS_ARB_MIN_DIVERGENCE = 0.10   # 两所费率差异显著阈值（%）

# ══════════════════════════════════════════════════════════════════
#  Gate.io 交易所配置 — 已移除
# ══════════════════════════════════════════════════════════════════
# Gate.io 配置已在代码审计中移除（2026-05）：
#   - 路由层 _resolve_exchange_routes() 从未支持 'gate' 分支
#   - GATE_LIVE_MODE 始终为 False，实际从未使用
#   - exchange_manager.get_gate() 虽存在但从未被主流程调用
# 如需重新启用 Gate.io，请先在 altcoin_scanner._resolve_exchange_routes() 中
# 添加 'gate' 路由分支，再恢复此处配置。

# ══════════════════════════════════════════════════════════════════
#  每交易所独立账户配置 (v6.0)
# ══════════════════════════════════════════════════════════════════
# 每个交易所拥有独立的资金池、杠杆、仓位、风控、复利、止盈止损参数。
# 旧代码通过 config.LEVERAGE 等全局变量访问仍然正常工作（向后兼容），
# 新代码通过 get_exchange_account_config('binance') 获取某个交易所的完整配置。
#
# ⚠️ 数据源优先级说明（L-6 修复）：
#   本字典是"代码层硬编码默认值"（最低优先级兜底）。
#   实际生效值的合并顺序（高→低）：
#     1. runtime_config.json _exchanges 段（admin panel 实时修改）
#     2. admin_secrets.json 中的 per-exchange settings
#     3. config/system.yaml 的 exchanges 段（项目级用户配置）
#     4. 本字典 EXCHANGE_ACCOUNTS（代码层默认值）
#   如需修改默认值，请优先修改 config/system.yaml 而非本文件。
#   本文件仅在 system.yaml 不存在或字段缺失时作为兜底。
#
# 设计理念：
#   - 用户可能在 Binance 放 100U 跑 10x，在 OKX 放 200U 跑 5x
#   - 每个交易所的风控应该独立（Binance 止损不影响 OKX 开仓）
#   - 每个交易所可以有不同的止盈止损策略

EXCHANGE_ACCOUNTS = {
    'binance': {
        'enabled': True,
        'live_mode': False,            # Binance 实盘开关（对应旧 LIVE_MODE）
        'account_balance': 100,        # Binance 账户本金 (USDT)
        'max_open_trades': 3,          # 最大同时持仓数
        'leverage': 10,                # Binance 杠杆倍数
        'default_stake': 33,           # 自动计算: account_balance / max_open_trades
        'slippage_alert_pct': 1.0,     # 滑点告警阈值 (%)
        # 风控
        'risk': {
            'max_daily_loss': 30,      # 单日最大亏损 (USDT)
            'max_daily_trades': 3,     # 单日最大开仓次数
            'consecutive_loss_pause': 3,  # 连亏暂停阈值
            'max_position_pct': 0.5,   # 最大持仓占比
            'cooldown_hours': 24,      # 止损后冷却期 (h)
        },
        # 复利
        'compound': {
            'enabled': True,
            'step': 50,                # 每累计盈利 N U 步进
            'increase': 25,            # 每步增加保证金 (U)
            'max_stake': 300,          # 单笔保证金上限 (U)
        },
        # 止盈止损
        'tp_sl': {
            'tp1_multiplier': 0.95,
            'tp2_multiplier': 0.92,
            'tp1_close_ratio': 0.5,
            'hard_stop_loss_pct': 5.0,
        },
    },
    'okx': {
        'enabled': True,
        'live_mode': False,            # OKX 实盘开关（对应旧 OKX_LIVE_MODE）
        'account_balance': 100,        # OKX 账户本金 (USDT)
        'max_open_trades': 3,          # 最大同时持仓数
        'leverage': 10,                # OKX 杠杆倍数
        'default_stake': 33,           # 自动计算: account_balance / max_open_trades
        'slippage_alert_pct': 1.0,
        'cross_validate': False,       # 是否用作交叉验证数据源
        # 风控
        'risk': {
            'max_daily_loss': 30,
            'max_daily_trades': 3,
            'consecutive_loss_pause': 3,
            'max_position_pct': 0.5,
            'cooldown_hours': 24,
        },
        # 复利
        'compound': {
            'enabled': True,
            'step': 50,
            'increase': 25,
            'max_stake': 300,
        },
        # 止盈止损
        'tp_sl': {
            'tp1_multiplier': 0.95,
            'tp2_multiplier': 0.92,
            'tp1_close_ratio': 0.5,
            'hard_stop_loss_pct': 5.0,
        },
    },
}

# 实盘路由配置
EXCHANGE_ROUTING = {
    'primary_exchange': 'binance',     # binance | okx | both | auto
    'primary_fallback': 'binance',     # auto 模式下两所都有合约时选谁
    'price_divergence_max_pct': 2.0,   # 开仓前跨交易所价格偏差上限 (%)
}


def get_exchange_account_config(exchange: str) -> dict:
    """
    获取指定交易所的独立账户配置。

    Args:
        exchange: 交易所名称 ('binance', 'okx', 'gate')

    Returns:
        该交易所的完整配置字典。如果交易所未定义，返回 Binance 的配置作为默认。

    用法:
        cfg = get_exchange_account_config('okx')
        leverage = cfg['leverage']       # 10
        stake = cfg['default_stake']     # 30
        max_loss = cfg['risk']['max_daily_loss']  # 30
    """
    exchange = exchange.lower()
    if exchange in EXCHANGE_ACCOUNTS:
        return dict(EXCHANGE_ACCOUNTS[exchange])
    # 未知交易所 → 返回 Binance 默认（向后兼容）
    return dict(EXCHANGE_ACCOUNTS.get('binance', {}))


def get_exchange_param(exchange: str, key: str, default=None):
    """
    获取指定交易所的单个参数值。支持点号分隔的嵌套路径。

    Args:
        exchange: 交易所名称
        key: 参数路径，支持 'leverage' 或 'risk.max_daily_loss' 形式
        default: 未找到时的默认值

    Returns:
        参数值，未找到返回 default

    用法:
        get_exchange_param('okx', 'leverage')          → 10
        get_exchange_param('okx', 'risk.max_daily_loss')  → 30
        get_exchange_param('gate', 'compound.step')    → 50
    """
    cfg = get_exchange_account_config(exchange)
    keys = key.split('.')
    current = cfg
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k)
        else:
            return default
        if current is None:
            return default
    return current


def is_exchange_live(exchange: str) -> bool:
    """检查指定交易所是否开启了实盘模式"""
    cfg = get_exchange_account_config(exchange)
    return bool(cfg.get('live_mode', False))


def get_active_live_exchanges() -> list:
    """返回所有开启了实盘模式的交易所名称列表"""
    result = []
    for name, cfg in EXCHANGE_ACCOUNTS.items():
        if cfg.get('enabled') and cfg.get('live_mode'):
            result.append(name)
    return result



# ══════════════════════════════════════════════════════════════════
#  S3 修复（2026-05）: 信号评分后端
# ══════════════════════════════════════════════════════════════════
# 决定 ``scoring.score_signal()`` 内部按何种顺序尝试 scorer：
#   'auto'        — ml > multifactor > linear（**默认**，模型不可用自动降级）
#   'ml'          — 只用 ML 评分；不可用直接 fallback linear
#   'multifactor' — 优先多因子；OHLCV 不可用 fallback linear
#   'linear'      — 始终用旧 4×25 评分（最稳定）
SCORING_BACKEND = 'auto'


# ══════════════════════════════════════════════════════════════════
#  S4 修复（2026-05）: DB 写模式
# ══════════════════════════════════════════════════════════════════
# 决定 ``db.compat.save_*`` 在 DB 与 JSON 之间的写策略：
#   'dual'         — DB + JSON 都写（**默认**，向后兼容老 dashboard）
#   'db-canonical' — DB 唯一真源；JSON 只在 dashboard 周期 export 时刷新
#   'json-only'    — DB 关闭（兜底，无 SQLAlchemy 时也能跑）
# 也可由环境变量 ``DB_WRITE_MODE`` 覆盖，便于运维一键切换。
DB_WRITE_MODE = 'dual'
