FROM cgr.dev/chainguard/wolfi-base:latest@sha256:360896efb0753a5701c48f860e17130c205eae5a264797aa3e7ea126ee2923a7 AS assembled

ARG DRAWIO_VERSION=32.6.1
ARG DRAWIO_SHA256=515a975fbdb982e0d736b6f99248dbbb856a8b07bb72d6d3648a54b850fa33db
COPY requirements.txt /opt/maf-drawio/requirements.txt
RUN apk upgrade --no-cache \
    && apk add --no-cache ca-certificates-bundle python-3.13 \
       'libbrotlicommon1>=1.2.0-r7' 'libbrotlidec1>=1.2.0-r7' 'libbrotlienc1>=1.2.0-r7' \
       graphviz xvfb-run xauth xkbcomp xkeyboard-config font-misc ttf-dejavu \
       mesa-gbm alsa-lib gtk-3 libnss cups-libs libudev \
    && apk add --no-cache --virtual .unpack curl dpkg py3.13-pip \
    && curl --fail --location --retry 3 \
       "https://github.com/jgraph/drawio-desktop/releases/download/v${DRAWIO_VERSION}/drawio-amd64-${DRAWIO_VERSION}.deb" \
       --output /tmp/drawio.deb \
    && echo "${DRAWIO_SHA256}  /tmp/drawio.deb" | sha256sum --check - \
    && dpkg-deb --extract /tmp/drawio.deb /tmp/drawio-unpacked \
    && mkdir -p /opt \
    && cp -a /tmp/drawio-unpacked/opt/drawio /opt/drawio \
    && rm -rf /tmp/drawio-unpacked /tmp/drawio.deb \
    && python3 -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
       --target /usr/lib/python3.13/site-packages -r /opt/maf-drawio/requirements.txt \
    && apk del --no-cache .unpack \
    && dot -c

COPY DEJAVU-LICENSE /usr/share/licenses/drawio/DEJAVU-LICENSE

COPY install.py export.py guard.js check-assets.py /opt/maf-drawio/
RUN python3 /opt/maf-drawio/install.py \
    && python3 /opt/maf-drawio/check-assets.py \
    && addgroup -g 10001 renderer \
    && adduser -D -u 10001 -G renderer renderer \
    && mkdir -p /maf-sandbox/work /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix
FROM assembled AS flattened
# Read the image snapshot rather than the build container's runtime device mounts.
RUN --mount=type=bind,from=assembled,target=/source \
    set -o pipefail && mkdir /rootfs \
    && tar -C /source --exclude='./dev/*' -cf - . | tar -C /rootfs -xf -

FROM scratch
# ACAS supplies runtime devices and rejects device nodes embedded in image layers.
COPY --from=flattened /rootfs/ /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt DRAWIO_DISABLE_UPDATE=true
LABEL org.opencontainers.image.description="Editable Draw.io and native PNG, SVG and JPEG export for Linux/amd64. Usage, verification and sandbox requirements: https://github.com/sokolaidev/maf-extensions/blob/main/images/drawio-export/README.md"
USER root
WORKDIR /maf-sandbox/work
CMD ["sleep", "infinity"]
