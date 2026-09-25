FROM python:3.14-alpine

RUN apk add --no-cache ca-certificates git \
    && python -m pip install --no-cache-dir docker==7.1.0

WORKDIR /workspace
CMD ["python", "/workspace/updater/server.py"]
