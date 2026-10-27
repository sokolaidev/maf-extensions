FROM cgr.dev/chainguard/wolfi-base:latest@sha256:360896efb0753a5701c48f860e17130c205eae5a264797aa3e7ea126ee2923a7

LABEL org.opencontainers.image.description="Guarded PDF, DOCX, PPTX and RTF conversion runtime for Linux/amd64. Usage and limits: https://github.com/sokolaidev/maf-extensions/blob/main/images/document-sandbox/README.md"

COPY requirements.txt /opt/maf-document/requirements.txt
RUN apk upgrade --no-cache \
    && apk add --no-cache python-3.13 libstdc++ \
    && apk add --no-cache --virtual .install py3.13-pip \
    && python3 -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
       --target /usr/lib/python3.13/site-packages -r /opt/maf-document/requirements.txt \
    && apk del --no-cache .install \
    && python3 -c "import pypdfium2, mammoth, pptx, striprtf" \
    && mkdir -p /maf-sandbox/work

CMD ["sleep", "infinity"]
