#!/usr/bin/env python3
"""
面板不显示数据 — 一键诊断脚本

把数据从"磁盘 -> get_dashboard_data() -> /api/data"这条链路上每一环都打印出来，
直接定位是哪一层断了。

用法：
    cd <项目根>
    python3 diagnose_dashboard.py            # 只看本地数据 + get_dashboard_data 输出
    python3 diagnose_dashboard.py --http     # 额外探测正在跑的 dashboard HTTP
"""

import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def section(title):
    print("\n" + "═" * 64)
    print(f"  {title}")
    print("═" * 64)


def kv(label, value):
    print(f"  {label:<28} : {value}")


def diag_files():
    section("[1] 数据文件落盘检查（dashboard 读这些 JSON）")
    from common import (
        TRADES_FILE, CANDIDATES_FILE, RISK_FILE,
        WEEKLY_REPORT_FILE, TRADES_INFLIGHT_FILE,
        SCRIPT_DIR,
    )
    kv("项目目录 SCRIPT_DIR", SCRIPT_DIR)
    files = {
        "altcoin_shadow_trades.json": TRADES_FILE,
        "altcoin_candidates.json": CANDIDATES_FILE,
        "risk_state.json": RISK_FILE,
        "weekly_report.json": WEEKLY_REPORT_FILE,
        "altcoin_trades_inflight.json": TRADES_INFLIGHT_FILE,
    }
    issues = []
    for name, path in files.items():
        if os.path.exists(path):
            size = os.path.getsize(path)
            try:
                with open(path) as f:
                    data = json.load(f)
                if isinstance(data, list):
                    summary = f"OK  {size}B  list len={len(data)}"
                elif isinstance(data, dict):
                    summary = f"OK  {size}B  dict keys={list(data.keys())[:6]}"
                else:
                    summary = f"OK  {size}B  type={type(data).__name__}"
                if size == 0 or (isinstance(data, list) and not data and name != 'altcoin_trades_inflight.json'):
                    issues.append(f"{name} 为空")
            except Exception as e:
                summary = f"❌ JSON 损坏: {e}"
                issues.append(f"{name} JSON 解析失败")
        else:
            summary = "❌ 不存在"
            if name in ("altcoin_shadow_trades.json", "risk_state.json"):
                issues.append(f"{name} 缺失（scheduler 没在跑或没写过）")
        kv(name, summary)

    return issues


def diag_account():
    section("[2] 当前活跃账户 & 数据归属")
    try:
        from admin_secrets import get_active_account_id, list_accounts, SHADOW_ACCOUNT_ID
        active = get_active_account_id() or '(空 — 单账户兼容模式)'
        kv("active_account_id", active)
        kv("SHADOW_ACCOUNT_ID", SHADOW_ACCOUNT_ID)
        accs = list_accounts()
        kv("已配置账户数", len(accs))
        for a in accs:
            kv(f"  {a['id']}",
               f"name={a['name']} bn={a.get('has_binance')} okx={a.get('has_okx')} trading={a.get('trading_enabled', True)}")
    except Exception as e:
        kv("admin_secrets 读取失败", e)
        active = ''

    # 看 trades.json 里每条记录的 account_id 分布
    try:
        from common import load_json, TRADES_FILE
        trades = load_json(TRADES_FILE, [])
        kv("trades.json 总条数", len(trades))
        from collections import Counter
        acct = Counter(t.get('account_id', '<empty>') for t in trades)
        for a_id, n in acct.most_common():
            kv(f"  account_id={a_id}", f"{n} 笔")
        # 关键检查：active 账户能看到几笔
        from common import filter_trades_by_account, get_current_account_id
        cur = get_current_account_id()
        visible = filter_trades_by_account(trades, cur)
        kv("当前活跃账户能看到的交易", f"{len(visible)} / {len(trades)}")
        if trades and not visible:
            print("\n  ⚠️  全部交易都被账户过滤掉了！这就是面板空白的根因。")
            print("     原因：trades.json 里的 account_id 与活跃账户不匹配。")
    except Exception as e:
        kv("交易归属分析失败", e)


def diag_dashboard_data():
    section("[3] get_dashboard_data() 实际返回（dashboard 渲染用的数据）")
    try:
        from dashboard import get_dashboard_data
        data = get_dashboard_data()
        a = data['account']
        p = data.get('pool', {})
        kv("account.balance",     a.get('balance'))
        kv("account.today_pnl",   a.get('today_pnl'))
        kv("account.total_pnl",   a.get('total_pnl'))
        kv("account.total_trades",a.get('total_trades'))
        kv("account.win_rate",    a.get('win_rate'))
        kv("short_trades.open",   len(data['short_trades']['open']))
        kv("short_trades.closed", len(data['short_trades']['closed']))
        kv("candidates",          len(data['candidates']))
        kv("risk.daily_loss",     data['risk'].get('daily_loss', 0))
        kv("risk.paused_until",   data['risk'].get('paused_until'))
        kv("pool.max_position",   p.get('max_position'))
        kv("pool.compound_stake", p.get('compound_stake'))
        kv("account_id (返回值)", data.get('account_id'))
        kv("timestamp",           data['timestamp'])

        empty = (a.get('total_trades', 0) == 0
                 and not data['short_trades']['open']
                 and not data['short_trades']['closed']
                 and not data['candidates'])
        if empty:
            print("\n  ⚠️  get_dashboard_data() 返回的所有列表都是空 —— 前端展示也必然空白。")
            print("     不是前端 bug，是数据源（trades.json / candidates.json）真的没东西，")
            print("     或者被账户过滤完全过滤掉了。")
    except Exception as e:
        import traceback
        kv("get_dashboard_data() 异常", e)
        traceback.print_exc()


def diag_http(port):
    section(f"[4] HTTP 探测 — 直接打 http://127.0.0.1:{port}/api/data")
    try:
        import urllib.request, urllib.error
        token = os.environ.get('DASHBOARD_TOKEN', '')
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/data")
        if token:
            req.add_header('X-Dashboard-Token', token)
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            kv("HTTP 状态", resp.status)
            kv("Content-Length", len(body))
            try:
                d = json.loads(body)
                kv("account.balance",     d['account']['balance'])
                kv("account.total_trades",d['account']['total_trades'])
                kv("short_trades.open",   len(d['short_trades']['open']))
                kv("candidates",          len(d['candidates']))
            except Exception as je:
                kv("JSON parse", f"❌ {je}")
                print(body[:400])
    except urllib.error.HTTPError as e:
        kv("HTTP 错误", f"{e.code} {e.reason}")
        if e.code == 401:
            print("\n  ⚠️  401 = DASHBOARD_TOKEN 已设置，前端没带 X-Dashboard-Token 头。")
            print("     要么给前端加 token，要么 unset DASHBOARD_TOKEN 后重启 dashboard。")
        try:
            print(e.read().decode()[:300])
        except Exception:
            pass
    except Exception as e:
        kv("连接失败", e)
        print("\n  ⚠️  Dashboard 进程没在跑，或没监听本端口。")
        print("     检查：ps -ef | grep dashboard.py")


def diag_processes():
    section("[5] 后台进程检查（scheduler 是数据生产者）")
    import subprocess
    try:
        out = subprocess.check_output(
            ['pgrep', '-af', 'scheduler.py|dashboard.py|realtime_monitor'],
            text=True, stderr=subprocess.DEVNULL,
        )
        if out.strip():
            for line in out.strip().split('\n'):
                kv("running", line)
        else:
            print("  ❌ 没有 scheduler.py / dashboard.py 在跑 —— 这就是没数据的根本原因。")
    except subprocess.CalledProcessError:
        print("  ❌ pgrep 没找到任何相关进程。scheduler 没在跑 → trades.json 永远不会更新。")


def diag_runtime_overrides():
    section("[6] runtime_overrides 检查（admin panel 改的配置可能覆盖默认值）")
    try:
        from runtime_config import apply_overrides
        apply_overrides()
        import config
        kv("LIVE_MODE",         getattr(config, 'LIVE_MODE'))
        kv("OKX_LIVE_MODE",     getattr(config, 'OKX_LIVE_MODE'))
        kv("DAILY_RSI_MIN",     getattr(config, 'DAILY_RSI_MIN'))
        kv("RISK_MAX_DAILY_LOSS", getattr(config, 'RISK_MAX_DAILY_LOSS'))
        kv("ACCOUNT_BALANCE",   getattr(config, 'ACCOUNT_BALANCE'))
    except Exception as e:
        kv("apply_overrides 失败", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--http', action='store_true')
    ap.add_argument('--port', type=int, default=8080)
    args = ap.parse_args()

    print("\n面板诊断 — 影子做空系统 v5.0\n")
    issues = diag_files()
    diag_account()
    diag_dashboard_data()
    diag_processes()
    diag_runtime_overrides()
    if args.http:
        diag_http(args.port)

    section("结论速览")
    if issues:
        print("  以下数据文件层面有问题：")
        for i in issues:
            print(f"   - {i}")
    else:
        print("  数据文件层面 OK。如果面板仍空白，重点看 [2] 账户过滤和 [4] HTTP 401。")
    print()


if __name__ == '__main__':
    main()
