FROM cgr.dev/chainguard/wolfi-base:latest@sha256:24f549a7fcace88e69b9557941168c74645688ecc4acf7fb6d734a6b74e9019d AS assembled
RUN --mount=type=bind,from=python-packages,target=/python-packages \
    cp /python-packages/maf-python.rsa.pub /etc/apk/keys/ \
    && apk add --no-cache /python-packages/x86_64/python-3.13-3.13.18_git20261030-r0.apk /python-packages/x86_64/python-3.13-base-3.13.18_git20261030-r0.apk \
    && rm /etc/apk/keys/maf-python.rsa.pub \
    && python3 -c "import sys, ssl; assert sys.version_info[:3] == (3, 13, 18); assert ssl.HAS_PSK"

COPY requirements.txt mermaid.json install.py /opt/maf-document-export/
RUN apk upgrade --no-cache \
    && apk add --no-cache python-3.13 chromium ttf-dejavu font-noto-emoji fontconfig \
    && apk add --no-cache --virtual .install py3.13-pip \
    && python3 -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
       --target /usr/lib/python3.13/site-packages -r /opt/maf-document-export/requirements.txt \
    && python3 /opt/maf-document-export/install.py \
    && apk del --no-cache .install \
    && fc-cache -f \
    && mkdir -p /maf-sandbox/work
COPY render.py /opt/maf-document-export/render.py
COPY DEJAVU-LICENSE NOTO-EMOJI-LICENSE /usr/share/licenses/maf-document-export/

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
LABEL org.opencontainers.image.description="Offline Markdown to PDF with Mermaid and passive SVG assets. Usage and limits: https://github.com/sokolaidev/maf-extensions/blob/main/images/document-export/README.md"
USER 0
WORKDIR /maf-sandbox/work
CMD ["sleep", "infinity"]
