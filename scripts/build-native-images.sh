#!/usr/bin/env bash
# Linux operator bootstrap: immutable native images, no deployment mutation.
set -euo pipefail
cd "$(dirname "$0")/.."
revision="${1:-${FRONTIERCLOUD_REVISION:-}}"
if [[ $# -gt 1 || ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
  printf '%s\n' 'Provide one exact committed native source SHA' >&2
  exit 1
fi
test "$(git rev-parse --verify "$revision^{commit}")" = "$revision"
git cat-file -e "$revision:Dockerfile.gin"
git cat-file -e "$revision:updater/Dockerfile.gin"
# Never send .env, data, keys, untracked files, .git or mutable source changes.
paths=(Dockerfile Dockerfile.gin go.mod go.sum cmd internal migrations protocol static nginx updater/Dockerfile updater/Dockerfile.gin)
for component in web updater nginx; do
  case "$component" in
    web) dockerfile=Dockerfile.gin ;;
    updater) dockerfile=updater/Dockerfile.gin ;;
    nginx) dockerfile=nginx/Dockerfile ;;
  esac
  args=(--build-arg "REVISION=$revision")
  if [[ "$component" == nginx ]]; then args+=(--build-arg FRONTIERCLOUD_RUNTIME=go); fi
  git archive --format=tar "$revision" -- "${paths[@]}" |
    docker --host unix:///var/run/docker.sock build -f "$dockerfile" "${args[@]}" \
      -t "frontiercloud-go-$component:$revision" -
done
printf 'Built immutable native candidates at %s; no services were replaced.\n' "$revision"
printf '%s\n' 'Production publication still requires reviewed source/CI proof.'
