@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ==========================================
echo   智关 Agent 9610 - 本地一键部署
echo ==========================================
echo.

set PYEXE=python

echo [0/4] 检测 Python 版本（需要 3.10 - 3.12）...
%PYEXE% -c "import sys; sys.exit(0 if (3,10)<=sys.version_info<(3,13) else 1)" >nul 2>nul
if not errorlevel 1 (
  for /f "tokens=2" %%v in ('%PYEXE% --version 2^>^&1') do echo       已找到 Python %%v
  goto build
)

echo       未找到可用的 Python 3.10-3.12
echo.
echo       尝试通过 winget 自动安装 Python 3.12 ...
where winget >nul 2>nul
if errorlevel 1 goto manual

winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto manual

echo.
echo       安装完成。请关闭本窗口，重新双击 start_local.bat 继续。
pause
exit /b 0

:manual
echo.
echo --------------------------------------------------
echo  需要手动安装 Python 3.12：
echo   1. 访问 https://www.python.org/downloads/
echo   2. 下载 Windows installer ^(64-bit^)
echo   3. 安装时务必勾选 "Add python.exe to PATH"
echo   4. 安装完成后重新双击本脚本
echo --------------------------------------------------
pause
exit /b 1

:build
echo.
if not exist .venv (
  echo [1/4] 创建虚拟环境 .venv ...
  %PYEXE% -m venv .venv
  if errorlevel 1 (
    echo 创建虚拟环境失败，请确认 Python 安装完整
    pause
    exit /b 1
  )
)
call .venv\Scripts\activate.bat

echo [2/4] 安装依赖（国内镜像，首次约 1-3 分钟）...
set PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
pip install -q --upgrade pip -i %PIP_INDEX% 2>nul || pip install -q --upgrade pip
pip install -q -r requirements_local.txt -i %PIP_INDEX% 2>nul || pip install -q -r requirements_local.txt
if errorlevel 1 (
  echo 依赖安装失败，请检查网络后重试
  pause
  exit /b 1
)

echo [3/4] 检查配置文件 ...
if not exist .env (
  copy .env.example .env >nul
  echo       已生成 .env。要启用 AI 能力，请用记事本打开 .env
  echo       把 DEEPSEEK_API_KEY=sk-your-key-here 改成你的真实 Key
  echo       不填也能运行，但 Agent 只走规则兜底
) else (
  echo       已存在 .env
)

echo [4/4] 启动服务： http://localhost:8000
echo       首次 OCR 识别约需 5-10 秒，之后更快
echo       按 Ctrl+C 停止服务
echo.
start "" http://localhost:8000
python -m uvicorn web.api.server:app --host 127.0.0.1 --port 8000

pause
