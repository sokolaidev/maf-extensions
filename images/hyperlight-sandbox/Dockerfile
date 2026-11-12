FROM cgr.dev/chainguard/wolfi-base:latest@sha256:94fa13e26d449f7d78dbcd02d12ea37a0da80d24fcc3e1f17b95d515dec607a7
RUN --mount=type=bind,from=python-packages,target=/python-packages \
    cp /python-packages/maf-python.rsa.pub /etc/apk/keys/ \
    && apk add --no-cache /python-packages/x86_64/python-3.13-3.13.18_git20261030-r0.apk /python-packages/x86_64/python-3.13-base-3.13.18_git20261030-r0.apk \
    && rm /etc/apk/keys/maf-python.rsa.pub \
    && python3 -c "import sys, ssl; assert sys.version_info[:3] == (3, 13, 18); assert ssl.HAS_PSK"
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
