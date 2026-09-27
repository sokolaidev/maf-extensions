#!/usr/bin/env bash
# Temporary: does an image built FROM Docker's shell template keep the capabilities sbx grants.
set +e
x() { echo; echo "\$ $*"; "$@"; echo "[exit $?]"; }
cd "$(dirname "$0")"

x docker pull -q docker/sandbox-templates:shell
docker inspect docker/sandbox-templates:shell --format '{{json .Config.Labels}} user={{.Config.User}} entry={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}}'
x docker build -q -t bicep-sandbox:local ../images/bicep-sandbox

cat > s_graphviz.dockerfile <<'EOF'
FROM docker/sandbox-templates:shell
USER root
RUN apt-get update && apt-get install -y --no-install-recommends graphviz && rm -rf /var/lib/apt/lists/*
USER agent
EOF
cat > s_bicep.dockerfile <<'EOF'
FROM docker/sandbox-templates:shell
USER root
RUN apt-get update && apt-get install -y --no-install-recommends libicu74 && rm -rf /var/lib/apt/lists/*
COPY --from=bicep-sandbox:local /usr/local/bin/bicep /usr/local/bin/bicep
USER agent
EOF
cat > s_rootuser.dockerfile <<'EOF'
FROM docker/sandbox-templates:shell
USER root
EOF
labels=$(docker inspect docker/sandbox-templates:shell --format '{{range $k, $v := .Config.Labels}}--label {{$k}}={{$v}} {{end}}')
cat > d_labels.dockerfile <<'EOF'
FROM debian:bookworm-slim
RUN useradd -u 1000 -m -s /bin/bash agent
USER agent
EOF

S='id; grep -E "^CapEff" /proc/self/status; mkdir -p /tmp/probe-src /tmp/probe-dst && mount --bind /tmp/probe-src /tmp/probe-dst && echo MOUNT_OK; command -v dot bicep; bicep --version 2>&1 | head -1'

for f in s_graphviz s_bicep s_rootuser d_labels; do
  echo; echo "=================== $f"
  if [ "$f" = d_labels ]; then
    # shellcheck disable=SC2086
    x docker build -q $labels -t "probe-$f:local" -f "$f.dockerfile" .
  else
    x docker build -q -t "probe-$f:local" -f "$f.dockerfile" .
  fi
  x docker save -o "/tmp/$f.tar" "probe-$f:local"
  x sbx template load "/tmp/$f.tar"
  name="probe-${f//_/-}"
  x sbx create shell --name "$name" --template "probe-$f:local" --cpus 1 --memory 1g --skills off --deny-network '**' --quiet "$(mktemp -d)"
  x sbx exec -u root "$name" sh -c "$S"
  x sbx exec "$name" sh -c "$S"
  x sbx rm --force "$name"
  x uv run python _backend_probe.py "probe-$f:local" 'id; pwd; cat in.txt; echo; command -v dot bicep'
done
