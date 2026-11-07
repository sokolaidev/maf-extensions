FROM mcr.microsoft.com/azurelinux/base/core:3.0@sha256:bfb630450e9c3d7428d59f25ce6df95ba5634d664d96b06017aa56f87da5ec0e
RUN mkdir -p /maf-sandbox/work && chown 10001:20001 /maf-sandbox/work && chmod 2770 /maf-sandbox/work
USER 10001:20001
WORKDIR /maf-sandbox/work
