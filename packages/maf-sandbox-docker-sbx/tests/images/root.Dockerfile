# A template that runs its commands as root, as an image with no USER does.
FROM debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl procps python3 \
    && rm -rf /var/lib/apt/lists/*
