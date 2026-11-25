# BriefNexus — 通用信息采集服务
#
# 单阶段构建：平台是纯 Python，无可编译依赖，多阶段收益不大
# 镜像目标 < 300MB（仅 fastapi/uvicorn/requests/bs4/trafilatura）
FROM python:3.12-slim

# 中文站编码检测需要（chardet 的可选依赖）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖先装（利用层缓存：源码改动不重装依赖）
COPY platform/requirements.txt requirements-platform.txt
COPY intel/requirements.txt requirements-intel.txt
RUN pip install --upgrade pip \
 && pip install -r requirements-platform.txt -r requirements-intel.txt \
 # 正文抽取（v1/url 核心能力），不在 requirements 里是因为体积大且可选
 && pip install "trafilatura>=2.0" "readabilipy>=0.3"

# 源码
COPY intel/ ./intel/
COPY platform/ ./platform/
COPY scripts/ ./scripts/

# 非 root 运行
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 9000

# 单 worker 是硬要求：内存态存储只在单进程内可见
CMD ["python", "platform/run.py"]