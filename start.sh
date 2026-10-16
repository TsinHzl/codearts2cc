#!/usr/bin/env bash
# 启动 CodeArts 代理（OpenAI + Anthropic Messages 兼容端点）
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "错误: 缺少 .env，请先执行 cp .env.example .env 并填入 CODEARTS_AK/CODEARTS_SK" >&2
    exit 1
fi

# admin 管理界面口令门：未设置 ADMIN_PASSWORD 时引��设置并写回 .env，设置完成才继续启动
if [ -z "${ADMIN_PASSWORD:-}" ] && ! grep -Eq '^[[:space:]]*(export[[:space:]]+)?ADMIN_PASSWORD[[:space:]]*=[[:space:]]*[^[:space:]#]' .env; then
    if [ ! -t 0 ]; then
        echo "错误: 未设置 ADMIN_PASSWORD（admin 管理界面口令），且当前为非交互环境无法引导设置" >&2
        echo "请先在 .env 中写入 ADMIN_PASSWORD=<你的密码> 后重试" >&2
        exit 1
    fi
    # read -s 由 bash 在 read 返回时自动恢复回显（EOF/Ctrl+C 路径安全，不残留
    # stty -echo 状态）；read 置于 if 条件上下文，EOF 显式报错退出而非被 set -e 静默终止
    read_secret() {  # $1=提示 $2=目标变量名
        if ! IFS= read -rs -p "$1" "$2"; then
            printf '\n错误: 读取密码输入失败，已取消启动\n' >&2
            exit 1
        fi
        printf '\n'
    }
    echo "检测到尚未设置 admin 管理界面访问密码，请先完成设置（写入 .env 的 ADMIN_PASSWORD）"
    while true; do
        read_secret "请输入管理界面访问密码: " admin_pw
        read_secret "请再次输入以确认: " admin_pw2
        if [ -z "$admin_pw" ]; then
            echo "密码不能为空，请重新设置" >&2
        elif [ "$admin_pw" != "$admin_pw2" ]; then
            echo "两次输入不一致，请重新设置" >&2
        elif ! printf '%s' "$admin_pw" | grep -Eq '^[A-Za-z0-9_@%+=:,.!?/~^-]+$'; then
            echo "密码仅可包含字母、数字及 _ @ % + = : , . ! ? / ~ ^ -，请重新设置" >&2
        else
            printf '\nADMIN_PASSWORD=%s\n' "$admin_pw" >> .env
            chmod 600 .env  # 写入明文口令后收紧权限（同文件内的 AK/SK 一并受益）
            echo "管理密码已写入 .env，启动后访问 /admin 登录"
            break
        fi
    done
fi

# 端口被旧实例占用时先停止，保证一条命令始终可启动
PORT="${CODEARTS_PROXY_PORT:-8787}"
PIDS=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null) || true
if [ -n "$PIDS" ]; then
    echo "端口 $PORT 已被占用 (PID: $PIDS)，先停止旧实例..."
    kill $PIDS 2>/dev/null || true
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
