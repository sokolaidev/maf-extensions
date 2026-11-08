#!/bin/bash
# Run inside a dedicated systemd service with User= and Delegate=cpu memory pids.
set -euo pipefail
test "$#" -ge 3 || { echo 'Usage: run-bubblewrap.sh RUNTIME_DIRECTORY PYTHON SOURCE_SHA [agent options]' >&2; exit 1; }
artifacts=$(realpath -- "$1")
python="$2"
source_sha="$3"
shift 3
repo=$(cd -- "$(dirname -- "$0")/../../.." && pwd)
group="/sys/fs/cgroup$(awk -F: '$1 == "0" { print $3 }' /proc/self/cgroup)"
mkdir "$group/host"
echo "$$" > "$group/host/cgroup.procs"
echo '+cpu +memory +pids' > "$group/cgroup.subtree_control"
state=$(mktemp -d)
cleanup() {
    status=$?
    if ! compgen -G "$state/*.json" > /dev/null; then
        find "$state" -maxdepth 1 -type f -name '*.lock' -delete
    fi
    if ! rmdir "$state"; then
        echo 'Incomplete sandbox state cleanup.' >&2
        exit 1
    fi
    exit "$status"
}
trap cleanup EXIT
"$python" "$repo/samples/experimental/azure_pricing/agent.py" \
    --backend bubblewrap --runtime "$artifacts/runtime" --state "$state" --cgroup "$group" \
    --image "$(cat "$artifacts/runtime.id")" --proxy "$artifacts/native/iron-proxy" \
    --proxy-sha256 "$(cut -d ' ' -f 1 "$artifacts/proxy.sha256")" \
    --source "$source_sha" "$@"
