ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS native
USER 0
RUN apk add --no-cache bash python3 util-linux util-linux-misc mount coreutils ca-certificates curl socat \
    && command -v mount && command -v unshare && command -v setsid \
    && command -v bash && command -v base64 && command -v python3 \
    && command -v update-ca-certificates && command -v socat
RUN mkdir -p /run /tmp
RUN addgroup -g 1000 agent && adduser -D -u 1000 -G agent -h /home/agent agent
ENV HOME=/home/agent
USER agent

FROM native AS bubblewrap
USER 0
RUN mkdir -p /maf-sandbox
