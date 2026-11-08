FROM docker.io/sbx/shell:1.1.0@sha256:32c458ddd69a0a6ad67c9f08d436d9814f3a8cb374b84c2a6acf4aa4f1630688
USER root
# Debian packages mount separately from the util-linux tools used by the exec wrapper.
RUN apt-get update \
    && apt-get install -y --no-install-recommends mount \
    && rm -rf /var/lib/apt/lists/*
USER 1000
RUN test "$(id -u)" = 1000 \
    && test ! -e /maf-sandbox \
    && for command in sh bash base64 setsid mount unshare mkdir cat rm sleep python3 curl ps; do \
         command -v "$command" >/dev/null || exit 1; \
       done \
    && unshare --map-user=0 --map-group=0 --help >/dev/null
