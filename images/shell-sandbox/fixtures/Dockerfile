ARG BASE
FROM ${BASE}
COPY HostProbe.psm1 /opt/maf-shell/dependencies/modules/HostProbe/HostProbe.psm1
RUN /opt/powershell/pwsh -NoLogo -NoProfile -NonInteractive -Command "Add-Type -TypeDefinition 'public class HostProbeType { public string Name; }' -OutputAssembly /opt/maf-shell/dependencies/assemblies/HostProbe.dll" \
    && python3 /opt/maf-shell/prepare_dependencies.py
