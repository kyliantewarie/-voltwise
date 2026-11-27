FROM ghcr.io/hassio-addons/base:12.2.4@sha256:edc3374383377636ce969be3f42d3413d811435c06fafe188b0b8c33b05f3c13

LABEL org.opencontainers.image.title="VoltWise" \
      org.opencontainers.image.description="Dynamic-tariff battery and EV optimiser" \
      org.opencontainers.image.licenses="Apache-2.0"

# Use the distribution package: no pip build toolchain needed on Alpine.
RUN apk add --no-cache python3 py3-yaml py3-cryptography

WORKDIR /opt/voltwise
COPY voltwise/ ./voltwise/
COPY requirements.txt ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/voltwise

COPY run.sh /
RUN chmod a+x /run.sh

EXPOSE 8099
CMD ["/run.sh"]
