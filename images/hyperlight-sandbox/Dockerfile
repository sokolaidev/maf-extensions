FROM python:3.13.16-slim-trixie@sha256:3dd7cc108ec1493442514f5c2a871af6af0ec31d768ff6e378a93340c3b3db5f

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /opt/requirements.txt
COPY wheels/ /opt/wheels/
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/requirements.txt \
    && python -m pip install --no-cache-dir --no-deps /opt/wheels/*.whl \
    && python -m pip check
COPY probe.py /opt/hyperlight-probe.py
COPY .dockerignore Dockerfile build-inputs.json source.json verify.py /opt/
USER 65534:65534
CMD ["python", "-I", "-u", "-m", "maf_sandbox_hyperlight._pod_supervisor", "python", "-I", "-u", "/opt/hyperlight-probe.py"]
