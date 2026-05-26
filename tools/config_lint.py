#!/usr/bin/env python3
"""
配置一致性检查工具

扫描所有账户的 runtime_config.json 覆盖值，与代码默认值 / risk.yaml 对比，
暴露配置漂移问题。

用法:
  python -m tools.config_lint
  python tools/config_lint.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    import config
    from runtime_config import (
        load_all_account_overrides, get_pristine_default, ALLOWED,
        RUNTIME_CONFIG_FILE,
    )

    print("=" * 60)
    print("  配置一致性检查 (config_lint)")
    print("=" * 60)

    # 1. 检查 runtime_config.json 是否存在
    if not os.path.exists(RUNTIME_CONFIG_FILE):
        print("\n⚠️  runtime_config.json 不存在（首次部署或未使用 admin panel）")
        print("   所有参数将使用代码默认值。")
        return

    # 2. 加载所有账户覆盖
    all_overrides = load_all_account_overrides()
    if not all_overrides:
        print("\n✅ 无账户级覆盖，所有账户使用统一默认值。")
        return

    print(f"\n📋 发现 {len(all_overrides)} 个账户有覆盖配置\n")

    # 3. 重点检查字段
    RISK_FIELDS = [
        'RISK_MAX_DAILY_TRADES',
        'RISK_MAX_DAILY_TRADES_LONG',
        'RISK_MAX_DAILY_TRADES_SHORT',
        'RISK_MAX_DAILY_LOSS',
        'RISK_CONSECUTIVE_LOSS_PAUSE',
        'RISK_MAX_POSITION_PCT',
        'DEFAULT_STAKE',
        'ACCOUNT_BALANCE',
        'LEVERAGE',
    ]

    issues = []

    for acc_id, overrides in all_overrides.items():
        print(f"  账户: {acc_id}")
        for field in RISK_FIELDS:
            pristine = get_pristine_default(field)
            override_val = overrides.get(field)
            if override_val is not None and override_val != pristine:
                print(f"    {field}: {pristine} → {override_val} (已覆盖)")
            elif override_val is None:
                # 没有覆盖 → 使用 PRISTINE 默认值
                pass

        # 检查一致性问题
        acc_stake = overrides.get('DEFAULT_STAKE', get_pristine_default('DEFAULT_STAKE'))
        acc_balance = overrides.get('ACCOUNT_BALANCE', get_pristine_default('ACCOUNT_BALANCE'))
        acc_pos_pct = overrides.get('RISK_MAX_POSITION_PCT',
                                    get_pristine_default('RISK_MAX_POSITION_PCT'))
        acc_max_trades = overrides.get('RISK_MAX_DAILY_TRADES',
                                       get_pristine_default('RISK_MAX_DAILY_TRADES'))

        if acc_stake and acc_balance and acc_pos_pct:
            try:
                max_pos = float(acc_balance) * float(acc_pos_pct)
                if float(acc_stake) > max_pos:
                    msg = (f"    ❌ {acc_id}: DEFAULT_STAKE({acc_stake}) > "
                           f"max_position({max_pos:.0f}U = {acc_balance}×{acc_pos_pct}) "
                           f"→ 风控会永远拒绝开仓!")
                    print(msg)
                    issues.append(msg)
            except (TypeError, ValueError):
                pass

        # 方向分桶检查
        max_long = overrides.get('RISK_MAX_DAILY_TRADES_LONG', 0)
        max_short = overrides.get('RISK_MAX_DAILY_TRADES_SHORT', 0)
        if acc_max_trades and max_long and max_short:
            if int(max_long) + int(max_short) > int(acc_max_trades):
                msg = (f"    ⚠️ {acc_id}: LONG({max_long}) + SHORT({max_short}) > "
                       f"总上限({acc_max_trades})，方向子限额永远不会同时打满")
                print(msg)

        print()

    # 4. 跨账户一致性
    print("─" * 40)
    print("跨账户对比:")
    for field in ['RISK_MAX_DAILY_TRADES', 'DEFAULT_STAKE', 'ACCOUNT_BALANCE']:
        values = {}
        pristine = get_pristine_default(field)
        for acc_id, overrides in all_overrides.items():
            val = overrides.get(field, f"(默认={pristine})")
            values[acc_id] = val
        if len(set(str(v) for v in values.values())) > 1:
            print(f"  ⚠️ {field} 各账户不一致:")
            for acc_id, val in values.items():
                print(f"      {acc_id}: {val}")
        else:
            print(f"  ✅ {field}: 所有账户一致")

    print()
    if issues:
        print(f"🚨 发现 {len(issues)} 个严重问题，请立即修复！")
        return 1
    else:
        print("✅ 未发现严重配置问题。")
        return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
