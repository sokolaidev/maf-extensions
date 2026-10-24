FROM python@sha256:8fb4cfa1a2616d7b8e0c2175cc6ad68f5729c34ea8488c0b360d2934b7be9024
RUN groupadd --gid 1000 sandbox && useradd --uid 1000 --gid 1000 --create-home sandbox && mkdir /sandbox && chown sandbox:sandbox /sandbox
WORKDIR /sandbox
USER sandbox
CMD ["sleep", "3600"]
