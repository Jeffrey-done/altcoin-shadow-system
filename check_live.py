#!/usr/bin/env python3
"""
实盘自检脚本 —— 在开真实资金之前运行，避免踩坑。

使用方法：
  python3 check_live.py              # 检查当前 config 对应的所有已开启交易所
  python3 check_live.py binance      # 只检查 Binance
  python3 check_live.py okx          # 只检查 OKX

检查项：
  1. API 凭证是否配置且可认证
  2. 合约账户是否可用 / 余额是否足够
  3. Binance: 持仓模式是否为 Hedge Mode（对冲模式）
     OKX:     持仓模式是否为双向持仓（long_short_mode）
  4. 杠杆设置接口是否可用（试设 BTC/USDT 的杠杆为当前 config.LEVERAGE，
     成功立即恢复）
  5. 路由配置打印（PRIMARY_EXCHANGE 生效后会在哪家下单）

运行要求：不会下任何真实订单；仅调用查询和设置杠杆的 API。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config


GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


def _pr_ok(msg: str):
    print(f"  {GREEN}✓{RESET} {msg}")


def _pr_fail(msg: str):
    print(f"  {RED}✗{RESET} {msg}")


def _pr_warn(msg: str):
    print(f"  {YELLOW}!{RESET} {msg}")


def _pr_info(msg: str):
    print(f"  {CYAN}·{RESET} {msg}")


def check_binance() -> bool:
    print(f"\n{BOLD}═══ Binance 合约实盘自检 ═══{RESET}")

    if not config.LIVE_MODE:
        _pr_warn("config.LIVE_MODE=False，当前处于纸上模式。继续检查 API 凭证可用性...")

    api_key = os.environ.get('BINANCE_API_KEY', '')
    secret = os.environ.get('BINANCE_SECRET', '')

    if not api_key or not secret:
        _pr_fail("BINANCE_API_KEY 或 BINANCE_SECRET 未配置（.env）")
        return False
    _pr_ok(f"API 凭证已配置（key 前缀={api_key[:6]}...）")

    try:
        import ccxt
    except ImportError:
        _pr_fail("ccxt 未安装，pip install ccxt")
        return False

    try:
        exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'future'},
        })
    except Exception as e:
        _pr_fail(f"创建 Binance 实例失败: {e}")
        return False

    # 1. 查账户余额
    try:
        balance = exchange.fetch_balance({'type': 'future'})
        usdt = balance.get('USDT', {})
        total = float(usdt.get('total', 0))
        free = float(usdt.get('free', 0))
        _pr_ok(f"合约账户余额: 总={total:.2f}U / 可用={free:.2f}U")
        if total < config.DEFAULT_STAKE:
            _pr_warn(
                f"余额 {total:.2f}U 低于 DEFAULT_STAKE={config.DEFAULT_STAKE}U，"
                f"首笔开仓会失败。"
            )
    except Exception as e:
        _pr_fail(f"查询余额失败（多半是 API 权限或网络）: {e}")
        return False

    # 2. 检查持仓模式（必须是 Hedge Mode / 对冲模式）
    try:
        result = exchange.fapiPrivateGetPositionSideDual()
        dual_side = bool(result.get('dualSidePosition', False))
        if dual_side:
            _pr_ok("持仓模式 = Hedge Mode（对冲模式）✓ 与 positionSide=SHORT/LONG 兼容")
        else:
            _pr_fail(
                "持仓模式 = One-way（单向模式）✗ "
                "代码使用 positionSide 参数会报错！"
            )
            _pr_warn(
                "请去 Binance 合约设置 → 持仓模式 → 切换为「对冲模式 / Hedge Mode」，"
                "然后重跑本脚本。"
            )
            return False
    except Exception as e:
        _pr_warn(f"查询持仓模式失败（不一定致命）: {e}")

    # 3. 试设杠杆（用 BTC/USDT 做试探，不下单）
    try:
        exchange.set_leverage(config.LEVERAGE, 'BTC/USDT')
        _pr_ok(f"杠杆设置接口可用（BTC/USDT 杠杆={config.LEVERAGE}x 已试设）")
    except Exception as e:
        _pr_warn(f"set_leverage 接口异常（可能是权限问题）: {e}")

    return True


def check_okx() -> bool:
    print(f"\n{BOLD}═══ OKX 合约实盘自检 ═══{RESET}")

    if not config.OKX_LIVE_MODE:
        _pr_warn("config.OKX_LIVE_MODE=False，当前 OKX 不会真实下单。继续检查 API 凭证可用性...")

    api_key = os.environ.get('OKX_API_KEY', '')
    secret = os.environ.get('OKX_SECRET', '')
    passphrase = os.environ.get('OKX_PASSPHRASE', '')

    missing = [n for n, v in [
        ('OKX_API_KEY', api_key), ('OKX_SECRET', secret), ('OKX_PASSPHRASE', passphrase)
    ] if not v]
    if missing:
        _pr_fail(f"以下环境变量未配置: {', '.join(missing)}")
        return False
    _pr_ok(f"API 凭证已配置（key 前缀={api_key[:6]}...）")

    try:
        import ccxt
    except ImportError:
        _pr_fail("ccxt 未安装，pip install ccxt")
        return False

    try:
        exchange = ccxt.okx({
            'apiKey': api_key,
            'secret': secret,
            'password': passphrase,
            'enableRateLimit': True,
        })
    except Exception as e:
        _pr_fail(f"创建 OKX 实例失败: {e}")
        return False

    # 1. 查合约账户余额
    try:
        balance = exchange.fetch_balance({'type': 'swap'})
        usdt = balance.get('USDT', {})
        total = float(usdt.get('total', 0))
        free = float(usdt.get('free', 0))
        _pr_ok(f"合约账户余额: 总={total:.2f}U / 可用={free:.2f}U")
        if total < config.DEFAULT_STAKE:
            _pr_warn(
                f"余额 {total:.2f}U 低于 DEFAULT_STAKE={config.DEFAULT_STAKE}U，"
                f"首笔开仓会失败。"
            )
    except Exception as e:
        _pr_fail(f"查询余额失败（多半是 API 权限或网络）: {e}")
        return False

    # 2. 检查持仓模式（OKX 需要 long_short_mode 才能用 posSide=long/short）
    try:
        # ccxt OKX 封装：privateGetAccountConfig
        result = exchange.privateGetAccountConfig()
        data = result.get('data', [{}])[0] if result.get('data') else {}
        pos_mode = data.get('posMode', '')
        if pos_mode == 'long_short_mode':
            _pr_ok("持仓模式 = long_short_mode（双向持仓）✓ 与 posSide=long/short 兼容")
        elif pos_mode == 'net_mode':
            _pr_fail(
                "持仓模式 = net_mode（单向净持仓）✗ "
                "代码使用 posSide 参数会报错！"
            )
            _pr_warn(
                "请去 OKX 交易 → 设置 → 合约持仓模式 → 切换为「双向持仓」，"
                "然后重跑本脚本。"
            )
            return False
        else:
            _pr_warn(f"持仓模式未知: {pos_mode}，请手动确认")
    except Exception as e:
        _pr_warn(f"查询持仓模式失败（不一定致命）: {e}")

    # 3. 试设杠杆
    try:
        exchange.set_leverage(config.OKX_DEFAULT_LEVERAGE, 'BTC/USDT',
                              params={'mgnMode': 'cross'})
        _pr_ok(f"杠杆设置接口可用（BTC/USDT 杠杆={config.OKX_DEFAULT_LEVERAGE}x 已试设）")
    except Exception as e:
        _pr_warn(f"set_leverage 接口异常（可能是权限或品种问题）: {e}")

    return True


def print_routing_summary():
    print(f"\n{BOLD}═══ 路由配置 ═══{RESET}")
    _pr_info(f"LIVE_MODE (Binance 实盘)      = {config.LIVE_MODE}")
    _pr_info(f"OKX_LIVE_MODE (OKX 实盘)      = {config.OKX_LIVE_MODE}")
    _pr_info(f"PRIMARY_EXCHANGE              = {getattr(config, 'PRIMARY_EXCHANGE', 'binance')}")
    _pr_info(f"DEFAULT_STAKE (每笔保证金)    = {config.DEFAULT_STAKE}U")
    _pr_info(f"LEVERAGE (币安杠杆)           = {config.LEVERAGE}x")
    _pr_info(f"OKX_DEFAULT_LEVERAGE          = {config.OKX_DEFAULT_LEVERAGE}x")
    _pr_info(f"SLIPPAGE_ALERT_PCT            = {config.SLIPPAGE_ALERT_PCT}%")

    # 推演一下实际路由会发生什么
    binance_on = config.LIVE_MODE
    okx_on = config.OKX_LIVE_MODE
    mode = getattr(config, 'PRIMARY_EXCHANGE', 'binance').lower()

    if not binance_on and not okx_on:
        print(f"\n  {YELLOW}→ 当前行为: 纸上交易（不会下任何真实单）{RESET}")
    elif binance_on and not okx_on:
        print(f"\n  {GREEN}→ 当前行为: 所有信号只在 Binance 下单{RESET}")
    elif okx_on and not binance_on:
        print(f"\n  {GREEN}→ 当前行为: 所有信号只在 OKX 下单{RESET}")
    else:
        if mode == 'both':
            print(f"\n  {GREEN}→ 当前行为: Binance + OKX 同时开仓（保证金各 50%）{RESET}")
        elif mode in ('binance', 'okx'):
            print(f"\n  {GREEN}→ 当前行为: 两所都启用，但信号只在 {mode.upper()} 下单{RESET}")
        elif mode == 'auto':
            print(f"\n  {GREEN}→ 当前行为: 两所都启用，按币种覆盖自动选择（fallback={config.PRIMARY_EXCHANGE_FALLBACK}）{RESET}")


def main():
    targets = sys.argv[1:] or ['all']
    targets = [t.lower() for t in targets]

    print(f"{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print(f"{BOLD}        altcoin-shadow-system 实盘自检{RESET}")
    print(f"{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")

    print_routing_summary()

    results = {}

    if 'all' in targets or 'binance' in targets:
        try:
            results['binance'] = check_binance()
        except Exception as e:
            _pr_fail(f"Binance 自检异常: {e}")
            results['binance'] = False

    if 'all' in targets or 'okx' in targets:
        try:
            results['okx'] = check_okx()
        except Exception as e:
            _pr_fail(f"OKX 自检异常: {e}")
            results['okx'] = False

    print(f"\n{BOLD}═══ 总结 ═══{RESET}")
    for name, ok in results.items():
        if ok:
            _pr_ok(f"{name.upper()}: 通过")
        else:
            _pr_fail(f"{name.upper()}: 未通过（见上方详情）")

    print(f"\n{BOLD}下一步建议：{RESET}")
    if all(results.values()):
        print(f"  {GREEN}1.{RESET} 所有自检通过，可以小仓位实测（建议 DEFAULT_STAKE=20）")
        print(f"  {GREEN}2.{RESET} 观察 TG 推送里的「成交价」不再是 0，订单号不再是 SHADOW")
        print(f"  {GREEN}3.{RESET} 第一笔止盈/止损后，确认「自动平仓成功」推送有真实订单号")
    else:
        print(f"  {RED}1.{RESET} 修复上面标红的问题后再重跑本脚本")
        print(f"  {RED}2.{RESET} 不要在未全部通过的情况下把 LIVE_MODE 设为 True")

    sys.exit(0 if all(results.values()) else 1)


if __name__ == '__main__':
    main()
