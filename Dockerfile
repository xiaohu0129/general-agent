# ---- general-agent 后端镜像 ----
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先装依赖（利用层缓存）：复制构建元数据 + 源码后安装
COPY pyproject.toml ./
COPY general_agent ./general_agent
RUN pip install .

# 运行时从 /app 导入源码（PYTHONPATH 优先），确保能读到 /app/config.yaml
ENV PYTHONPATH=/app
COPY config.yaml ./config.yaml

EXPOSE 9093

# 单实例：单 uvicorn 进程承载内存 Broker 与登录态；asyncio 可支撑数百并发 SSE
CMD ["python", "-m", "general_agent"]
