FROM mcr.microsoft.com/azurelinux/base/core:3.0@sha256:bd88359666e3f20296e7fe639a480dfd3c3c8eba299b21f26e4560353e21a656
RUN mkdir -p /maf-sandbox/work && chown 10001:20001 /maf-sandbox/work && chmod 2770 /maf-sandbox/work
USER 10001:20001
WORKDIR /maf-sandbox/work
