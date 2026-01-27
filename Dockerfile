# 第一阶段：构建环境
FROM python:3.11-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    fastapi uvicorn starlette itsdangerous python-multipart

# 第二阶段：运行环境
FROM python:3.11-slim
WORKDIR /app
# 仅安装运行必须的最小工具包
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && apt-get install -y --no-install-recommends curl libc-bin && \
    rm -rf /var/lib/apt/lists/*
    
# 从构建阶段拷贝已安装的库
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn

COPY . .
EXPOSE 18080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "18080"]
