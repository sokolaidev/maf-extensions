FROM cgr.dev/chainguard/wolfi-base:latest@sha256:360896efb0753a5701c48f860e17130c205eae5a264797aa3e7ea126ee2923a7 AS assembled

RUN apk upgrade --no-cache \
    && apk add --no-cache graphviz ttf-dejavu bash coreutils util-linux-misc mount \
       'libbrotlicommon1>=1.2.0-r7' 'libbrotlidec1>=1.2.0-r7' 'libbrotlienc1>=1.2.0-r7' \
    && dot -c \
    && for tool in sh bash base64 setsid mount unshare mkdir cat rm sleep; do \
         command -v "$tool" >/dev/null || exit 1; \
       done \
    && unshare --help | grep -q -- --map-user \
    && unshare --help | grep -q -- --map-group

FROM scratch
# ACAS supplies runtime devices and rejects device nodes embedded in image layers.
COPY --from=assembled --exclude=dev/* / /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
LABEL org.opencontainers.image.description="Graphviz DOT-to-PNG renderer for Linux/amd64. Usage, verification and support: https://github.com/sokolaidev/maf-extensions/blob/main/images/graphviz-sandbox/README.md"
CMD ["/bin/sh"]
