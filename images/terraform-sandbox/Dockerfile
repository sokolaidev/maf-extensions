# The initial supported guest platform is Linux amd64. Build each engine separately.
ARG BASE_IMAGE=scratch
ARG ENGINE=terraform
FROM golang:1.27.2-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS opentofu-build
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
RUN apk add --no-cache git python3
COPY image.json build_opentofu.py /recipe/
RUN python3 /recipe/build_opentofu.py

FROM golang:1.27.2-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS terraform-build
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
RUN apk add --no-cache git python3
COPY image.json build_terraform.py /recipe/
RUN python3 /recipe/build_terraform.py && go clean -cache -modcache

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
COPY --from=terraform-build /out/ /opt/maf-terraform/source-build/
RUN test "$TARGETARCH" = amd64 \
    && python3 -I /opt/maf-terraform/install.py "$ENGINE" "$PROFILE" "$ENGINE_VERSION" /opt/maf-terraform/source-build \
    && rm /opt/maf-terraform/source-build/terraform /opt/maf-terraform/install.py

FROM runtime AS opentofu-image
COPY --from=opentofu-build /out/ /opt/maf-terraform/source-build/
RUN test "$TARGETARCH" = amd64 \
    && python3 -I /opt/maf-terraform/install.py "$ENGINE" "$PROFILE" "$ENGINE_VERSION" /opt/maf-terraform/source-build \
    && rm /opt/maf-terraform/source-build/tofu /opt/maf-terraform/install.py

FROM ${ENGINE}-image
COPY runner.py /opt/maf-terraform/runner.py
# docker exec invokes the launcher; the image entrypoint does not accept workload arguments.
CMD ["sleep", "infinity"]
