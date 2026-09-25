#!/bin/sh
set -eu
umask 077
: "${MAF_SANDBOX_CONFIG_B64:?missing proxy policy}"
: "${MAF_SANDBOX_TUNNEL_SUBNETS:?missing sandbox network}"
# Bind the tunnel to the sandbox network only, found by subnet: a restart renumbers interfaces.
listen=
for subnet in $MAF_SANDBOX_TUNNEL_SUBNETS; do
    listen=$(ip -4 route show "$subnet" | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1)
    [ -z "$listen" ] || break
done
if [ -z "$listen" ]; then
    echo "no address on the sandbox network $MAF_SANDBOX_TUNNEL_SUBNETS" >&2
    exit 1
fi
export IRON_PROXY_TUNNEL_LISTEN="$listen:3128"
od -An -N24 -tx1 /dev/urandom | tr -d ' \n' > /run/maf-proxy/boot
iron-proxy generate-ca --outdir /run/maf-proxy > /dev/null
printf '%s' "$MAF_SANDBOX_CONFIG_B64" | base64 -d > /run/maf-proxy/config.yaml
unset MAF_SANDBOX_CONFIG_B64
exec iron-proxy -config /run/maf-proxy/config.yaml 2>&1
