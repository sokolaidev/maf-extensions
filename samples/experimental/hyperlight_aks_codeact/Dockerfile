ARG HYPERLIGHT_RUNTIME
FROM ${HYPERLIGHT_RUNTIME}

USER 0
COPY requirements.txt /opt/aks-codeact-requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/aks-codeact-requirements.txt \
    && python -m pip check \
    && python -c "from agent_framework.openai import OpenAIChatClient"
COPY agent.py _scaffold.py /opt/aks-codeact/
USER 65534:65534
CMD ["python", "-I", "-u", "-m", "maf_sandbox_hyperlight._pod_supervisor", "python", "-u", "/opt/aks-codeact/agent.py"]
