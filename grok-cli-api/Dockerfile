# Grok Build CLI as an OpenAI-compatible API (provider-agnostic via env)
FROM python:3.12-slim

COPY grok-unleashed-linux-x86_64 /usr/local/bin/grok
RUN chmod 0755 /usr/local/bin/grok

WORKDIR /app
COPY main.py .
COPY provider_models.py .
COPY agent_prompt.txt .
RUN pip install --no-cache-dir fastapi uvicorn

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

ENV GROK_BIN=/usr/local/bin/grok \
    GROK_CONFIG_DIR=/data/.grok \
    HOST=0.0.0.0 \
    PORT=8090

EXPOSE 8090
ENTRYPOINT ["docker-entrypoint.sh"]
