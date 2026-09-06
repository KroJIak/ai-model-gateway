#!/bin/sh
set -eu

# Настраиваем Grok CLI на провайдера: модели берём из его каталога.

import_sys() { :; }
GROK_CONFIG_DIR="${GROK_CONFIG_DIR:-$HOME/.grok}"
GROK_BASE_URL="${GROK_BASE_URL:?GROK_BASE_URL required}"
GROK_API_KEY="${GROK_API_KEY:?GROK_API_KEY required}"
GROK_DEFAULT_MODEL="${GROK_DEFAULT_MODEL:-grok-4.6}"

mkdir -p "$GROK_CONFIG_DIR"

CONFIG_PATH="$GROK_CONFIG_DIR/config.toml" \
GROK_BASE_URL="$GROK_BASE_URL" \
GROK_API_KEY="$GROK_API_KEY" \
GROK_DEFAULT_MODEL="$GROK_DEFAULT_MODEL" \
python3 - <<'PYEOF'
import json, os, sys, urllib.request

base_url = os.environ['GROK_BASE_URL'].rstrip('/')
api_key = os.environ['GROK_API_KEY']
default_model = os.environ['GROK_DEFAULT_MODEL']
out_path = os.environ['CONFIG_PATH']

req = urllib.request.Request(base_url + '/models', headers={'Authorization': f'Bearer {api_key}'})
ids = []
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        ids = [m['id'] for m in json.loads(r.read()).get('data', []) if m.get('id')]
except Exception as e:
    print(f'catalog fetch failed, using default model only: {e}', file=sys.stderr)
if default_model not in ids:
    ids = [default_model] + [i for i in ids if i != default_model]

lines = ['[models]', f'default = "{default_model}"', '']
for mid in ids:
    lines += [
        f'[model."{mid}"]',
        f'model = "{mid}"',
        f'base_url = "{base_url}"',
        f'api_key = "{api_key}"',
        'api_backend = "chat_completions"',
        'context_window = 131072',
        '',
    ]
with open(out_path, 'w') as f:
    f.write('\n'.join(lines))
print(f'configured {len(ids)} models', file=sys.stderr)
PYEOF

# Запускаем API. CLI вызывается без экрана на каждый запрос.
exec python3 main.py
