FROM cgr.dev/chainguard/wolfi-base:latest@sha256:360896efb0753a5701c48f860e17130c205eae5a264797aa3e7ea126ee2923a7 AS assembled

RUN apk upgrade --no-cache \
    && apk add --no-cache python-3.13 graphviz ttf-dejavu \
       'libbrotlicommon1>=1.2.0-r7' 'libbrotlidec1>=1.2.0-r7' 'libbrotlienc1>=1.2.0-r7' \
    && dot -c

COPY DEJAVU-LICENSE /usr/share/licenses/drawio/DEJAVU-LICENSE

FROM scratch
# ACAS supplies runtime devices and rejects device nodes embedded in image layers.
COPY --from=assembled --exclude=dev/* / /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
LABEL org.opencontainers.image.description="Editable Draw.io authoring runtime with Python and Graphviz for Linux/amd64. Usage, verification and support: https://github.com/sokolaidev/maf-extensions/blob/main/images/drawio-sandbox/README.md"
CMD ["sleep", "infinity"]
