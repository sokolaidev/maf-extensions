FROM cgr.dev/chainguard/wolfi-base:latest@sha256:0b179206ab162b12a43bacea1b525a4023e0272005c92d08555c61b1beaedc40 AS assembled

RUN apk upgrade --no-cache \
    && apk add --no-cache python-3.13 graphviz ttf-dejavu \
       'libbrotlicommon1>=1.2.0-r7' 'libbrotlidec1>=1.2.0-r7' 'libbrotlienc1>=1.2.0-r7' \
    && dot -c

COPY DEJAVU-LICENSE /usr/share/licenses/drawio/DEJAVU-LICENSE

FROM assembled AS flattened
# Read the image snapshot rather than the build container's runtime device mounts.
RUN --mount=type=bind,from=assembled,target=/source \
    set -o pipefail && mkdir /rootfs \
    && tar -C /source --exclude='./dev/*' -cf - . | tar -C /rootfs -xf -

FROM scratch
# ACAS supplies runtime devices and rejects device nodes embedded in image layers.
COPY --from=flattened /rootfs/ /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
LABEL org.opencontainers.image.description="Editable Draw.io authoring runtime with Python and Graphviz for Linux/amd64. Usage, verification and support: https://github.com/sokolaidev/maf-extensions/blob/main/images/drawio-sandbox/README.md"
CMD ["sleep", "infinity"]
