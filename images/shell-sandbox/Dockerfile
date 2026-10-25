FROM python:3.13-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libicu76 libssl3t64 libgmp10 bash dash util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/maf-shell/modules
COPY image.json install.py /recipe/
RUN python3 /recipe/install.py bash /tmp \
    && python3 /recipe/install.py dash /tmp \
    && python3 /recipe/install.py powershell /opt/powershell \
    && chmod +x /opt/powershell/pwsh \
    && ln -s /opt/powershell/pwsh /usr/local/bin/pwsh \
    && python3 /recipe/install.py psscriptanalyzer /opt/maf-shell/modules/PSScriptAnalyzer/1.25.0
ENV POWERSHELL_TELEMETRY_OPTOUT=1 DOTNET_CLI_TELEMETRY_OPTOUT=1
CMD ["sleep", "infinity"]

FROM runtime AS compiler
RUN apt-get update && apt-get install -y --no-install-recommends \
    ghc libghc-aeson-dev libghc-diff-dev libghc-fgl-dev libghc-regex-tdfa-dev libghc-quickcheck2-dev \
    && rm -rf /var/lib/apt/lists/*
RUN python3 /recipe/install.py shellcheck /build
COPY patch_shellcheck.py ShellAudit.hs /recipe/
RUN python3 /recipe/patch_shellcheck.py \
    && cd /build/shellcheck-0.11.0 \
    && ghc -XHaskell98 -O1 -threaded -rtsopts -isrc -o /usr/local/bin/maf-shell-audit /recipe/ShellAudit.hs

FROM runtime AS shell
COPY --from=compiler /usr/local/bin/maf-shell-audit /usr/local/bin/maf-shell-audit
COPY --from=compiler /usr/share/maf-shell/shellcheck-source.tar.gz /usr/share/maf-shell/
COPY --from=compiler /build/shellcheck-0.11.0/LICENSE /usr/share/licenses/shellcheck/LICENSE
COPY ShellAudit.hs patch_shellcheck.py /usr/share/maf-shell/
COPY runner.py analyze.ps1 analysis-output.ps1 dependencies.json prepare_dependencies.py /opt/maf-shell/
RUN apt-get update && apt-get install -y --no-install-recommends libnuma1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/maf-shell/dependencies/modules /opt/maf-shell/dependencies/assemblies
