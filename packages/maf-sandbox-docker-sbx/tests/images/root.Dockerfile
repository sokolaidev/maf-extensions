# A template that runs its commands as root, as an image with no USER does.
FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl procps python3 \
    && rm -rf /var/lib/apt/lists/*
