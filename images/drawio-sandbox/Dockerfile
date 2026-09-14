FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 graphviz \
    && rm -rf /var/lib/apt/lists/*

CMD ["sleep", "infinity"]
