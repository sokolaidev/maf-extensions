# A template that is not Docker's: the shape Docker's docs ask for, with an `agent` user.
# `sbx` grants it no mount capability, which the backend's per-command namespace works around.
FROM debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl procps python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -m -s /bin/bash agent
USER agent
