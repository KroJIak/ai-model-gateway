#!/bin/sh
set -eu

# Настраиваем CLIProxyAPI на провайдера по переменным из .env.

ROLE="${API_ROLE:-gpt}"
PORT_NUM="${API_PORT:-8317}"
INBOUND_KEY="${API_KEYS:?API_KEYS required (inbound bearer key)}"
PROVIDER_URL="${PROVIDER_BASE_URL:?PROVIDER_BASE_URL required}"
PROVIDER_KEY="${PROVIDER_API_KEY:?PROVIDER_API_KEY required}"

mkdir -p /CLIProxyAPI /root/.cli-proxy-api

cat > /CLIProxyAPI/config.yaml <<EOF
host: ""
port: ${PORT_NUM}
auth-dir: "/root/.cli-proxy-api"
api-keys:
  - "${INBOUND_KEY}"
remote-management:
  allow-remote: false
request-retry: 3
codex-api-key:
  - api-key: "${PROVIDER_KEY}"
    base-url: "${PROVIDER_URL}"
EOF

/CLIProxyAPI/CLIProxyAPI &
PID=$!

# Убираем лишнюю секцию, которую добавляет образ.
i=0
while [ "$i" -lt 30 ]; do
  if grep -q '^payload:' /CLIProxyAPI/config.yaml 2>/dev/null; then
    awk 'BEGIN{drop=0} /^payload:/{drop=1} !drop{print}' \
      /CLIProxyAPI/config.yaml > /CLIProxyAPI/config.stripped.yaml
    mv /CLIProxyAPI/config.stripped.yaml /CLIProxyAPI/config.yaml
    break
  fi
  i=$((i + 1))
  sleep 1
done

wait "$PID"
