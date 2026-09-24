#!/bin/sh
set -eu
umask 077
: "${MAF_SANDBOX_CONFIG_B64:?missing proxy policy}"
od -An -N24 -tx1 /dev/urandom | tr -d ' \n' > /run/maf-proxy/boot
iron-proxy generate-ca --outdir /run/maf-proxy > /dev/null
printf '%s' "$MAF_SANDBOX_CONFIG_B64" | base64 -d > /run/maf-proxy/config.yaml
unset MAF_SANDBOX_CONFIG_B64
exec iron-proxy -config /run/maf-proxy/config.yaml 2>&1
