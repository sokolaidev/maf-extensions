# The initial supported guest platform is Linux amd64. Build each engine separately.
ARG BASE_IMAGE=scratch
FROM ${BASE_IMAGE}
ARG TARGETARCH
ARG ENGINE=terraform
ARG PROFILE=builtin
ARG ENGINE_VERSION
LABEL org.opencontainers.image.version="${ENGINE_VERSION}" \
      ai.sokol.maf.engine="${ENGINE}" \
      ai.sokol.maf.engine.version="${ENGINE_VERSION}"
COPY build.json dependencies.terraform.json dependencies.opentofu.json /opt/maf-terraform/
COPY install.py /opt/maf-terraform/install.py
RUN test "$TARGETARCH" = amd64 \
    && python3 -I /opt/maf-terraform/install.py "$ENGINE" "$PROFILE" "$ENGINE_VERSION" \
    && rm /opt/maf-terraform/install.py
COPY runner.py /opt/maf-terraform/runner.py
# docker exec invokes the launcher; the image entrypoint does not accept workload arguments.
CMD ["sleep", "infinity"]
