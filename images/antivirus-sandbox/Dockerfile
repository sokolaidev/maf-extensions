FROM alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a AS seed

RUN apk upgrade --no-cache \
    && apk add --no-cache clamav-scanner=1.4.6-r0 ca-certificates \
    && mkdir -p /opt/maf-antivirus/seed \
    && printf '%s\n' 'DatabaseMirror database.clamav.net' \
       'DatabaseDirectory /opt/maf-antivirus/seed' 'DatabaseOwner root' \
       'ScriptedUpdates no' 'Bytecode yes' > /tmp/freshclam.conf \
    && freshclam --config-file=/tmp/freshclam.conf \
    && sigtool --info /opt/maf-antivirus/seed/main.cvd \
    && sigtool --info /opt/maf-antivirus/seed/daily.cvd \
    && sigtool --info /opt/maf-antivirus/seed/bytecode.cvd \
    && rm -f /opt/maf-antivirus/seed/freshclam.dat

FROM alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a

LABEL org.opencontainers.image.description="Offline ClamAV upload scanner with an authenticated seed and host-supplied updates. Usage: https://github.com/sokolaidev/maf-extensions/blob/main/images/antivirus-sandbox/README.md"

RUN apk upgrade --no-cache \
    && apk add --no-cache clamav-scanner=1.4.6-r0 python3 \
    && mkdir -p /maf-sandbox/work
COPY --from=seed /opt/maf-antivirus/seed /opt/maf-antivirus/seed

CMD ["sleep", "infinity"]
