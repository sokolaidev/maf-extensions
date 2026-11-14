FROM ubuntu:26.04@sha256:6a06111c3b0a8600feadb270aec771dc239bf407b25625f93f44ab00e11f4d3c AS assembled

ADD --checksum=sha256:4d740636b5c96f687798602c5aad6a5a270f44dd02e48db0638ae77aa4867b1b https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_157.0.8103.78-1_amd64.deb /tmp/chrome.deb
COPY requirements.txt mermaid.json install.py /opt/maf-document-export/
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends python3 python3-pip /tmp/chrome.deb fonts-dejavu-core fonts-noto-color-emoji fontconfig \
    && python3 -m pip install --break-system-packages --no-cache-dir --only-binary=:all: --require-hashes \
       -r /opt/maf-document-export/requirements.txt \
    && python3 /opt/maf-document-export/install.py \
    && ln -s /usr/bin/google-chrome-stable /usr/bin/chromium \
    && rm -rf /var/lib/apt/lists/* /tmp/chrome.deb \
    && fc-cache -f \
    && mkdir -p /maf-sandbox/work
COPY render.py /opt/maf-document-export/render.py
COPY DEJAVU-LICENSE NOTO-EMOJI-LICENSE /usr/share/licenses/maf-document-export/

FROM assembled AS flattened
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
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
