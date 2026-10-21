FROM cgr.dev/chainguard/wolfi-base@sha256:9c2092b053779e14c82fb50f77b37bcc38b7d2c83972352d5813280f9d035b03

LABEL org.opencontainers.image.description="Editable Draw.io and native PNG, SVG and JPEG export for Linux/amd64. Usage, verification and sandbox requirements: https://github.com/sokolaidev/maf-extensions/blob/main/images/drawio-export/README.md"

ARG DRAWIO_VERSION=32.4.1
ARG DRAWIO_SHA256=f4455b3bba3d3b539c3cd069218fc64f0462b1b01610fe8d11acc3ca8566f32b
COPY requirements.txt /opt/maf-drawio/requirements.txt
RUN apk upgrade --no-cache \
    && apk add --no-cache ca-certificates-bundle python-3.13 \
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
ENV DRAWIO_DISABLE_UPDATE=true
USER root
WORKDIR /maf-sandbox/work
CMD ["sleep", "infinity"]
