FROM cgr.dev/chainguard/wolfi-base@sha256:360896efb0753a5701c48f860e17130c205eae5a264797aa3e7ea126ee2923a7 AS runtime
RUN apk upgrade --no-cache && apk add --no-cache \
    python-3.13 ca-certificates-bundle icu libssl3 libcrypto3 gmp numactl libstdc++ util-linux ncurses \
    && mkdir -p /opt/maf-shell/modules /usr/local/bin \
    && ln -s /usr/bin/python3 /usr/local/bin/python3
COPY image.json install.py /recipe/
RUN python3 /recipe/install.py bash /tmp \
    && python3 /recipe/install.py dash /tmp \
    && python3 /recipe/install.py powershell /opt/powershell \
    && chmod +x /opt/powershell/pwsh \
    && ln -s /opt/powershell/pwsh /usr/local/bin/pwsh \
    && python3 /recipe/install.py psscriptanalyzer /opt/maf-shell/modules/PSScriptAnalyzer/1.25.0

FROM python:3.13-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS compiler
RUN apt-get update && apt-get install -y --no-install-recommends \
    ghc libghc-aeson-dev libghc-diff-dev libghc-fgl-dev libghc-regex-tdfa-dev libghc-quickcheck2-dev \
    && rm -rf /var/lib/apt/lists/*
COPY image.json install.py /recipe/
RUN python3 /recipe/install.py shellcheck /build
COPY patch_shellcheck.py ShellAudit.hs /recipe/
RUN python3 /recipe/patch_shellcheck.py \
    && cd /build/shellcheck-0.11.0 \
    && ghc -XHaskell98 -O1 -threaded -rtsopts -isrc -o /usr/local/bin/maf-shell-audit /recipe/ShellAudit.hs

FROM runtime AS assembled
COPY --from=compiler /usr/local/bin/maf-shell-audit /usr/local/bin/maf-shell-audit
COPY --from=compiler /usr/share/maf-shell/shellcheck-source.tar.gz /usr/share/maf-shell/
COPY --from=compiler /build/shellcheck-0.11.0/LICENSE /usr/share/licenses/shellcheck/LICENSE
COPY ShellAudit.hs patch_shellcheck.py /usr/share/maf-shell/
COPY runner.py analyze.ps1 analysis-output.ps1 dependencies.json prepare_dependencies.py /opt/maf-shell/
RUN mkdir -p /opt/maf-shell/dependencies/modules /opt/maf-shell/dependencies/assemblies

FROM scratch AS shell
# ACAS supplies runtime devices and rejects device nodes embedded in image layers.
COPY --from=assembled --exclude=dev/* / /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    POWERSHELL_TELEMETRY_OPTOUT=1 DOTNET_CLI_TELEMETRY_OPTOUT=1
USER 0
CMD ["sleep", "infinity"]
