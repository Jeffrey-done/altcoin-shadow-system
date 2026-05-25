#!/usr/bin/env python3
"""Pre-Pump Sniffer Backtest - 妖币起飞前嗅探器"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from backtesting.metrics import calculate_metrics
from backtesting.slippage import SlippageModel, SlippageConfig, SlippageModelType

print("═"*60)
print("  🔮 Pre-Pump Sniffer 回测 (妖币起飞前嗅探器)")
print("═"*60)

np.random.seed(2024)
n_bars = 90 * 24

# 模拟含"横盘吸筹→起飞"周期的小币种
price = 0.05
prices = [price]
volumes = []
oi_values = [1000000.0]
funding_rates = [0.01]

for i in range(n_bars - 1):
    hourly_vol = 0.005
    drift = 0.0
    vol_mult = 1.0
    oi_change = np.random.uniform(-0.002, 0.002)
    fr_drift = 0.0
    cycle = i % (24 * 10)
    if cycle < 24*6:
        drift = np.random.uniform(-0.0003, 0.0003)
        vol_mult = np.random.uniform(0.4, 1.0)
    elif 24*6 <= cycle < 24*7:
        drift = np.random.uniform(-0.001, 0.001)
        vol_mult = np.random.uniform(2.5, 5.0)
        oi_change = np.random.uniform(0.02, 0.05)
        fr_drift = 0.005
    elif 24*7 <= cycle < 24*8:
        drift = np.random.uniform(0.005, 0.015)
        hourly_vol = 0.02
        vol_mult = np.random.uniform(3.0, 8.0)
        oi_change = np.random.uniform(0.01, 0.03)
    elif 24*8 <= cycle < 24*9:
        drift = np.random.uniform(-0.008, -0.002)
        hourly_vol = 0.015
        vol_mult = np.random.uniform(2.0, 4.0)
        oi_change = np.random.uniform(-0.03, -0.01)
    else:
        drift = np.random.uniform(-0.002, 0.001)
        vol_mult = np.random.uniform(0.5, 1.2)
    ret = drift + hourly_vol * np.random.randn()
    price = max(0.001, prices[-1] * (1 + ret))
    prices.append(price)
    volumes.append(600000 * vol_mult)
    oi_values.append(max(100000, oi_values[-1] * (1 + oi_change)))
    funding_rates.append(np.clip(funding_rates[-1] + fr_drift + np.random.uniform(-0.002, 0.002), -0.1, 0.2))

volumes.append(volumes[-1])
highs = [p * np.random.uniform(1.002, 1.02) for p in prices]
lows = [p * np.random.uniform(0.98, 0.998) for p in prices]
closes = np.array(prices)
vol_arr = np.array(volumes)
oi_arr = np.array(oi_values)
fr_arr = np.array(funding_rates)

print(f"  数据: {n_bars} bars | 价格 {min(prices):.5f}~{max(prices):.5f}")
print(f"  日波动: {pd.Series(prices).pct_change().std()*np.sqrt(24)*100:.1f}%")
print(f"  周期: 10天(横盘6→吸筹1→起飞1→回落1→平静1)")
print()

slippage = SlippageModel(SlippageConfig(model_type=SlippageModelType.VOLUME_BASED, base_bps=3.0, volume_impact_coeff=50.0))

P = dict(vol_spike=2.5, price_calm=2.0, oi_spike=3.0, fr_shift=0.02, bb_sq=50,
         bk_lookback=12, bk_vol=1.5, stake=30, lev=5,
         tp1=10.0, tp2=25.0, tp1_r=0.5, sl=4.0, trail_act=10.0, trail_ret=0.35, max_hold=24,
         threshold=4)
fee = 0.04

# 衍生指标
vol_24h = pd.Series(vol_arr).rolling(24, min_periods=6).mean().values
vol_7d = pd.Series(vol_arr).rolling(24*7, min_periods=24).mean().values
hi_12h = pd.Series(closes).rolling(12, min_periods=6).max().values
bb_w = pd.Series(closes).rolling(20, min_periods=10).std().values / closes * 100
bb_pct = pd.Series(bb_w).rolling(24*7, min_periods=24).apply(lambda x: (x.rank(pct=True).iloc[-1])*100, raw=False).values
oi_4h = np.zeros(n_bars)
for idx in range(4, n_bars):
    if oi_arr[idx-4] > 0: oi_4h[idx] = (oi_arr[idx]-oi_arr[idx-4])/oi_arr[idx-4]*100
fr_4h = np.zeros(n_bars)
for idx in range(4, n_bars): fr_4h[idx] = fr_arr[idx-4]

cap = 1000.0; eq = cap; trades = []; last_tb = -30; sigs = 0
i = 24*7

while i < n_bars - P['max_hold'] - 2:
    if i - last_tb < P['max_hold'] + 6: i+=1; continue

    score = 0; det = []
    vr = vol_arr[i]/vol_24h[i] if vol_24h[i]>0 else 0
    pc4 = abs(closes[i]-closes[max(0,i-4)])/closes[max(0,i-4)]*100 if i>=4 else 99

    if vr >= P['vol_spike'] and pc4 < P['price_calm']:
        score+=1; det.append(f"Vol×{vr:.1f}")
    if oi_4h[i] >= P['oi_spike'] and pc4 < P['price_calm']:
        score+=1; det.append(f"OI+{oi_4h[i]:.1f}%")
    if fr_arr[i] >= P['fr_shift'] and fr_4h[i] < P['fr_shift']*0.5:
        score+=1; det.append("FR↑")
    if oi_4h[i] > 2.0 and vr > 2.0:
        score+=1; det.append("Depth")
    if abs((closes[i]-closes[i-1])/closes[i-1]*100) < 0.3 and vr > 2.0:
        score+=1; det.append("Spread")
    if i>=3 and vol_arr[i]>vol_arr[i-1]>vol_arr[i-2]:
        pf = abs(closes[i]-closes[i-3])/closes[i-3]*100 < 1.5
        if pf and vr > 2.0: score+=1; det.append("SM↑")
    if not np.isnan(bb_pct[i]) and bb_pct[i] < P['bb_sq']:
        score+=1; det.append(f"Sq{bb_pct[i]:.0f}")

    if score < P['threshold']: i+=1; continue
    sigs += 1

    # 等突破确认
    eb = -1; rh = hi_12h[i]
    for j in range(i+1, min(i+12, n_bars)):
        if closes[j] > rh*1.005 and vol_arr[j] > vol_7d[j]*P['bk_vol']:
            eb=j; break
    if eb<0: i+=13; continue

    # 入场
    notl = P['stake']*P['lev']
    ep = slippage.apply(closes[eb], notl, vol_arr[eb], 'LONG')
    efee = notl*fee/100
    tp1p=ep*(1+P['tp1']/100); tp2p=ep*(1+P['tp2']/100); slp=ep*(1-P['sl']/100)
    bpnl=0; t1d=False; rem=1.0; t1pnl=0; xb=-1; xp=0; xr=''

    for b in range(eb+1, min(eb+P['max_hold']+1, n_bars)):
        h=highs[b]; lo=lows[b]; c=closes[b]
        pp=(c-ep)/ep*100; ph=(h-ep)/ep*100; bpnl=max(bpnl,ph)
        if lo<=slp: xb=b; xp=slp; xr='hard_stop'; break
        if not t1d and h>=tp1p:
            t1d=True; t1n=notl*P['tp1_r']; t1pnl=t1n*P['tp1']/100-t1n*fee/100; rem=0.5
        if h>=tp2p: xb=b; xp=tp2p; xr='tp2'; break
        if bpnl>=P['trail_act']:
            if pp<=bpnl*(1-P['trail_ret']): xb=b; xp=c; xr='trail'; break
    if xb<0: xb=min(eb+P['max_hold'],n_bars-1); xp=closes[xb]; xr='time'

    xps = slippage.apply_close(xp, notl*rem, vol_arr[min(xb,n_bars-1)], 'LONG')
    rpnl = notl*rem*((xps-ep)/ep*100)/100 - notl*rem*fee/100
    tpnl = t1pnl + rpnl - efee
    trades.append({'score':score,'det':' '.join(det),'pnl':round(tpnl,2),
        'pct':round(tpnl/P['stake']*100,1),'h':xb-eb,'r':xr,'t1':t1d})
    eq+=tpnl; last_tb=xb; i=xb+1
    if eq<cap*0.3: break

print(f"  信号检测: {sigs} | 确认入场: {len(trades)}")
print()
print("═"*60)
print("  📊 回测结果")
print("═"*60)
if trades:
    pnls=[t['pnl'] for t in trades]; hrs=[t['h'] for t in trades]
    m = calculate_metrics(pnls, cap, hrs, 90)
    reasons={}
    for t in trades: reasons[t['r']]=reasons.get(t['r'],0)+1
    t1h=sum(1 for t in trades if t['t1'])
    print(f"  总交易数:     {m.total_trades}")
    print(f"  胜率:         {m.win_rate:.1f}%")
    print(f"  盈亏比:       {m.payoff_ratio:.2f}")
    print(f"  总PnL:        {m.total_pnl:+.2f}U ({m.total_return_pct:+.1f}%)")
    print(f"  Sharpe Ratio: {m.sharpe_ratio:.2f}")
    print(f"  Sortino:      {m.sortino_ratio:.2f}")
    print(f"  Max Drawdown: {m.max_drawdown_pct:.1f}%")
    print(f"  Profit Factor:{m.profit_factor:.2f}")
    print(f"  平均持仓:     {m.avg_hold_hours:.1f}h")
    print(f"  连胜/连亏:    {m.max_consecutive_wins}/{m.max_consecutive_losses}")
    print(f"  评级:         {m.grade}")
    print()
    print(f"  退出原因:")
    for r,c in sorted(reasons.items(), key=lambda x:-x[1]):
        w=sum(1 for t in trades if t['r']==r and t['pnl']>0)
        print(f"    {r:12s}: {c:2d} ({c/len(trades)*100:4.1f}%) win={w/max(c,1)*100:.0f}%")
    print(f"  TP1触发率:    {t1h}/{len(trades)} ({t1h/len(trades)*100:.0f}%)")
    print()
    print(f"  逐笔交易:")
    print(f"  {'#':>2} {'Sc':>2} {'PnL':>7} {'Pct':>6} {'H':>3} {'Reason':>10} {'T1':>2} {'信号详情'}")
    for idx,t in enumerate(trades):
        print(f"  {idx+1:2d} {t['score']:2d} {t['pnl']:+7.2f} {t['pct']:+5.1f}% {t['h']:3d} {t['r']:>10} {'Y' if t['t1'] else ' ':>2} {t['det']}")
    print()
    print("═"*60)
    print(f"  💰 最终权益: {eq:.2f}U (初始 {cap:.0f}U)")
    print(f"  📈 收益率:   {(eq/cap-1)*100:+.1f}%")
    print(f"  📊 年化:     ~{(eq/cap-1)*100*365/90:+.0f}%")
    print("═"*60)
else:
    print("  ⚠️ 无交易")
