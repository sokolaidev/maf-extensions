#!/bin/sh
set -eu

melange keygen /tmp/maf-python.rsa
set --
for key in /etc/apk/keys/*.pub; do
    set -- "$@" --keyring-append "$key"
done
melange build python-3.13.yaml --arch x86_64 --runner bubblewrap \
    --namespace maf-extensions --license Apache-2.0 \
    --source-dir python-3.13 --pipeline-dir pipelines \
    --env-file build-x86_64.env --env-file build-common.env \
    --repository-append https://apk.cgr.dev/chainguard \
    --signing-key /tmp/maf-python.rsa --out-dir /out "$@"
cp /tmp/maf-python.rsa.pub /out/
cp python-3.13.yaml /out/
