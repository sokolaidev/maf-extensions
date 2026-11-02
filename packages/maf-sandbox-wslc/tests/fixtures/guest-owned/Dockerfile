FROM mcr.microsoft.com/azurelinux/base/core:3.0@sha256:88fc2b4a425801b3b4d8fe5342e24fb6134669fb718ec970340bdd36dee50c35
RUN mkdir -p /maf-sandbox/work && chown 10001:20001 /maf-sandbox/work && chmod 2770 /maf-sandbox/work
USER 10001:20001
WORKDIR /maf-sandbox/work
