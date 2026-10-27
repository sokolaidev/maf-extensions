FROM mcr.microsoft.com/dotnet/sdk:10.0.403@sha256:de4fe90a98845d75910b9769662465c84de1a934665c431279d1382b2bddc02b AS build
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY source.json source.Dockerfile dependencies.csproj /build/
COPY --from=scripts bicep_source.py container_image_inputs.py /build/
RUN --mount=type=cache,target=/root/.nuget/packages \
    python3 /build/bicep_source.py --policy /build/source.json --output /out

FROM cgr.dev/chainguard/wolfi-base:latest@sha256:360896efb0753a5701c48f860e17130c205eae5a264797aa3e7ea126ee2923a7 AS assembled
RUN apk upgrade --no-cache && apk add --no-cache ca-certificates-bundle icu libstdc++ libssl3
COPY --from=build /out/bicep /usr/local/bin/bicep
COPY --from=build /out/build.json /usr/share/maf-bicep/build.json
COPY --from=build /out/NOTICE /usr/share/licenses/bicep/NOTICE
COPY BICEP-LICENSE /usr/share/licenses/bicep/LICENSE
COPY bicepconfig.json /maf-sandbox/work/bicepconfig.json

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
LABEL dev.sokolai.maf.bicep.route="source"
