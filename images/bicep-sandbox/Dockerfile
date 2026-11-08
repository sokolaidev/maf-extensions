FROM cgr.dev/chainguard/wolfi-base:latest@sha256:24f549a7fcace88e69b9557941168c74645688ecc4acf7fb6d734a6b74e9019d AS assembled

ARG BICEP_VERSION=v0.49.2
# SHA-256 of that version's bicep-linux-x64; bump it with BICEP_VERSION.
ARG BICEP_SHA256=0ab9de90c2493f206f8281bae74f9ee2d5ed3605548dab1adf41fef185e1f7c5

# Without `icu` the Bicep CLI aborts at startup: "Couldn't find a valid ICU package".
RUN apk upgrade --no-cache && apk add --no-cache ca-certificates-bundle curl icu libstdc++ \
    && mkdir -p /usr/local/bin \
    && curl -fsSL -o /usr/local/bin/bicep \
        "https://github.com/Azure/bicep/releases/download/${BICEP_VERSION}/bicep-linux-x64" \
    && echo "${BICEP_SHA256}  /usr/local/bin/bicep" | sha256sum -c - \
    && chmod +x /usr/local/bin/bicep

# Fallback policy for clients that do not stage a configuration.
COPY bicepconfig.json /maf-sandbox/work/bicepconfig.json
COPY BICEP-LICENSE /usr/share/licenses/bicep/LICENSE

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
