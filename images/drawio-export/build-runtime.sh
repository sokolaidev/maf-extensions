#!/bin/bash
# Build the native Linux runtime directory without Docker or an OCI image.
set -euo pipefail
test "$(id -u)" = 0 || { echo 'Run provisioning as root.' >&2; exit 1; }
test "$#" = 1 || { echo 'Usage: build-runtime.sh ABSOLUTE_NEW_DIRECTORY' >&2; exit 1; }
case "$1" in /*) ;; *) echo 'Destination must be absolute.' >&2; exit 1;; esac
target=$(realpath -m -- "$1")
case "$target" in /|/usr|/opt|/var|/home|/tmp) echo 'Choose a new dedicated directory.' >&2; exit 1;; esac
test ! -e "$target" || { echo 'Destination must not exist.' >&2; exit 1; }
sources=$(cd -- "$(dirname -- "$0")" && pwd)
debootstrap --variant=minbase --arch=amd64 bookworm "$target" https://deb.debian.org/debian
chroot "$target" /bin/sh -ec 'apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-pil graphviz xvfb xauth fonts-dejavu-core fonts-dejavu-extra libgbm1 libasound2 ca-certificates curl'
chroot "$target" curl -fL --retry 3 -o /tmp/drawio.deb https://github.com/jgraph/drawio-desktop/releases/download/v32.4.1/drawio-amd64-32.4.1.deb
echo 'f4455b3bba3d3b539c3cd069218fc64f0462b1b01610fe8d11acc3ca8566f32b  tmp/drawio.deb' | (cd "$target" && sha256sum -c -)
chroot "$target" /bin/sh -ec 'DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends /tmp/drawio.deb; rm /tmp/drawio.deb; apt-get clean'
install -d "$target/opt/maf-drawio" "$target/maf-sandbox/work"
install -m 644 "$sources/install.py" "$sources/export.py" "$sources/guard.js" "$sources/check-assets.py" "$target/opt/maf-drawio/"
install -D -m 644 "$sources/DEJAVU-LICENSE" "$target/usr/share/licenses/drawio/DEJAVU-LICENSE"
chroot "$target" python3 /opt/maf-drawio/install.py
chroot "$target" python3 /opt/maf-drawio/check-assets.py
echo "Runtime ready: $target"
