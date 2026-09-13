FROM python:3.13.12-slim-bookworm@sha256:3121f8b0804aa3698ab750d9a39ea4a42657a385c9b133722b915e55c51551a6

COPY requirements.txt /proof/requirements.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /proof/requirements.txt
COPY probe.py /proof/probe.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/work XDG_CACHE_HOME=/work/cache TMPDIR=/work/tmp
USER 65534:65534
WORKDIR /work
ENTRYPOINT ["timeout", "--signal=TERM", "--kill-after=5s", "90s", "python", "-I", "-u", "/proof/probe.py"]
CMD ["--stage", "sdk"]
