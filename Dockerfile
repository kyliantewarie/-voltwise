# Standalone image for Home Assistant Container / generic Docker users.
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="VoltWise" \
      org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /opt/voltwise
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY voltwise/ ./voltwise/

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/voltwise

EXPOSE 8099
CMD ["python", "-m", "voltwise.service", "--config", "/config/voltwise.json", \
     "--output", "/data/voltwise/plan.json", "--web-host", "0.0.0.0"]
