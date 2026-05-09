# 蝦皮代操儀表板 — Cloud Run 部署用
# 使用 python:3.12-slim（小、穩、快）
FROM python:3.12-slim

# 設定工作目錄
WORKDIR /app

# 環境變數
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

# 安裝相依套件（先複製 requirements 利用 Docker layer cache）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式碼
COPY *.py ./
COPY *.html ./
COPY *.js ./

# 建立資料目錄（Cloud Run 會 mount GCS bucket 到這個位置）
RUN mkdir -p ${DATA_DIR}/watch ${DATA_DIR}/uploads

# Cloud Run 強制要求監聽 $PORT（通常是 8080）
ENV PORT=8080

# Healthcheck — 讓 Cloud Run 知道 server 是否健康
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen(f'http://localhost:{8080}/api/v1/ad-data/status', timeout=3)" || exit 1

# 啟動 server（用 uvicorn 直接跑，吃 $PORT）
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
