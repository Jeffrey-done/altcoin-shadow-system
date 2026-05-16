#!/bin/sh
# docker-entrypoint.sh — 确保运行时数据文件存在（防止 Docker 将缺失的 bind mount 创建为目录）

set -e

# 列表类型文件（trades / candidates / archive 存的是 JSON array）
LIST_FILES="
altcoin_shadow_trades.json
altcoin_candidates.json
altcoin_trades_archive.json
"

# 字典类型文件（risk_state / weekly_report 存的是 JSON object）
DICT_FILES="
risk_state.json
weekly_report.json
"

for f in $LIST_FILES; do
    filepath="/app/$f"
    if [ ! -f "$filepath" ]; then
        echo "[]" > "$filepath"
        echo "[entrypoint] 已初始化（array）: $filepath"
    fi
done

for f in $DICT_FILES; do
    filepath="/app/$f"
    if [ ! -f "$filepath" ]; then
        echo "{}" > "$filepath"
        echo "[entrypoint] 已初始化（object）: $filepath"
    fi
done

# 确保缓存目录存在
mkdir -p /app/backtest_cache

exec "$@"
