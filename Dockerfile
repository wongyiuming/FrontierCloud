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
COPY pyproject.toml .
COPY main.py .

# 安装 Python 依赖（使用 pip）
# 注意：如果你用 poetry，可改用 poetry export
RUN pip install --no-cache-dir .

# 复制应用代码

# 设置中文字体环境变量（关键！）
ENV WATERMARK_FONT_PATH=/usr/share/fonts/truetype/wqy/wqy-microhei.ttc
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]