FROM python:3.13-slim@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2
RUN pip install --no-cache-dir --only-binary=:all: \
    hyperlight-sandbox==0.7.0 \
    hyperlight-sandbox-backend-wasm==0.7.0 \
    hyperlight-sandbox-python-guest==0.7.0
COPY hyperlight-aca-probe.py /probe/hyperlight-aca-probe.py
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-I", "-u", "/probe/hyperlight-aca-probe.py"]
