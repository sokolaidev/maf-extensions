# The initial supported guest platform is Linux amd64. Build each engine separately.
ARG BASE_IMAGE=scratch
ARG ENGINE=terraform
FROM golang:1.27.1-alpine@sha256:8a5910f31396cd4d89662f56c68b3ae31d374308270a1c3bd96672ee5ed43414 AS opentofu-build
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
RUN apk add --no-cache git python3
COPY image.json build_opentofu.py /recipe/
RUN python3 /recipe/build_opentofu.py

FROM ${BASE_IMAGE} AS runtime
RUN apk upgrade --no-cache && apk add --no-cache python-3.13 ca-certificates-bundle \
    && mkdir -p /usr/local/bin \
    && ln -s /usr/bin/python3 /usr/local/bin/python3
ARG TARGETARCH
ARG ENGINE=terraform
ARG PROFILE=builtin
ARG ENGINE_VERSION
ARG PROVIDER_MANIFEST=image.json
LABEL org.opencontainers.image.version="${ENGINE_VERSION}" \
      ai.sokol.maf.engine="${ENGINE}" \
      ai.sokol.maf.engine.version="${ENGINE_VERSION}"
COPY image.json /opt/maf-terraform/
COPY ${PROVIDER_MANIFEST} /opt/maf-terraform/
COPY install.py /opt/maf-terraform/install.py

FROM runtime AS terraform-image
RUN test "$TARGETARCH" = amd64 \
    && python3 -I /opt/maf-terraform/install.py "$ENGINE" "$PROFILE" "$ENGINE_VERSION" \
    && rm /opt/maf-terraform/install.py

FROM runtime AS opentofu-image
COPY --from=opentofu-build /out/ /opt/maf-terraform/source-build/
RUN test "$TARGETARCH" = amd64 \
    && python3 -I /opt/maf-terraform/install.py "$ENGINE" "$PROFILE" "$ENGINE_VERSION" /opt/maf-terraform/source-build \
    && rm /opt/maf-terraform/source-build/tofu /opt/maf-terraform/install.py

FROM ${ENGINE}-image
COPY runner.py /opt/maf-terraform/runner.py
# docker exec invokes the launcher; the image entrypoint does not accept workload arguments.
CMD ["sleep", "infinity"]
