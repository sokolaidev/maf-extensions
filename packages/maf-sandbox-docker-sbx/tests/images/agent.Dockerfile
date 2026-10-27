# A template that is not Docker's: the shape Docker's docs ask for, with an `agent` user.
# `sbx` grants it no mount capability, which the backend's per-command namespace works around.
FROM debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl procps python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -m -s /bin/bash agent
USER agent
