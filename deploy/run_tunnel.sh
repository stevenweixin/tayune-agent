#!/bin/zsh
# 只启动本项目隧道，不启动后端、不修改数据库，也不安装开机服务。
set -eu
cd "$(dirname "$0")/.."
export HOME="$PWD/deploy/.cloudflare-local/home"
BIN="$PWD/deploy/.cloudflare-local/bin/cloudflared"
CONFIG="$PWD/deploy/.cloudflare-local/config.yml"
if [[ ! -x "$BIN" || ! -f "$CONFIG" ]]; then
    print -u2 "缺少本机 cloudflared 或隧道配置，请先完成本机部署。"
    exit 1
fi
exec "$BIN" tunnel --config "$CONFIG" --pidfile "$PWD/deploy/.cloudflare-local/tunnel.pid" run
