FROM python:3.13-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS download
COPY image.json install.py /recipe/
RUN python3 /recipe/install.py

FROM cgr.dev/chainguard/wolfi-base:latest@sha256:0b179206ab162b12a43bacea1b525a4023e0272005c92d08555c61b1beaedc40 AS runtime
RUN apk upgrade --no-cache && apk add --no-cache ca-certificates-bundle icu libssl3 libcrypto3 libstdc++ zlib
COPY --from=download /out/azmcp /usr/local/bin/azmcp
COPY --from=download /out/LICENSE /usr/share/licenses/azure-mcp/LICENSE
COPY image.json /usr/share/azure-pricing/image.json

FROM runtime AS flattened
RUN --mount=type=bind,from=runtime,target=/source \
    set -o pipefail && mkdir /rootfs \
    && tar -C /source --exclude='./dev/*' -cf - . | tar -C /rootfs -xf -

FROM scratch
COPY --from=flattened /rootfs/ /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 AZURE_MCP_COLLECT_TELEMETRY=false
USER 0
CMD ["sleep", "infinity"]
