# A template that runs its commands as root, as an image with no USER does.
FROM debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl procps python3 \
    && rm -rf /var/lib/apt/lists/*
