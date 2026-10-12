FROM mcr.microsoft.com/azurelinux/base/core:3.0

ARG BICEP_VERSION=v0.48.1
# SHA-256 of that version's bicep-linux-x64; bump it with BICEP_VERSION.
ARG BICEP_SHA256=b09ec25a9d376c1f8e33ede6ed22b587f915ad68488d5db77a6f9541748c7f6e

# Without `icu` the Bicep CLI aborts at startup: "Couldn't find a valid ICU package".
RUN tdnf install -y ca-certificates curl icu \
    && tdnf clean all \
    && curl -fsSL -o /usr/local/bin/bicep \
        "https://github.com/Azure/bicep/releases/download/${BICEP_VERSION}/bicep-linux-x64" \
    && echo "${BICEP_SHA256}  /usr/local/bin/bicep" | sha256sum -c - \
    && chmod +x /usr/local/bin/bicep

# Fallback policy for clients that do not stage a configuration.
COPY bicepconfig.json /maf-sandbox/work/bicepconfig.json
COPY BICEP-LICENSE /usr/share/licenses/bicep/LICENSE
