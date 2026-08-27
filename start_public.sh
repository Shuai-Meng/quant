#!/usr/bin/env bash
# 公网展示模式启动脚本：启用 Basic Auth + 只读模式
# 访问方式: http://<公网IP>:18888  用户名/密码见 config/access.txt
set -e
cd "$(dirname "$0")"

# 读取访问凭据（由 setup_public.sh 生成）
if [ -f config/access.txt ]; then
    export BASIC_AUTH_USER=$(cut -d: -f1 config/access.txt)
    export BASIC_AUTH_PASS=$(cut -d: -f2 config/access.txt)
fi
export READ_ONLY=1

pkill -f "[s]ervice\.py daemon" 2>/dev/null || true
sleep 1
nohup .venv/bin/python service.py daemon > state/daemon.log 2>&1 < /dev/null &
echo "PID=$!"
PUB_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo "公网IP待确认")
echo "公开访问: http://${PUB_IP}:18888"
echo "凭据: 见 config/access.txt"
