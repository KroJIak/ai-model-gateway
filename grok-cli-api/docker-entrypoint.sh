#!/bin/sh
set -eu

# Настраиваем Grok CLI на провайдера: модели берём из его каталога.

GROK_CONFIG_DIR="${GROK_CONFIG_DIR:?GROK_CONFIG_DIR required}"
GROK_BASE_URL="${GROK_BASE_URL:?GROK_BASE_URL required}"
GROK_API_KEY="${GROK_API_KEY:?GROK_API_KEY required}"

mkdir -p "$GROK_CONFIG_DIR"

CONFIG_PATH="$GROK_CONFIG_DIR/config.toml" \
GROK_BASE_URL="$GROK_BASE_URL" \
GROK_API_KEY="$GROK_API_KEY" \
python3 - <<'PYEOF'
import json, os, re, sys, urllib.request

base_url = os.environ['GROK_BASE_URL'].rstrip('/')
api_key = os.environ['GROK_API_KEY']
out_path = os.environ['CONFIG_PATH']

def label_for(mid):
    """Личность агента = настоящее имя семейства модели (без навязанного Grok)."""
    m = re.match(r'[a-z]+', mid.lower())
    prefix = m.group(0) if m else 'AI'
    return {'gpt': 'GPT', 'glm': 'GLM', 'deepseek': 'DeepSeek'}.get(prefix, prefix.capitalize())

req = urllib.request.Request(base_url + '/models', headers={'Authorization': f'Bearer {api_key}'})
ids = []
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        ids = [m['id'] for m in json.loads(r.read()).get('data', []) if m.get('id')]
except Exception as e:
    print(f'catalog fetch failed, models will come from provider anyway: {e}', file=sys.stderr)

default_model = next((i for i in ids if i.startswith('grok-')), ids[0] if ids else 'grok-4.6')
ids = [default_model] + [i for i in ids if i != default_model]

lines = ['[models]', f'default = "{default_model}"', '']
for mid in ids:
    lines += [
        f'[model."{mid}"]',
        f'model = "{mid}"',
        f'system_prompt_label = "{label_for(mid)}"',
        f'base_url = "{base_url}"',
        f'api_key = "{api_key}"',
        'api_backend = "chat_completions"',
        'context_window = 131072',
        '',
    ]
with open(out_path, 'w') as f:
    f.write('\n'.join(lines))
print(f'configured {len(ids)} models, default: {default_model}', file=sys.stderr)
PYEOF

# Запускаем API.
exec python3 main.py
