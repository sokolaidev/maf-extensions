#!/bin/sh
set -eu
umask 077
: "${MAF_SANDBOX_CONFIG_B64:?missing proxy policy}"
iron-proxy generate-ca --outdir /run/maf-proxy > /dev/null
printf '%s' "$MAF_SANDBOX_CONFIG_B64" | base64 -d > /run/maf-proxy/config.yaml
unset MAF_SANDBOX_CONFIG_B64
exec iron-proxy -config /run/maf-proxy/config.yaml 2>&1
