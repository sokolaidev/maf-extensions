# The egress proxy for Egress.ALLOWLIST: build with `wslc build -t <tag> <this directory>`.
# Pinned by digest, not tag, because this image is a security boundary — refresh it with
# `wslc inspect mcr.microsoft.com/azurelinux/base/core:3.0` (read RepoDigests) when you mean to.
FROM mcr.microsoft.com/azurelinux/base/core@sha256:4ecd6b297db85c54ec2df07145a28536c3655a3e98e54eb2364189bc4e6eac23
RUN tdnf install -y python3 && tdnf clean all
COPY proxy.py /proxy.py
EXPOSE 3128
CMD ["python3", "/proxy.py"]
