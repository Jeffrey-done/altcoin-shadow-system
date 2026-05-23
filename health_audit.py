#!/usr/bin/env python3
"""
Runtime health audit for live trading consistency.

Checks:
1) Docker services up
2) Local open live trades vs exchange positions
3) Local protection fields vs exchange algo orders
4) Prints PASS/WARN/FAIL summary with actionable notes
5) Detects local<->exchange drift on protect_* fields
"""

import json
import subprocess
import argparse
from dataclasses import dataclass
from typing import Dict, List

from common import TRADES_FILE, load_json
from live_executor import get_live_exchange, get_binance_open_algo_orders
from common import send_tg

STACK_NAMES = [
    'altcoin-shadow-system-dashboard-1',
    'altcoin-shadow-system-scheduler-1',
    'altcoin-shadow-system-realtime-monitor-1',
]


@dataclass
class CheckResult:
    level: str  # PASS/WARN/FAIL
    name: str
    detail: str


def _run(cmd: str) -> str:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (p.stdout or '') + (p.stderr or '')


def check_services(mode: str = 'auto') -> CheckResult:
    """
    mode:
      - auto: 容器内无 docker cli 时跳过；有 docker 时严格检查
      - host: 强制严格检查（无 docker 视为 FAIL）
      - skip: 跳过服务检查
    """
    mode = (mode or 'auto').strip().lower()
    if mode == 'skip':
        return CheckResult('WARN', 'services', 'service check skipped by config')

    out = _run("docker ps --format '{{.Names}}\t{{.Status}}'")
    low = out.lower()
    no_docker = ('docker: not found' in low or 'not recognized as an internal or external command' in low)
    if no_docker:
        if mode == 'auto':
            return CheckResult('WARN', 'services', 'docker cli unavailable in runtime, skip container service check')
        return CheckResult('FAIL', 'services', 'docker cli unavailable but services-check=host required')

    missing = []
    bad = []
    lines = [x.strip() for x in out.splitlines() if x.strip()]
    m = {}
    for ln in lines:
        parts = ln.split('\t')
        if len(parts) >= 2:
            m[parts[0]] = parts[1]
    for n in STACK_NAMES:
        if n not in m:
            missing.append(n)
        elif not m[n].lower().startswith('up'):
            bad.append(f"{n}: {m[n]}")
    if missing or bad:
        return CheckResult('FAIL', 'services', f"missing={missing} bad={bad}")
    return CheckResult('PASS', 'services', 'all core services are up')




def _is_account_live_expected(account_id: str) -> bool:
    """判断该账号当前是否处于"预期实盘"状态。"""
    try:
        from admin_secrets import list_accounts
        acc = next((a for a in (list_accounts() or []) if a.get('id') == account_id), None)
        if not acc:
            return False
        trading_enabled = bool(acc.get('trading_enabled', True))
        has_binance = bool(acc.get('has_binance', False))
        # 只针对当前审计里的 binance 交易检查
        return trading_enabled and has_binance
    except Exception:
        return False

def _to_fapi_symbol(ccxt_symbol: str) -> str:
    return ccxt_symbol.replace('/USDT', 'USDT').replace('/', '')


def check_live_positions_and_algos(account_id: str) -> List[CheckResult]:
    rs: List[CheckResult] = []
    trades = load_json(TRADES_FILE, [])
    live_open = [
        t for t in trades
        if t.get('status') == 'open' and t.get('exchange') == 'binance' and t.get('account_id') == account_id
    ]
    if not live_open:
        if _is_account_live_expected(account_id):
            rs.append(CheckResult('WARN', 'live_open_trades', f'no open binance trades for {account_id}'))
        else:
            rs.append(CheckResult('PASS', 'live_open_trades', f'account {account_id} not in expected binance live mode'))
        return rs

    ex = get_live_exchange(account_id)
    if not ex:
        rs.append(CheckResult('WARN', 'exchange', f'exchange client unavailable [account_id={account_id}], skip live checks'))
        ex_cache[account_id] = None
        return rs
    if not ex:
        rs.append(CheckResult('WARN', 'exchange', 'exchange client unavailable, skip live exchange checks for this account'))
        return rs
    ex.load_markets()
    algo_by_symbol: Dict[str, List[dict]] = {}
    for t in live_open:
        sym = t['symbol']
        try:
            algo_all = get_binance_open_algo_orders(sym, account_id=account_id) or []
        except Exception:
            algo_all = []
        fapi = _to_fapi_symbol(sym)
        algo_by_symbol[fapi] = list(algo_all)

    for t in live_open:
        sym = t['symbol']
        fapi = _to_fapi_symbol(sym)
        # position
        pos_amt = 0.0
        try:
            poss = ex.fetch_positions([f"{sym}:USDT"]) if not sym.endswith(':USDT') else ex.fetch_positions([sym])
            for p in poss:
                if str(p.get('side', '')).lower() == 'short':
                    pos_amt = float(p.get('contracts') or 0)
                    break
        except Exception as e:
            rs.append(CheckResult('FAIL', f'position:{sym}', f'fetch_positions error: {e}'))
            continue

        shares = float(t.get('shares') or 0)
        if pos_amt <= 0:
            rs.append(CheckResult('FAIL', f'position:{sym}', f'local open but exchange short contracts=0 (local shares={shares})'))
        elif abs(pos_amt - shares) / max(1.0, shares) > 0.35:
            rs.append(CheckResult('WARN', f'position:{sym}', f'position mismatch local={shares} exchange={pos_amt}'))
        else:
            rs.append(CheckResult('PASS', f'position:{sym}', f'local≈exchange ({shares} vs {pos_amt})'))

        # algo consistency + drift audit
        algos = algo_by_symbol.get(fapi, [])
        stop_id = str(t.get('protect_stop_algo_id') or '')
        tp_id = str(t.get('protect_tp_algo_id') or '')
        stage = t.get('protect_stage') or ''

        if not algos:
            if stop_id or tp_id or stage:
                rs.append(CheckResult('WARN', f'algo:{sym}', 'exchange algo empty but local protect_* still set (drift)'))
            else:
                rs.append(CheckResult('WARN', f'algo:{sym}', 'no open algo orders on exchange'))
            continue

        algo_ids = {str(a.get('algoId')) for a in algos}
        drift = False
        if stop_id and stop_id not in algo_ids:
            drift = True
            rs.append(CheckResult('WARN', f'algo:{sym}', f'local stop id {stop_id} not in exchange open algos'))
        if tp_id and tp_id not in algo_ids:
            drift = True
            rs.append(CheckResult('WARN', f'algo:{sym}', f'local tp id {tp_id} not in exchange open algos'))

        kinds = {a.get('orderType') for a in algos}
        if stage == 'stage1' and not ({'STOP_MARKET', 'TAKE_PROFIT_MARKET'} <= kinds):
            drift = True
            rs.append(CheckResult('WARN', f'algo:{sym}', f'stage1 expected STOP+TP, got {sorted(kinds)}'))
        elif stage == 'stage2' and not ({'STOP_MARKET', 'TAKE_PROFIT_MARKET'} <= kinds):
            drift = True
            rs.append(CheckResult('WARN', f'algo:{sym}', f'stage2 expected STOP+TP2, got {sorted(kinds)}'))

        if not drift:
            rs.append(CheckResult('PASS', f'algo:{sym}', f'stage={stage or "(none)"}, open_algo={len(algos)}, ids_consistent'))

    return rs


def summarize(results: List[CheckResult], send_tg_alert: bool = False) -> int:
    rank = {'PASS': 0, 'WARN': 1, 'FAIL': 2}
    code = 0
    for r in results:
        print(f"[{r.level}] {r.name}: {r.detail}")
        code = max(code, rank.get(r.level, 2))
    print('---')
    if code == 0:
        print('OVERALL: PASS')
    elif code == 1:
        print('OVERALL: WARN')
    else:
        print('OVERALL: FAIL')

    if send_tg_alert and code > 0:
        try:
            lines = [f"[{r.level}] {r.name}: {r.detail}" for r in results if r.level != 'PASS']
            if not lines:
                lines = [f"audit finished with level={code}"]
            send_tg("🩺 <b>多账号健康审计告警</b>\n\n" + "\n".join(lines[:20]))
        except Exception:
            pass
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="Runtime health audit")
    parser.add_argument("--account", default=None, help="account id for live checks")
    parser.add_argument("--all", action="store_true", help="check all trading accounts")
    parser.add_argument("--tg", action="store_true", help="send TG when overall WARN/FAIL")
    parser.add_argument("--services-check", default="auto", choices=["auto", "host", "skip"],
                        help="service check mode: auto|host|skip")
    args = parser.parse_args()
    account_id = args.account
    if not account_id:
        try:
            from admin_secrets import get_active_account_id
            account_id = get_active_account_id()
        except Exception:
            account_id = 'acc_shadow_system'
    results: List[CheckResult] = []
    results.append(check_services(args.services_check))

    if args.all:
        try:
            from admin_secrets import get_all_trading_accounts
            accts = get_all_trading_accounts() or []
            ids = [a.get('id') for a in accts if a.get('id')]

            if not ids:
                results.append(CheckResult('WARN', 'accounts', 'no trading accounts found in admin_secrets, skip per-account live checks'))
            else:
                for aid in ids:
                    results.extend(check_live_positions_and_algos(aid))
        except Exception as e:
            results.append(CheckResult('FAIL', 'accounts', f'failed to load accounts: {e}'))
    else:
        results.extend(check_live_positions_and_algos(account_id))

    return summarize(results, send_tg_alert=args.tg)


if __name__ == '__main__':
    raise SystemExit(main())
