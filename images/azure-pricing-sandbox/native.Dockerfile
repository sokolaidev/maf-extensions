ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS native
USER 0
RUN apk add --no-cache bash python3 util-linux util-linux-misc mount coreutils ca-certificates curl socat \
    && command -v mount && command -v unshare && command -v setsid \
    && command -v bash && command -v base64 && command -v python3 \
    && command -v update-ca-certificates && command -v socat
RUN mkdir -p /run /tmp

FROM native AS bubblewrap
RUN mkdir -p /maf-sandbox
