FROM cgr.dev/chainguard/wolfi-base@sha256:9c2092b053779e14c82fb50f77b37bcc38b7d2c83972352d5813280f9d035b03
LABEL org.opencontainers.image.title="Hyperlight AKS probe" \
      org.opencontainers.image.description="Scoped Hyperlight verification application with pinned workspace dependencies" \
      org.opencontainers.image.documentation="https://github.com/sokolaidev/maf-extensions/blob/main/images/hyperlight-sandbox/README.md"

RUN apk upgrade --no-cache \
    && apk add --no-cache python-3.13 py3.13-pip libstdc++ openssl \
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
