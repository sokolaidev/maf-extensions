# Build the base from Dockerfile, then use the repository root as this build context.
ARG BASE_IMAGE=scratch
FROM ${BASE_IMAGE} AS prepare
RUN tdnf install -y python3 && tdnf clean all
COPY scripts/bicep_dependencies.py /src/scripts/bicep_dependencies.py
COPY images/bicep-sandbox/dependencies.bicep-avm*.json /src/images/bicep-sandbox/
RUN python3 /src/scripts/bicep_dependencies.py prepare
RUN --network=none python3 /src/scripts/bicep_dependencies.py verify-offline

FROM ${BASE_IMAGE}
ARG MANIFEST_SHA256
LABEL org.maf-sandbox.bicep.manifest-sha256=${MANIFEST_SHA256}
COPY --from=prepare /prepared/cache/ /opt/maf-bicep/cache/
COPY --from=prepare /prepared/receipt.json /opt/maf-bicep/dependencies.json
COPY images/bicep-sandbox/prepared.bicepconfig.json /maf-sandbox/work/bicepconfig.json
RUN --network=none test -n "$MANIFEST_SHA256" \
    && grep -Fq "\"manifest_sha256\": \"$MANIFEST_SHA256\"" /opt/maf-bicep/dependencies.json \
    && chmod -R a-w /opt/maf-bicep
