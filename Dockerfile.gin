# Build a statically linked Go runtime without Python or CGO.
FROM golang:1.26.0-bookworm AS build

WORKDIR /src
COPY go.mod go.sum ./
RUN /usr/local/go/bin/go mod download
COPY cmd ./cmd
COPY internal ./internal
COPY protocol ./protocol
COPY migrations ./migrations
COPY static ./static
# Keep deployment contract tests executable in the build stage only.
COPY Dockerfile Dockerfile.gin ./
COPY updater/Dockerfile updater/Dockerfile.gin ./updater/
# Bound per-build package/compiler parallelism. Several independent node agents
# may build on one Engine; inheriting every host CPU can exhaust host memory.
RUN GOMAXPROCS=2 CGO_ENABLED=0 /usr/local/go/bin/go test -p=1 ./... && \
    GOMAXPROCS=2 CGO_ENABLED=0 /usr/local/go/bin/go build -p=1 -trimpath -ldflags="-s -w" -o /out/frontiercloud ./cmd/frontiercloud

FROM debian:13.3-slim
ARG REVISION
LABEL frontiercloud.revision=$REVISION frontiercloud.component="web" \
      frontiercloud.runtime="go" frontiercloud.schema-generation="2" \
      frontiercloud.release-manifest-version="1"

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --system --gid 10001 appuser && \
    useradd --system --uid 10001 --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser && \
    mkdir -p /app/data /data && \
    chown 10001:10001 /app/data /data

WORKDIR /app
COPY --from=build /out/frontiercloud /app/frontiercloud
COPY --chown=10001:10001 static /app/static
COPY internal/search/data/LICENSE.pypinyin.txt internal/search/data/LICENSE.opencc.txt /app/licenses/
RUN chmod -R a+rX /app/static

ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
EXPOSE 8000
USER 10001:10001
ENTRYPOINT ["/app/frontiercloud"]
CMD ["serve"]
