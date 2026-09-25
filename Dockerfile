# 一个镜像跑两个服务：载体 commerce-core 与 Agent API（只差启动命令）
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY scripts ./scripts
COPY migrations ./migrations
EXPOSE 8000 8001
