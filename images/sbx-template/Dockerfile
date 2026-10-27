# /maf-sandbox goes because the backend mounts the workspace there and refuses an image that has it.
# A base whose unshare cannot map ids fails here, not at acquire.
ARG BASE
FROM ${BASE}
USER root
RUN rm -rf /maf-sandbox \
    && if command -v tdnf >/dev/null; then \
         tdnf install -y bash util-linux coreutils shadow-utils && tdnf clean all; \
       elif command -v apt-get >/dev/null; then \
         apt-get update && apt-get install -y --no-install-recommends bash util-linux mount \
         && rm -rf /var/lib/apt/lists/*; \
       elif command -v apk >/dev/null; then \
         apk add --no-cache bash util-linux-misc mount umount coreutils shadow; \
       else \
         echo "the base image has no supported package manager (tdnf, apt-get, apk)" >&2 && exit 1; \
       fi \
    && { unshare --map-user=0 --map-group=0 --help >/dev/null \
         || { echo "this base's unshare has no --map-user or --map-group, which the backend needs" >&2; \
              exit 1; }; }
