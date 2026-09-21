FROM mcr.microsoft.com/azurelinux/base/core:3.0

ARG BICEP_VERSION=v0.46.1

# Without `icu` the Bicep CLI aborts at startup: "Couldn't find a valid ICU package".
RUN tdnf install -y ca-certificates curl icu \
    && tdnf clean all \
    && curl -fsSL -o /usr/local/bin/bicep \
        "https://github.com/Azure/bicep/releases/download/${BICEP_VERSION}/bicep-linux-x64" \
    && chmod +x /usr/local/bin/bicep

# Fallback policy for clients that do not stage their own packaged configuration.
COPY bicepconfig.json /maf-sandbox/work/bicepconfig.json
