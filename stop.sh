#!/usr/bin/env bash
# 停止 CodeArts 代理（按监听端口精准定位，避免误杀同名进程；端口以 config.json 为唯一来源）
PORT=$(python3 -c "import config_store; config_store.init('config.json'); print(config_store.get('CODEARTS_PROXY_PORT', '51818'))" 2>/dev/null || echo "51818")

PIDS=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null)
if [ -z "$PIDS" ]; then
    echo "端口 $PORT 无运行中的代理服务"
    exit 0
fi

echo "停止代理 (PID: $PIDS, 端口 $PORT)..."
kill $PIDS
sleep 1
# 1 秒后仍在监听则强制终止
if lsof -ti tcp:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "进程未退出，强制终止..."
    kill -9 $PIDS
fi
echo "已停止"
