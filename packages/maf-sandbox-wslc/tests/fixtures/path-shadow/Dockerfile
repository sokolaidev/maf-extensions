FROM mcr.microsoft.com/azurelinux/base/core:3.0@sha256:88fc2b4a425801b3b4d8fe5342e24fb6134669fb718ec970340bdd36dee50c35
RUN mkdir -p /maf-sandbox/work /usr/local/bin && chown 10001:20001 /maf-sandbox/work /usr/local/bin
ENV PATH=/usr/local/bin:/usr/bin:/bin
USER 10001:20001
WORKDIR /maf-sandbox/work
