FROM debian:bookworm-slim

# Graphviz provides `dot`, the only program this sandbox runs. --no-install-recommends keeps
# the layer to the renderer and its libraries; the apt lists are dropped so nothing but the
# package survives. There is no Python and no agent code here — the DOT to render arrives at
# run time as a file the tool writes in, and the PNG is read back out the same way.
RUN apt-get update \
    && apt-get install -y --no-install-recommends graphviz \
    && rm -rf /var/lib/apt/lists/*
