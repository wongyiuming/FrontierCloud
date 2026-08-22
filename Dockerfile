# 使用官方 Python 3.14 slim 镜像（Debian-based）
FROM python:3.14-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖（关键！）
# - libgl1: PyMuPDF 需要
# - libglib2.0-0: Pillow 可能需要
# - zlib1g-dev, libjpeg-dev: Pillow 处理图片
# - fonts-wqy-microhei: 中文字体
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        zlib1g-dev \
        libjpeg-dev \
        libpng-dev \
        fonts-wqy-microhei && \
    rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY --chmod=0644 pyproject.toml .
# Git worktree permissions can be restrictive on the host. The unprivileged
# runtime user must always be able to import the application entry point.
COPY --chmod=0644 main.py .
COPY app ./app
COPY static ./static
COPY tests ./tests

# Runtime code is immutable inside the image. Normalize permissions after COPY
# so a restrictive host umask cannot make source files unreadable by UID 10001.
RUN chmod -R a+rX /app/app /app/static /app/tests

# 安装 Python 依赖（使用已修复归档/入口点漏洞的 pip）
# 注意：如果你用 poetry，可改用 poetry export
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.1.2" && \
    python -m pip install --no-cache-dir .

# 公开文件解析不需要 root；固定 UID 便于宿主机授权 data 目录。
RUN groupadd --system --gid 10001 appuser && \
    useradd --system --uid 10001 --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser && \
    mkdir -p /app/data && \
    chown 10001:10001 /app/data

# 设置中文字体环境变量（关键！）
ENV WATERMARK_FONT_PATH=/usr/share/fonts/truetype/wqy/wqy-microhei.ttc
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# 暴露端口
EXPOSE 8000

# 运行阶段保持最低权限。
USER 10001:10001

# 启动命令
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log"]
