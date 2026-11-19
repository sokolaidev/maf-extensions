FROM python:3.14.10-alpine3.24@sha256:5862d971d1f0ae26b258cefa4e893556b1d3aba81bd2ff30888704ad378bab67 AS base

FROM base AS sqlite-build

# Build the newer recipe against the same Alpine release as the runtime.
ADD --checksum=sha256:de648fc75c4ec249f958c9cf557793ab35a5a9e90b7d8758a3ec654f483117bd https://raw.githubusercontent.com/alpinelinux/aports/f8081b25590214e2695a4faa77c7892bd902a4e8/main/sqlite/APKBUILD /build/sqlite/APKBUILD
WORKDIR /build/sqlite
RUN apk add --no-cache abuild build-base readline-dev \
    && abuild-keygen -anq \
    && cp /root/.abuild/*.rsa.pub /etc/apk/keys/ \
    && abuild -FK \
    && mkdir /sqlite-package \
    && cp /root/packages/*/"$(apk --print-arch)"/sqlite-libs-*.apk /root/.abuild/*.rsa.pub /sqlite-package/ \
    && cp src/sqlite-src-*/LICENSE.md /sqlite-package/LICENSE.md

FROM base AS seed

RUN apk upgrade --no-cache \
    && apk add --no-cache clamav-scanner=1.4.7-r0 ca-certificates \
    && mkdir -p /opt/maf-antivirus/seed \
    && printf '%s\n' 'DatabaseMirror database.clamav.net' \
       'DatabaseDirectory /opt/maf-antivirus/seed' 'DatabaseOwner root' \
       'ScriptedUpdates no' 'Bytecode yes' > /tmp/freshclam.conf \
    && freshclam --config-file=/tmp/freshclam.conf \
    && sigtool --info /opt/maf-antivirus/seed/main.cvd \
    && sigtool --info /opt/maf-antivirus/seed/daily.cvd \
    && sigtool --info /opt/maf-antivirus/seed/bytecode.cvd \
    && rm -f /opt/maf-antivirus/seed/freshclam.dat

FROM base

LABEL org.opencontainers.image.description="Offline ClamAV upload scanner with an authenticated seed and host-supplied updates. Usage: https://github.com/sokolaidev/maf-extensions/blob/main/images/antivirus-sandbox/README.md"

RUN --mount=type=bind,from=sqlite-build,source=/sqlite-package,target=/sqlite-package \
    apk upgrade --no-cache \
    && apk add --no-network --keys-dir /sqlite-package /sqlite-package/sqlite-libs-*.apk \
    && apk add --no-cache clamav-scanner=1.4.7-r0 \
    && install -Dm644 /sqlite-package/LICENSE.md /usr/share/licenses/sqlite/LICENSE.md \
    && python3 -I -c 'import sqlite3; assert sqlite3.sqlite_version == "3.54.1", sqlite3.sqlite_version' \
    && mkdir -p /maf-sandbox/work
COPY --from=seed /opt/maf-antivirus/seed /opt/maf-antivirus/seed

CMD ["sleep", "infinity"]
