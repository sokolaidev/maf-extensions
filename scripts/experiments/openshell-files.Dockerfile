FROM maf-openshell-1797-files-base
USER root
RUN mkdir -p /opt/spike1797 && chmod 777 /opt/spike1797 && printf outside-sentinel > /opt/spike1797/sentinel && chmod 666 /opt/spike1797/sentinel && printf readonly-sentinel > /etc/spike1797-readable && chmod 666 /etc/spike1797-readable
USER sandbox
