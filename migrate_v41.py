#!/usr/bin/env python3
"""
v4.0 → v4.1 数据迁移：修正 TP1 双计数历史记录
==============================================

为什么需要迁移
--------------
v4.0 的 altcoin_tracker.evaluate_trade() 在平仓分支 (hard_stop/tp2/trail/time)
写 `trade.pnl = tp1_locked_pnl + remaining_pnl`（即"合计总盈亏"）。
随后所有聚合模块（dashboard / tg_bot / health_check / weekly_report /
common.get_dynamic_balance / risk_control._calc_today_realized_loss）又做
`tp1_locked_pnl + trade.pnl`，于是 TP1 被算了两次。

v4.1 起，`trade.pnl` 的语义改为"仅剩余仓位的盈亏"（不含 tp1_locked_pnl），
聚合侧保持 `tp1_locked_pnl + pnl`——这样结果才正确。

但是 JSON 文件里已经有 v4.0 写入的历史 closed 记录，它们的 pnl 字段里
混合了 tp1_locked_pnl。新代码读取这些旧数据时会"看起来"盈亏翻倍。

迁移做的事
----------
对每条 `status='closed' 且 tp1_locked_pnl != 0` 的记录：
    trade.pnl  ←  trade.pnl - tp1_locked_pnl
并盖上 `_v41_migrated: true` 标记，保证幂等。

不动的数据
----------
- 开仓中 (status='open') 的记录：
    * tp1 未触发：`pnl` 原本就等于整笔仓位浮动盈亏（stake==stake_remaining），
      v4.1 下一次 evaluate_trade 会覆盖，无需动。
    * tp1 已触发但没平仓：v4.0 代码在 TP1 分支没改 `trade.pnl`（它保留了上一次
      evaluate 里的"剩余 50% 浮动盈亏"），语义已符合 v4.1，无需动。
- tp1_locked_pnl == 0 的 closed 记录：没触发 TP1，整笔是一次性平仓的，
  `pnl` 字段本就等于真实盈亏，无需动。
- 已打过 `_v41_migrated` 标记的：跳过，防止二次减除。

数据安全
--------
- 迁移前自动备份原文件到 `*.bak.v40.YYYYMMDDTHHMMSS`
- 原子写入（临时文件 + rename）
- 支持 --dry-run 预览
- 运行前务必停掉 scheduler / realtime_monitor / dashboard 三个进程
  避免并发写入

用法
----
    # 先预览
    python3 migrate_v41.py --dry-run

    # 确认无误后执行
    python3 migrate_v41.py

    # 指定其他文件
    python3 migrate_v41.py --files my_trades.json

回滚
----
发现迁移有误时，把 *.bak.v40.* 改名回原文件即可：
    mv altcoin_shadow_trades.json.bak.v40.20260513T120000 altcoin_shadow_trades.json
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TRADES_FILE = os.path.join(SCRIPT_DIR, 'altcoin_shadow_trades.json')
DEFAULT_ARCHIVE_FILE = os.path.join(SCRIPT_DIR, 'altcoin_trades_archive.json')


def migrate_trades(trades: list, force: bool = False) -> tuple:
    """
    对一组交易做 TP1 双计数修正。

    参数:
        trades: 原始交易字典列表（不会被修改，返回新列表）
        force: True 时无视 _v41_migrated 标记再减一次（危险，仅在误标时用）

    返回: (migrated_trades, stats_dict)
    """
    migrated = []
    stats = {
        'total': len(trades),
        'skipped_open': 0,
        'skipped_no_tp1': 0,
        'skipped_already_migrated': 0,
        'fixed': 0,
        'deltas': [],
    }

    for t in trades:
        # 不改 open 交易
        if t.get('status') != 'closed':
            stats['skipped_open'] += 1
            migrated.append(t)
            continue

        tp1_locked = t.get('tp1_locked_pnl', 0) or 0

        # 没触发 TP1 的 closed 交易不需改
        if tp1_locked == 0:
            stats['skipped_no_tp1'] += 1
            migrated.append(t)
            continue

        # 幂等保护：已迁移过的跳过
        if t.get('_v41_migrated') and not force:
            stats['skipped_already_migrated'] += 1
            migrated.append(t)
            continue

        old_pnl = t.get('pnl', 0) or 0
        new_pnl = round(old_pnl - tp1_locked, 2)

        t_new = dict(t)
        t_new['pnl'] = new_pnl
        t_new['_v41_migrated'] = True
        t_new['_v41_migrated_at'] = datetime.now(timezone.utc).isoformat()

        migrated.append(t_new)
        stats['fixed'] += 1

        # 详细信息用于报告
        # v4.0 下 dashboard 显示的错误总额 = tp1_locked + old_pnl (double counted)
        # v4.0 文件里 pnl 字段本身就是真实总额（= tp1_locked + remaining_pnl）
        # v4.1 下真实总额 = tp1_locked + new_pnl = old_pnl（一致）
        stats['deltas'].append({
            'id': t.get('id', '?'),
            'symbol': t.get('symbol', '?'),
            'closed_at': t.get('closed_at', '')[:10],
            'old_pnl_field': old_pnl,
            'new_pnl_field': new_pnl,
            'tp1_locked': tp1_locked,
            'true_total': round(tp1_locked + new_pnl, 2),
            'buggy_dashboard_showed': round(tp1_locked + old_pnl, 2),
            'inflation_removed': tp1_locked,
        })

    return migrated, stats


def _load_json(path: str) -> list:
    """安全读取 JSON 列表，不存在或损坏则返回空列表"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            print(f"⚠️  {path} 顶层不是 list，跳过", file=sys.stderr)
            return []
        return data
    except (json.JSONDecodeError, IOError) as e:
        print(f"⚠️  无法读取 {path}: {e}", file=sys.stderr)
        return []


def _backup(path: str) -> str:
    """备份原文件，返回备份路径"""
    if not os.path.exists(path):
        return ''
    suffix = datetime.now(timezone.utc).strftime('.bak.v40.%Y%m%dT%H%M%SZ')
    bak = path + suffix
    shutil.copy2(path, bak)
    return bak


def _write_json_atomic(path: str, data: list) -> None:
    """原子写入（临时文件 + rename）"""
    tmp = path + '.migrate.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _print_report(file_label: str, stats: dict, dry_run: bool) -> None:
    mode = '[DRY-RUN]' if dry_run else '[EXECUTE]'
    print(f"\n{mode} {file_label}")
    print(f"  总记录数:              {stats['total']}")
    print(f"  跳过（未平仓）:        {stats['skipped_open']}")
    print(f"  跳过（无 TP1 锁定）:   {stats['skipped_no_tp1']}")
    print(f"  跳过（已迁移）:        {stats['skipped_already_migrated']}")
    print(f"  需修正:               {stats['fixed']}")

    if not stats['deltas']:
        return

    print(f"\n  修正明细：")
    header = (f"    {'日期':<12} {'币种':<14} {'ID':<38} "
              f"{'旧pnl':>8} →{'新pnl':>8} "
              f"{'TP1锁定':>8} {'真实总额':>9} {'旧dash显示':>11}")
    print(header)
    print('    ' + '-' * (len(header) - 4))

    total_inflation = 0.0
    for d in stats['deltas']:
        total_inflation += d['inflation_removed']
        print(f"    {d['closed_at']:<12} {d['symbol']:<14} {d['id']:<38} "
              f"{d['old_pnl_field']:+8.2f}  {d['new_pnl_field']:+8.2f} "
              f"{d['tp1_locked']:+8.2f} {d['true_total']:+9.2f} "
              f"{d['buggy_dashboard_showed']:+11.2f}")

    print(f"\n  旧 dashboard / tg_bot 的合计虚高："
          f"{total_inflation:+.2f} U（= 所有 tp1_locked 之和）")
    print(f"  迁移后显示的合计盈亏会减少这个金额——这才是真实数字。")


def main():
    parser = argparse.ArgumentParser(
        description='v4.0 → v4.1 TP1 双计数历史数据修复迁移',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="运行前记得先停掉 scheduler / realtime_monitor / dashboard，避免并发写冲突。",
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='只预览不写文件')
    parser.add_argument('--force', action='store_true',
                        help='忽略 _v41_migrated 标记强制再跑（危险，可能把正确数据再减一次）')
    parser.add_argument('--files', nargs='+',
                        default=[DEFAULT_TRADES_FILE, DEFAULT_ARCHIVE_FILE],
                        help='要迁移的 JSON 文件路径（默认：主交易文件 + 归档文件）')
    args = parser.parse_args()

    if args.force and not args.dry_run:
        print("⚠️  --force 会无视幂等标记。这可能把已经正确的数据再减一次。")
        resp = input("确认继续？请输入 yes： ")
        if resp.strip().lower() != 'yes':
            print("已取消。")
            sys.exit(1)

    overall_fixed = 0
    overall_inflation = 0.0

    for path in args.files:
        label = os.path.basename(path)
        trades = _load_json(path)
        if not trades:
            print(f"\n[SKIP] {label} 不存在或为空")
            continue

        migrated, stats = migrate_trades(trades, force=args.force)
        _print_report(label, stats, args.dry_run)

        overall_fixed += stats['fixed']
        overall_inflation += sum(d['inflation_removed'] for d in stats['deltas'])

        if args.dry_run or stats['fixed'] == 0:
            continue

        bak = _backup(path)
        if bak:
            print(f"  📦 备份: {bak}")
        _write_json_atomic(path, migrated)
        print(f"  ✅ 已写入 {path}")

    print("\n" + "=" * 64)
    if args.dry_run:
        print(f"DRY-RUN：共 {overall_fixed} 笔交易需要修正，未实际写入。")
        print(f"预计合计虚高消除：{overall_inflation:+.2f} U")
        print("确认无误后去掉 --dry-run 重新运行即可执行。")
    else:
        print(f"迁移完成：共修正 {overall_fixed} 笔交易")
        print(f"合计虚高消除：{overall_inflation:+.2f} U")
        print("原文件已备份为 *.bak.v40.* 文件，如有异常可恢复。")


if __name__ == '__main__':
    main()
