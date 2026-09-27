#!/usr/bin/env bash
# Temporary: which image properties decide whether sbx lets root mount in a sandbox.
set +e
x() { echo; echo "\$ $*"; "$@"; echo "[exit $?]"; }
cd "$(dirname "$0")"

cat > d_root.dockerfile <<'EOF'
FROM debian:bookworm-slim
EOF
cat > d_agent.dockerfile <<'EOF'
FROM debian:bookworm-slim
RUN useradd -u 1000 -m -s /bin/bash agent
USER agent
EOF
cat > d_agent_rootuser.dockerfile <<'EOF'
FROM debian:bookworm-slim
RUN useradd -u 1000 -m -s /bin/bash agent
EOF
cat > d_agent_sudo.dockerfile <<'EOF'
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends sudo && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -m -s /bin/bash agent && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent
USER agent
EOF
cat > az_plain.dockerfile <<'EOF'
FROM mcr.microsoft.com/azurelinux/base/core:3.0
EOF
cat > az_agent.dockerfile <<'EOF'
FROM mcr.microsoft.com/azurelinux/base/core:3.0
RUN tdnf install -y bash shadow-utils util-linux && tdnf clean all && useradd -u 1000 -m -s /bin/bash agent
USER agent
EOF

S='id; grep -E "^Cap(Eff|Bnd)" /proc/self/status; ls -l /bin/sh /bin/bash 2>&1; mkdir -p /tmp/probe-src /tmp/probe-dst && mount --bind /tmp/probe-src /tmp/probe-dst && echo MOUNT_OK; mkdir -p /maf-probe && echo MKDIR_ROOT_OK'

x sbx create shell --name probe-shell --cpus 1 --memory 1g --skills off --deny-network '**' --quiet "$(mktemp -d)"
x sbx exec probe-shell sh -c "$S"
x sbx exec -u root probe-shell sh -c "$S"
x sbx rm --force probe-shell

for f in *.dockerfile; do
  n="${f%.dockerfile}"
  echo; echo "=================== $n"
  x docker build -q -t "probe-$n:local" -f "$f" .
  x docker save -o "/tmp/$n.tar" "probe-$n:local"
  x sbx template load "/tmp/$n.tar"
  name="probe-${n//_/-}"
  x sbx create shell --name "$name" --template "probe-$n:local" --cpus 1 --memory 1g --skills off --deny-network '**' --quiet "$(mktemp -d)"
  x sbx exec "$name" sh -c "$S"
  x sbx exec -u root "$name" sh -c "$S"
  x sbx rm --force "$name"
done
