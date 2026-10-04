FROM debian:bookworm-slim

ARG DRAWIO_VERSION=31.7.0
ARG DRAWIO_SHA256=eb9695e208fcc5ccfbfc496aa8ab2f52a273297d83715de2177b231c172c13de
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl python3 python3-pil \
       graphviz xvfb xauth fonts-dejavu-core libgbm1 libasound2 \
    && curl --fail --location --retry 3 \
       "https://github.com/jgraph/drawio-desktop/releases/download/v${DRAWIO_VERSION}/drawio-amd64-${DRAWIO_VERSION}.deb" \
       --output /tmp/drawio.deb \
    && echo "${DRAWIO_SHA256}  /tmp/drawio.deb" | sha256sum --check - \
    && apt-get install -y --no-install-recommends /tmp/drawio.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/drawio.deb

RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-extra \
    && rm -rf /var/lib/apt/lists/*

COPY install.py export.py guard.js /opt/maf-drawio/
RUN python3 /opt/maf-drawio/install.py \
    && groupadd --gid 10001 renderer \
    && useradd --create-home --uid 10001 --gid 10001 renderer \
    && mkdir -p /maf-sandbox/work \
    && chown renderer:renderer /maf-sandbox/work
ENV DRAWIO_DISABLE_UPDATE=true
USER root
WORKDIR /maf-sandbox/work
CMD ["sleep", "infinity"]
