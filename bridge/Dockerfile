# Model Bridge — aggregator image
FROM python:3.12-slim

WORKDIR /app
COPY bridge/requirements.txt ./bridge-requirements.txt
RUN pip install --no-cache-dir -r bridge-requirements.txt
COPY bridge/main.py ./main.py
COPY bridge/icons ./icons

ENV HOST=0.0.0.0 \
    PORT=8080 \
    BRIDGE_CONFIG=/app/config.yaml

EXPOSE 8080
CMD ["python", "main.py"]
