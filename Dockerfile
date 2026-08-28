# Use the official Debian-based Python 3.14 slim image.
FROM python:3.14-slim

# Set the application working directory.
WORKDIR /app

# Install the native libraries required by PyMuPDF and Pillow, plus a CJK font.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        zlib1g-dev \
        libjpeg-dev \
        libpng-dev \
        fonts-wqy-microhei && \
    rm -rf /var/lib/apt/lists/*

# Copy dependency metadata before application sources for better layer reuse.
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

# Install Python dependencies with a pip release that includes the archive and
# entry-point security fixes required by this image.
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.1.2" && \
    python -m pip install --no-cache-dir .

# Public file processing does not require root. A fixed UID simplifies host
# permissions for the data directory.
RUN groupadd --system --gid 10001 appuser && \
    useradd --system --uid 10001 --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser && \
    mkdir -p /app/data && \
    chown 10001:10001 /app/data

# Configure the CJK font used by the watermark renderer.
ENV WATERMARK_FONT_PATH=/usr/share/fonts/truetype/wqy/wqy-microhei.ttc
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# Document the application port.
EXPOSE 8000

# Keep the runtime process unprivileged.
USER 10001:10001

# Start the ASGI server without trusting client-supplied proxy headers.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log"]
