#!/bin/sh
set -eu

workspace=${1:-/build/karaoke}
destination=${2:-/build/karaoke-dist}
cd "$workspace"
rm -rf "$destination"
mkdir -p "$destination"

cargo test --workspace --locked
cargo build --release --locked --target wasm32-unknown-unknown -p frontier-karaoke-web -p frontier-karaoke-worklet
wasm-bindgen target/wasm32-unknown-unknown/release/frontier_karaoke_web.wasm \
  --target web --no-typescript --out-dir "$destination" --out-name frontier_karaoke_web
cp target/wasm32-unknown-unknown/release/frontier_karaoke_worklet.wasm "$destination/"
cp web/index.html web/bootstrap.js web/audio-worklet.js web/karaoke.css "$destination/"

manifest="$destination/manifest.json"
printf '{"version":1,"files":{' > "$manifest"
separator=
for path in "$destination"/*; do
  [ "$(basename "$path")" = manifest.json ] && continue
  name=$(basename "$path")
  bytes=$(stat -c %s "$path")
  digest=$(sha256sum "$path" | cut -d ' ' -f 1)
  printf '%s"%s":{"bytes":%s,"sha256":"%s"}' "$separator" "$name" "$bytes" "$digest" >> "$manifest"
  separator=,
done
printf '}}\n' >> "$manifest"
