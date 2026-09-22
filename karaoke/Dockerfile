FROM rust:1.90-alpine AS build
WORKDIR /build
COPY karaoke/Cargo.toml ./Cargo.toml
COPY karaoke/Cargo.lock ./Cargo.lock
COPY karaoke/src ./src
COPY karaoke/static ./static
RUN cargo build --release --locked

FROM alpine:3.22
RUN addgroup -g 10002 karaoke && adduser -D -H -u 10002 -G karaoke karaoke
COPY --from=build /build/target/release/frontiercloud-karaoke /usr/local/bin/frontiercloud-karaoke
USER 10002:10002
EXPOSE 8090
ENTRYPOINT ["/usr/local/bin/frontiercloud-karaoke"]
