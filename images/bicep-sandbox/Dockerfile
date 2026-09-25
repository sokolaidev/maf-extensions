FROM mcr.microsoft.com/azurelinux/base/core:3.0

ARG BICEP_VERSION=v0.46.1
# SHA-256 of that version's bicep-linux-x64; bump it with BICEP_VERSION.
ARG BICEP_SHA256=3e011d629ea4311b7a7dd8f0040ab2b1a072ea4ff5d02cb75e0e55a9a6703fb9

# Without `icu` the Bicep CLI aborts at startup: "Couldn't find a valid ICU package".
RUN tdnf install -y ca-certificates curl icu \
    && tdnf clean all \
    && curl -fsSL -o /usr/local/bin/bicep \
        "https://github.com/Azure/bicep/releases/download/${BICEP_VERSION}/bicep-linux-x64" \
    && echo "${BICEP_SHA256}  /usr/local/bin/bicep" | sha256sum -c - \
    && chmod +x /usr/local/bin/bicep

# Fallback policy for clients that do not stage a configuration.
COPY bicepconfig.json /maf-sandbox/work/bicepconfig.json
