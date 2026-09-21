FROM mcr.microsoft.com/azurelinux/base/core:3.0
RUN mkdir -p /maf-sandbox/work /usr/local/bin && chown 10001:20001 /maf-sandbox/work /usr/local/bin
ENV PATH=/usr/local/bin:/usr/bin:/bin
USER 10001:20001
WORKDIR /maf-sandbox/work
