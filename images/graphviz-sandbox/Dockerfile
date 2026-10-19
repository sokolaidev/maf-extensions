FROM cgr.dev/chainguard/wolfi-base@sha256:9c2092b053779e14c82fb50f77b37bcc38b7d2c83972352d5813280f9d035b03

LABEL org.opencontainers.image.description="Graphviz DOT-to-PNG renderer for Linux/amd64. Usage, verification and support: https://github.com/sokolaidev/maf-extensions/blob/main/images/graphviz-sandbox/README.md"

RUN apk upgrade --no-cache \
    && apk add --no-cache graphviz ttf-dejavu bash coreutils util-linux-misc mount \
    && dot -c \
    && for tool in sh bash base64 setsid mount unshare mkdir cat rm sleep; do \
         command -v "$tool" >/dev/null || exit 1; \
       done \
    && unshare --help | grep -q -- --map-user \
    && unshare --help | grep -q -- --map-group
