FROM cgr.dev/chainguard/wolfi-base:latest@sha256:0b179206ab162b12a43bacea1b525a4023e0272005c92d08555c61b1beaedc40
LABEL org.opencontainers.image.title="Hyperlight AKS probe" \
      org.opencontainers.image.description="Scoped Hyperlight verification application with pinned workspace dependencies" \
      org.opencontainers.image.documentation="https://github.com/sokolaidev/maf-extensions/blob/main/images/hyperlight-sandbox/README.md"

RUN apk upgrade --no-cache \
    && apk add --no-cache python-3.13 py3.13-pip libstdc++ openssl \
       'libbrotlicommon1>=1.2.0-r7' 'libbrotlidec1>=1.2.0-r7' 'libbrotlienc1>=1.2.0-r7' \
    && python3.13 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /opt/requirements.txt
COPY wheels/ /opt/wheels/
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/requirements.txt \
    && python -m pip install --no-cache-dir --no-deps /opt/wheels/*.whl \
    && python -m pip check
COPY probe.py /opt/hyperlight-probe.py
COPY .dockerignore Dockerfile build-inputs.json source.json verify.py /opt/
USER 65534:65534
CMD ["python", "-I", "-u", "-m", "maf_sandbox_hyperlight._pod_supervisor", "python", "-I", "-u", "/opt/hyperlight-probe.py"]
