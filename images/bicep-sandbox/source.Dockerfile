ARG SDK_IMAGE=scratch
FROM ${SDK_IMAGE} AS build
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY source.json /build/source.json
COPY --from=scripts bicep_source.py /build/bicep_source.py
RUN --mount=type=cache,target=/root/.nuget/packages \
    python3 /build/bicep_source.py --policy /build/source.json --output /out

FROM mcr.microsoft.com/azurelinux/base/core:3.0
RUN tdnf install -y ca-certificates icu && tdnf clean all
LABEL dev.sokolai.maf.bicep.route="source"
COPY --from=build /out/bicep /usr/local/bin/bicep
COPY --from=build /out/build.json /usr/share/maf-bicep/build.json
COPY --from=build /out/NOTICE /usr/share/licenses/bicep/NOTICE
COPY BICEP-LICENSE /usr/share/licenses/bicep/LICENSE
COPY bicepconfig.json /maf-sandbox/work/bicepconfig.json
