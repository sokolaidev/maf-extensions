FROM python:3.13.12-slim-bookworm@sha256:3121f8b0804aa3698ab750d9a39ea4a42657a385c9b133722b915e55c51551a6

COPY requirements.txt /opt/requirements.txt
COPY wheels/ /opt/wheels/
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/requirements.txt \
    && python -m pip install --no-cache-dir --no-deps /opt/wheels/*.whl \
    && python -m pip check
COPY probe.py /opt/hyperlight-probe.py
COPY .dockerignore Dockerfile build-inputs.json source.json verify.py /opt/
USER 65534:65534
CMD ["python", "-I", "-u", "-m", "maf_sandbox_hyperlight._pod_supervisor", "python", "-I", "-u", "/opt/hyperlight-probe.py"]
