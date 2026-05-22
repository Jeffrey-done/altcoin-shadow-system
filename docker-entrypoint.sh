#!/bin/sh
# docker-entrypoint.sh — 确保运行时数据文件存在且是普通文件
#
# 防御 Docker bind mount 副作用：宿主机上的目标路径不存在时，daemon 会自动把
# mount point 创建为目录（详见 docker-compose.yml 顶部注释）。这里要能从这种
# 状态自愈，否则容器会进 restart loop。
#
# 兼容场景：
#   1. 普通文件已存在 → 跳过
#   2. 路径不存在     → 写入默认值
#   3. 路径是空目录   → rmdir 后写入默认值
#   4. 路径是非空目录 → rm -rf 后写入默认值（说明是 bind mount 误产物，无业务数据）
#   5. 其它类型       → 报错退出（防止误删特殊文件）

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

# 把误挂的目录修复为"路径不存在"，让后续 echo > 能正常写入
fix_dir_to_missing() {
    p="$1"
    # 排除 symlink（-L）和非目录（-d）
    if [ -d "$p" ] && [ ! -L "$p" ]; then
        if rmdir "$p" 2>/dev/null; then
            echo "[entrypoint] ⚠️  $p 是空目录（bind mount 副作用），已删除"
        else
            echo "[entrypoint] ⚠️  $p 是非空目录，强制清理（bind mount 误产物，无业务数据）"
            rm -rf "$p"
        fi
    fi
}

ensure_file() {
    p="$1"
    default="$2"
    kind="$3"

    fix_dir_to_missing "$p"

    if [ ! -e "$p" ]; then
        echo "$default" > "$p"
        echo "[entrypoint] 已初始化（$kind）: $p"
    elif [ -f "$p" ]; then
        : # 已是普通文件，OK
    else
        # symlink / fifo / 设备文件等，未知形态，不敢动
        echo "[entrypoint] ❌ $p 既不是普通文件也不是目录（type=$(stat -c %F "$p" 2>/dev/null || echo unknown)），退出"
        exit 1
    fi
}

for f in $LIST_FILES; do
    ensure_file "/app/$f" "[]" "array"
done

for f in $DICT_FILES; do
    ensure_file "/app/$f" "{}" "object"
done

# 缓存目录（这个本来就该是目录，不需要 fix）
mkdir -p /app/backtest_cache

# 启动前语法校验（防止定时任务因语法错误静默失效）
if [ "${SKIP_PY_COMPILE_CHECK:-0}" != "1" ]; then
    echo "[entrypoint] 运行 python3 -m compileall 语法校验..."
    python3 -m compileall -q /app
fi

# 敏感文件权限加固（仅在文件存在时执行）
for sf in /app/.env /app/admin_secrets.json /app/runtime_config.json /app/admin_audit.log /app/.admin_ratelimit.json; do
    if [ -f "$sf" ]; then
        chmod 600 "$sf" 2>/dev/null || true
    fi
done

exec "$@"
