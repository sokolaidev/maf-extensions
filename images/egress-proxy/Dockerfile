ARG BASE_IMAGE=maf-image-scan:proxy-base
FROM ${BASE_IMAGE}
LABEL org.opencontainers.image.title="MAF egress proxy" \
      org.opencontainers.image.description="Patched iron-proxy for Docker and WSLC sandbox egress" \
      org.opencontainers.image.documentation="https://github.com/sokolaidev/maf-extensions/blob/main/images/egress-proxy/README.md"
COPY --from=suite LICENSE /licenses/maf-extensions-LICENSE
COPY --from=proxy iron.patch credential_gate.go /usr/share/maf-egress-proxy/
