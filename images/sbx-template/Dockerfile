# Adapts an image for maf-sandbox-docker-sbx:
#   docker build --build-arg BASE=bicep-sandbox:local -t bicep-sandbox:sbx images/sbx-template
# It adds bash and util-linux, and removes /maf-sandbox, where the backend mounts the workspace.
ARG BASE
FROM ${BASE}
USER root
RUN rm -rf /maf-sandbox \
    && if command -v tdnf >/dev/null; then \
         tdnf install -y bash util-linux coreutils && tdnf clean all; \
       elif command -v apt-get >/dev/null; then \
         apt-get update && apt-get install -y --no-install-recommends bash util-linux mount \
         && rm -rf /var/lib/apt/lists/*; \
       else \
         echo "the base image has neither tdnf nor apt-get" >&2 && exit 1; \
       fi
