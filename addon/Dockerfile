FROM ghcr.io/hassio-addons/base:21.1.0@sha256:ce73287a53cc08874e5fa8e3fe3e9aac098171c40326c0d384e93a5761e067b2

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
