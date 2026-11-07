ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS native
USER 0
RUN apk add --no-cache bash python3 util-linux coreutils
RUN mkdir -p /run /tmp

FROM native AS bubblewrap
RUN mkdir -p /maf-sandbox
