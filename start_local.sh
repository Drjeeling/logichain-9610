#!/usr/bin/env bash
# 智关 Agent 9610 - 本地一键部署（macOS / Linux）
set -e
cd "$(dirname "$0")"

echo "=========================================="
echo "  智关 Agent 9610 - 本地一键部署"
echo "=========================================="

# ---------- [0/4] 检测 Python 3.10-3.12 ----------
PY=""
for cand in python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c "import sys; sys.exit(0 if (3,10)<=sys.version_info<(3,13) else 1)" >/dev/null 2>&1; then
      PY="$cand"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "[0/4] 未找到 Python 3.10-3.12"
  if command -v brew >/dev/null 2>&1; then
    echo "      检测到 Homebrew，正在安装 python@3.12 ..."
    brew install python@3.12
    PY="python3.12"
  else
    echo ""
    echo "--------------------------------------------------"
    echo "  需要手动安装 Python 3.12："
    echo "   macOS: https://www.python.org/downloads/"
    echo "   或安装 Homebrew 后执行: brew install python@3.12"
    echo "--------------------------------------------------"
    exit 1
  fi
else
  echo "[0/4] 已找到 $PY ($("$PY" --version 2>&1))"
fi

# ---------- [1/4] 虚拟环境 ----------
if [ ! -d ".venv" ]; then
  echo "[1/4] 创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# ---------- [2/4] 依赖 ----------
echo "[2/4] 安装依赖（国内镜像，首次约 1-3 分钟）..."
PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
pip install -q --upgrade pip -i "$PIP_INDEX" || pip install -q --upgrade pip
pip install -q -r requirements_local.txt -i "$PIP_INDEX" || pip install -q -r requirements_local.txt

# ---------- [3/4] 配置 ----------
echo "[3/4] 检查配置文件 ..."
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "      已生成 .env。要启用 AI 能力，请编辑 .env 填入真实的 DEEPSEEK_API_KEY"
  echo "      不填也能运行，但 Agent 只走规则兜底"
else
  echo "      已存在 .env"
fi

# ---------- [4/4] 启动 ----------
echo "[4/4] 启动服务： http://localhost:8000"
echo "      按 Ctrl+C 停止服务"
echo ""
if command -v open >/dev/null 2>&1; then
  (sleep 2 && open http://localhost:8000) &
elif command -v xdg-open >/dev/null 2>&1; then
  (sleep 2 && xdg-open http://localhost:8000) &
fi

python -m uvicorn web.api.server:app --host 127.0.0.1 --port 8000
