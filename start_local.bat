@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv (
  echo [1/3] 创建虚拟环境 .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo 创建失败：请确认已安装 Python 3.10+ 并勾选 Add to PATH
    pause
    exit /b 1
  )
)

call .venv\Scripts\activate.bat

echo [2/3] 安装依赖（首次约 1-3 分钟）...
set PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
pip install -q --upgrade pip -i %PIP_INDEX%
if errorlevel 1 pip install -q --upgrade pip
pip install -q -r requirements_local.txt -i %PIP_INDEX%
if errorlevel 1 pip install -q -r requirements_local.txt

if not exist .env (
  copy .env.example .env >nul
  echo.
  echo !! 已生成 .env，请用记事本填入 DEEPSEEK_API_KEY 后重新运行本脚本
  echo    不填也能跑，但 Agent 不会调用 LLM，走规则兜底
  echo.
)

echo [3/3] 启动服务： http://localhost:8000
python -m uvicorn web.api.server:app --host 127.0.0.1 --port 8000

pause
