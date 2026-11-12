FROM cgr.dev/chainguard/wolfi-base:latest@sha256:94fa13e26d449f7d78dbcd02d12ea37a0da80d24fcc3e1f17b95d515dec607a7 AS assembled
RUN --mount=type=bind,from=python-packages,target=/python-packages \
    cp /python-packages/maf-python.rsa.pub /etc/apk/keys/ \
    && apk add --no-cache /python-packages/x86_64/python-3.13-3.13.18_git20261030-r0.apk /python-packages/x86_64/python-3.13-base-3.13.18_git20261030-r0.apk \
    && rm /etc/apk/keys/maf-python.rsa.pub \
    && python3 -c "import sys, ssl; assert sys.version_info[:3] == (3, 13, 18); assert ssl.HAS_PSK"

COPY requirements.txt /opt/maf-document/requirements.txt
RUN apk upgrade --no-cache \
    && apk add --no-cache python-3.13 libstdc++ \
    && apk add --no-cache --virtual .install py3.13-pip \
    && python3 -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
       --target /usr/lib/python3.13/site-packages -r /opt/maf-document/requirements.txt \
    && apk del --no-cache .install \
    && python3 -c "import pypdfium2, mammoth, pptx, striprtf" \
    && mkdir -p /maf-sandbox/work

FROM assembled AS flattened
# Read the image snapshot rather than the build container's runtime device mounts.
RUN --mount=type=bind,from=assembled,target=/source \
    set -o pipefail && mkdir /rootfs \
    && tar -C /source --exclude='./dev/*' -cf - . | tar -C /rootfs -xf -

FROM scratch AS document
# ACAS supplies runtime devices and rejects device nodes embedded in image layers.
COPY --from=flattened /rootfs/ /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
LABEL org.opencontainers.image.description="Guarded PDF, DOCX, PPTX and RTF conversion runtime for Linux/amd64. Usage and limits: https://github.com/sokolaidev/maf-extensions/blob/main/images/document-sandbox/README.md"
USER 0
CMD ["sleep", "infinity"]
