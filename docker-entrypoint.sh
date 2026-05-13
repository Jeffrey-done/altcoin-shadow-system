#!/bin/sh
# docker-entrypoint.sh — 确保运行时数据文件存在（防止 Docker 将缺失的 bind mount 创建为目录）

set -e

DATA_FILES="
altcoin_shadow_trades.json
altcoin_candidates.json
risk_state.json
altcoin_trades_archive.json
weekly_report.json
"

for f in $DATA_FILES; do
    filepath="/app/$f"
    if [ ! -f "$filepath" ]; then
        echo "{}" > "$filepath"
        echo "[entrypoint] 已初始化: $filepath"
    fi
done

# 确保缓存目录存在
mkdir -p /app/backtest_cache

exec "$@"
