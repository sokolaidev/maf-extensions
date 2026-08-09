# The egress proxy for Egress.ALLOWLIST: build with `wslc build -t <tag> <this directory>`.
FROM mcr.microsoft.com/azurelinux/base/core:3.0
RUN tdnf install -y python3 && tdnf clean all
COPY proxy.py /proxy.py
EXPOSE 3128
CMD ["python3", "/proxy.py"]
