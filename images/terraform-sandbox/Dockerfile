# The initial supported guest platform is Linux amd64. Build each engine separately.
ARG ENGINE=terraform
FROM golang:1.27.2-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS opentofu-build
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
RUN apk add --no-cache git python3
COPY image.json build_opentofu.py build_go.py /recipe/
RUN python3 /recipe/build_opentofu.py

FROM golang:1.27.2-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS terraform-build
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
RUN apk add --no-cache git python3
COPY image.json build_terraform.py build_go.py /recipe/
RUN python3 /recipe/build_terraform.py && go clean -cache -modcache

FROM cgr.dev/chainguard/wolfi-base:latest@sha256:0b179206ab162b12a43bacea1b525a4023e0272005c92d08555c61b1beaedc40 AS runtime
RUN --mount=type=bind,from=python-packages,target=/python-packages \
    cp /python-packages/maf-python.rsa.pub /etc/apk/keys/ \
    && apk add --no-cache /python-packages/x86_64/python-3.13-3.13.18_git20261030-r0.apk /python-packages/x86_64/python-3.13-base-3.13.18_git20261030-r0.apk \
    && rm /etc/apk/keys/maf-python.rsa.pub \
    && python3 -c "import sys, ssl; assert sys.version_info[:3] == (3, 13, 18); assert ssl.HAS_PSK"
RUN apk upgrade --no-cache && apk add --no-cache python-3.13 ca-certificates-bundle \
       'libbrotlicommon1>=1.2.0-r7' 'libbrotlidec1>=1.2.0-r7' 'libbrotlienc1>=1.2.0-r7' \
    && mkdir -p /usr/local/bin \
    && ln -s /usr/bin/python3 /usr/local/bin/python3
ARG TARGETARCH
ARG ENGINE=terraform
ARG PROFILE=builtin
ARG ENGINE_VERSION
ARG PROVIDER_MANIFEST=image.json
COPY image.json /opt/maf-terraform/
COPY ${PROVIDER_MANIFEST} /opt/maf-terraform/
COPY install.py /opt/maf-terraform/install.py

FROM runtime AS terraform-image
COPY --from=terraform-build /out/ /opt/maf-terraform/source-build/
RUN test "$TARGETARCH" = amd64 \
    && python3 -I /opt/maf-terraform/install.py "$ENGINE" "$PROFILE" "$ENGINE_VERSION" /opt/maf-terraform/source-build \
    && rm /opt/maf-terraform/source-build/terraform /opt/maf-terraform/install.py

FROM runtime AS opentofu-image
COPY --from=opentofu-build /out/ /opt/maf-terraform/source-build/
RUN test "$TARGETARCH" = amd64 \
    && python3 -I /opt/maf-terraform/install.py "$ENGINE" "$PROFILE" "$ENGINE_VERSION" /opt/maf-terraform/source-build \
    && rm /opt/maf-terraform/source-build/tofu /opt/maf-terraform/install.py

FROM ${ENGINE}-image AS assembled
COPY runner.py /opt/maf-terraform/runner.py

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
ARG ENGINE=terraform
ARG ENGINE_VERSION
LABEL org.opencontainers.image.version="${ENGINE_VERSION}" \
      ai.sokol.maf.engine="${ENGINE}" \
      ai.sokol.maf.engine.version="${ENGINE_VERSION}"
# docker exec invokes the launcher; the image entrypoint does not accept workload arguments.
CMD ["sleep", "infinity"]
