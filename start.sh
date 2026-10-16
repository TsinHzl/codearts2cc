#!/usr/bin/env bash
# 启动 CodeArts 代理（OpenAI + Anthropic Messages 兼容端点）
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "错误: 缺少 .env，请先执行 cp .env.example .env 并填入 CODEARTS_AK/CODEARTS_SK" >&2
    exit 1
fi

# 端口被旧实例占用时先停止，保证一条命令始终可启动
PORT="${CODEARTS_PROXY_PORT:-8787}"
PIDS=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null) || true
if [ -n "$PIDS" ]; then
    echo "端口 $PORT 已被占用 (PID: $PIDS)，先停止旧实例..."
    kill $PIDS
    for i in $(seq 1 10); do
        lsof -ti tcp:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
        sleep 1
    done
    if lsof -ti tcp:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "旧实例未退出，强制终止..." >&2
        kill -9 $PIDS
        sleep 1
    fi
fi

echo "启动 CodeArts 代理: http://127.0.0.1:$PORT (Ctrl+C 停止)"
exec python3 server.py
