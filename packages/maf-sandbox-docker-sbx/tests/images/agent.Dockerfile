# A template that is not Docker's: the shape Docker's docs ask for, with an `agent` user.
# `sbx` grants it no mount capability, which the backend's per-command namespace works around.
FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl procps python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -m -s /bin/bash agent
USER agent
