# 智关 Agent — 9610 出口申报自动化系统
# Render.com / Railway / Fly.io 部署镜像
FROM python:3.11-slim

# 系统依赖：rapidocr-onnxruntime 需要 libgomp / libgl1
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先拷依赖文件缓存 layer
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 项目文件
COPY . .

# .env 里不要带 DEEPSEEK_API_KEY 之类的凭证；用平台环境变量注入
EXPOSE 8000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

# Render 会覆盖 PORT；其他平台用固定端口
CMD uvicorn web.api.server:app --host 0.0.0.0 --port ${PORT:-8000}
