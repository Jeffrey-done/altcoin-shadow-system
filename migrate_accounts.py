#!/usr/bin/env python3
"""
一次性迁移脚本：将所有无 account_id 的历史交易绑定到影子账户。

运行方式：python3 migrate_accounts.py
安全：幂等（多次运行不会重复修改）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import TRADES_FILE, TRADES_ARCHIVE_FILE, LockedJsonFile, setup_logger
from admin_secrets import SHADOW_ACCOUNT_ID, ensure_shadow_account

logger = setup_logger("migrate_accounts")


def migrate_file(filepath: str) -> int:
    """给指定 JSON 文件中所有 account_id 为空的交易打上影子账户 ID。返回修改数量。"""
    if not os.path.exists(filepath):
        return 0

    count = 0
    with LockedJsonFile(filepath, default=[]) as (trades, save):
        for t in trades:
            if not t.get('account_id'):
                t['account_id'] = SHADOW_ACCOUNT_ID
                count += 1
        if count > 0:
            save(trades)
    return count


def main():
    # 确保影子账户存在
    ensure_shadow_account()
    logger.info(f"影子账户 ID: {SHADOW_ACCOUNT_ID}")

    # 迁移主交易文件
    n1 = migrate_file(TRADES_FILE)
    logger.info(f"主交易文件: 已标记 {n1} 笔交易 → {SHADOW_ACCOUNT_ID}")

    # 迁移归档文件
    n2 = migrate_file(TRADES_ARCHIVE_FILE)
    logger.info(f"归档文件: 已标记 {n2} 笔交易 → {SHADOW_ACCOUNT_ID}")

    total = n1 + n2
    if total == 0:
        logger.info("✅ 无需迁移（所有交易已有 account_id）")
    else:
        logger.info(f"✅ 迁移完成：共 {total} 笔交易已绑定到影子账户")


if __name__ == '__main__':
    main()
