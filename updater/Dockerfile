FROM docker:27-cli
RUN apk add --no-cache docker-cli-compose git python3 curl ca-certificates
WORKDIR /workspace
CMD ["python3", "/workspace/updater/server.py"]
