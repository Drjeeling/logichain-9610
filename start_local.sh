#!/usr/bin/env bash
# 智关 Agent 本地一键启动（macOS / Linux）
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "[1/3] 创建虚拟环境 .venv ..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[2/3] 安装依赖（首次约 1-3 分钟）..."
# 国内网络优先走清华源，失败再回退官方源
PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
pip install -q --upgrade pip -i "$PIP_INDEX" || pip install -q --upgrade pip
pip install -q -r requirements_local.txt -i "$PIP_INDEX" || pip install -q -r requirements_local.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "!! 已生成 .env，请填入 DEEPSEEK_API_KEY 后重新运行本脚本"
  echo "   （不填也能跑，但 Agent 不会调用 LLM，走规则兜底）"
  echo ""
fi

echo "[3/3] 启动服务： http://localhost:8000"
python -m uvicorn web.api.server:app --host 127.0.0.1 --port 8000
