#!/bin/bash
# Build trusted pricing bytes before any sandbox call.
set -euo pipefail
test "$(id -u)" = 0 || { echo 'Provision the native runtime as root.' >&2; exit 1; }
test "$#" = 1 || { echo 'Usage: provision.sh ABSOLUTE_NEW_DIRECTORY' >&2; exit 1; }
case "$1" in /*) ;; *) echo 'Destination must be absolute.' >&2; exit 1;; esac
target=$(realpath -m -- "$1")
test ! -e "$target" || { echo 'Destination must not exist.' >&2; exit 1; }
repo=$(cd -- "$(dirname -- "$0")/../../.." && pwd)
mkdir -p "$target/runtime" "$target/native"
container=
trap 'if [ -n "$container" ]; then docker rm -f "$container"; fi' EXIT
docker build -t maf-pricing-base:qualification "$repo/images/azure-pricing-sandbox"
base=$(docker image inspect --format '{{.Id}}' maf-pricing-base:qualification)
docker build --target native --build-arg BASE_IMAGE=maf-pricing-base:qualification -t maf-pricing-native:qualification \
    -f "$repo/images/azure-pricing-sandbox/native.Dockerfile" "$repo/images/azure-pricing-sandbox"
docker build --target bubblewrap --build-arg BASE_IMAGE=maf-pricing-base:qualification -t maf-pricing-bubblewrap:qualification \
    -f "$repo/images/azure-pricing-sandbox/native.Dockerfile" "$repo/images/azure-pricing-sandbox"
runtime=$(docker image inspect --format '{{.Id}}' maf-pricing-bubblewrap:qualification)
template=$(docker image inspect --format '{{.Id}}' maf-pricing-native:qualification)
container=$(docker create "$runtime")
docker export "$container" > "$target/runtime.tar"
tar --no-same-owner -xf "$target/runtime.tar" -C "$target/runtime"
docker rm "$container"
container=
sha256sum "$target/runtime.tar" > "$target/runtime.sha256"
rm "$target/runtime.tar"
docker save -o "$target/template.tar" maf-pricing-native:qualification
sha256sum "$target/template.tar" > "$target/template.sha256"
docker build --target build -t maf-native-proxy:qualification \
    "$repo/packages/maf-sandbox-docker/src/maf_sandbox_docker/_proxy"
proxy=$(docker image inspect --format '{{.Id}}' maf-native-proxy:qualification)
container=$(docker create "$proxy")
docker cp "$container:/iron-proxy" "$target/native/iron-proxy"
docker cp "$container:/src/LICENSE" "$target/native/iron-proxy-LICENSE"
docker cp "$container:/iron-proxy-build-info.txt" "$target/native/build-info.txt"
docker rm "$container"
container=
chmod 0555 "$target/native/iron-proxy"
chmod -R a-w "$target/runtime"
sha256sum "$target/native/iron-proxy" > "$target/proxy.sha256"
printf '%s\n' "$runtime" > "$target/runtime.id"
printf '%s\n' "$template" > "$target/template.id"
printf '%s\n' "$base" > "$target/base.id"
printf '%s\n' "$proxy" > "$target/proxy-build.id"
echo "Pricing runtime and sbx template ready in $target"
