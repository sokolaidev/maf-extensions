#!/usr/bin/env bash
# Temporary: can a command bind the workspace inside its own user and mount namespace.
set +e
x() { echo; echo "\$ $*"; "$@"; echo "[exit $?]"; }
cd "$(dirname "$0")"

cat > d_agent.dockerfile <<'EOF'
FROM debian:bookworm-slim
RUN useradd -u 1000 -m -s /bin/bash agent
USER agent
EOF
cat > d_root.dockerfile <<'EOF'
FROM debian:bookworm-slim
EOF
cat > s_graphviz.dockerfile <<'EOF'
FROM docker/sandbox-templates:shell
USER root
RUN apt-get update && apt-get install -y --no-install-recommends graphviz && rm -rf /var/lib/apt/lists/*
USER agent
EOF
cat > az_agent.dockerfile <<'EOF'
FROM mcr.microsoft.com/azurelinux/base/core:3.0
RUN tdnf install -y bash shadow-utils util-linux && tdnf clean all && useradd -u 1000 -m -s /bin/bash agent
USER agent
EOF

# $1 is the guest workspace mount (pwd -P at create).
NS='ws=$1; unshare --version | head -1; cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null; cat /proc/sys/user/max_user_namespaces
unshare --user --mount --map-current-user sh -c "mount --bind \"$ws\" /maf-sandbox && cd /maf-sandbox && pwd && /bin/pwd && id && echo from-ns > made-in-ns && ls -ln" && echo NS_OK
unshare --user --mount --map-root-user sh -c "mount --bind \"$ws\" /maf-sandbox && echo NS_ROOTMAP_OK"
ls -ln "$ws"; echo "outside ns /maf-sandbox:"; ls -la /maf-sandbox'

run() {
  local name=$1
  mount=$(sbx exec "$name" sh -c 'pwd -P')
  echo "guest mount: $mount"
  x sbx exec -u root "$name" sh -c 'mkdir -p /maf-sandbox && chmod 0755 /maf-sandbox && ls -ld /maf-sandbox'
  x sbx exec "$name" sh -c "$NS" ns "$mount"
  x sbx exec -u root "$name" sh -c "$NS" ns "$mount"
  echo "host side:"; ls -ln "$2"
}

d=$(mktemp -d)
x sbx create shell --name probe-shell --cpus 1 --memory 1g --skills off --deny-network '**' --quiet "$d"
run probe-shell "$d"
x sbx rm --force probe-shell

for f in d_agent d_root s_graphviz az_agent; do
  echo; echo "=================== $f"
  x docker build -q -t "probe-$f:local" -f "$f.dockerfile" .
  docker save -o "/tmp/$f.tar" "probe-$f:local" && sbx template load "/tmp/$f.tar" >/dev/null
  name="probe-${f//_/-}"
  d=$(mktemp -d)
  x sbx create shell --name "$name" --template "probe-$f:local" --cpus 1 --memory 1g --skills off --deny-network '**' --quiet "$d"
  run "$name" "$d"
  x sbx rm --force "$name"
done
