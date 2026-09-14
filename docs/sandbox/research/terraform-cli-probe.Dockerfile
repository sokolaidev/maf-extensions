FROM python:3.13-slim@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2

COPY bin /probe/bin
COPY mirror /probe/mirror
COPY guest/probe.py /probe/probe.py
RUN chmod 0555 /probe/bin/terraform/terraform /probe/bin/tofu/tofu

CMD ["python", "-I", "/probe/probe.py"]
