# The initial supported guest platform is Linux amd64. Build each engine separately.
FROM python:3.13-slim@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2
ARG TARGETARCH
ARG ENGINE=terraform
ARG PROFILE=builtin
COPY install.py /opt/maf-terraform/install.py
RUN test "$TARGETARCH" = amd64 \
    && python3 -I /opt/maf-terraform/install.py "$ENGINE" "$PROFILE" \
    && rm /opt/maf-terraform/install.py
COPY runner.py /opt/maf-terraform/runner.py
# docker exec invokes the launcher; the image entrypoint does not accept workload arguments.
CMD ["sleep", "infinity"]
