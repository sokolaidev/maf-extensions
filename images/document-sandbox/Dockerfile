FROM cgr.dev/chainguard/wolfi-base:latest@sha256:0b179206ab162b12a43bacea1b525a4023e0272005c92d08555c61b1beaedc40
RUN --mount=type=bind,from=python-packages,target=/python-packages \
    cp /python-packages/maf-python.rsa.pub /etc/apk/keys/ \
    && apk add --no-cache /python-packages/x86_64/python-3.13-3.13.18-r0.apk /python-packages/x86_64/python-3.13-base-3.13.18-r0.apk \
    && rm /etc/apk/keys/maf-python.rsa.pub \
    && python3 -c "import sys, ssl; assert sys.version_info[:3] == (3, 13, 18); assert ssl.HAS_PSK"

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
