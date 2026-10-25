FROM cgr.dev/chainguard/wolfi-base@sha256:9c2092b053779e14c82fb50f77b37bcc38b7d2c83972352d5813280f9d035b03

LABEL org.opencontainers.image.description="Editable Draw.io authoring runtime with Python and Graphviz for Linux/amd64. Usage, verification and support: https://github.com/sokolaidev/maf-extensions/blob/main/images/drawio-sandbox/README.md"

RUN apk upgrade --no-cache \
    && apk add --no-cache python-3.13 graphviz ttf-dejavu \
       'libbrotlicommon1>=1.2.0-r7' 'libbrotlidec1>=1.2.0-r7' 'libbrotlienc1>=1.2.0-r7' \
    && dot -c

COPY DEJAVU-LICENSE /usr/share/licenses/drawio/DEJAVU-LICENSE

CMD ["sleep", "infinity"]
