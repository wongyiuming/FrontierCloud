FROM rust:1.90-bookworm AS karaoke-build
ARG WASM_BINDGEN_VERSION=0.2.104
WORKDIR /build
RUN rustup target add wasm32-unknown-unknown && \
    curl -fsSL "https://github.com/wasm-bindgen/wasm-bindgen/releases/download/${WASM_BINDGEN_VERSION}/wasm-bindgen-${WASM_BINDGEN_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
      | tar -xz -C /tmp && \
    install "/tmp/wasm-bindgen-${WASM_BINDGEN_VERSION}-x86_64-unknown-linux-musl/wasm-bindgen" /usr/local/bin/wasm-bindgen
COPY karaoke /build/karaoke
COPY contracts /build/contracts
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/karaoke/target \
    chmod +x /build/karaoke/build.sh && /build/karaoke/build.sh /build/karaoke /build/karaoke-dist

FROM nginx:1.30.4-alpine

# Remove the bundled default server.
RUN rm /etc/nginx/conf.d/default.conf

# Keep runtime environment substitution in Nginx templates.
COPY nginx/nginx.conf /etc/nginx/templates/nginx.conf.template
COPY --chmod=0755 nginx/15-select-tls.sh /docker-entrypoint.d/15-select-tls.sh
COPY --chmod=0755 nginx/25-ip-security.sh /docker-entrypoint.d/25-ip-security.sh
COPY nginx/render-security.awk /etc/nginx/render-security.awk
COPY nginx/security-headers.conf /etc/nginx/security-headers.conf
COPY nginx/karaoke-security-headers.conf /etc/nginx/karaoke-security-headers.conf
COPY nginx/transport /etc/nginx/transport
COPY static /app/static
COPY --from=karaoke-build /build/karaoke-dist /app/static/karaoke
RUN chmod -R a+rX /app/static

EXPOSE 80 443

CMD ["nginx", "-g", "daemon off;"]
