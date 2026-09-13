#!/bin/sh
set -eu

# Настраиваем Grok CLI на провайдера: модели берём из его каталога.

GROK_CONFIG_DIR="${GROK_CONFIG_DIR:?GROK_CONFIG_DIR required}"
GROK_BASE_URL="${GROK_BASE_URL:?GROK_BASE_URL required}"
GROK_API_KEY="${GROK_API_KEY:?GROK_API_KEY required}"

mkdir -p "$GROK_CONFIG_DIR" /workspace

CONFIG_PATH="$GROK_CONFIG_DIR/config.toml" \
GROK_BASE_URL="$GROK_BASE_URL" \
GROK_API_KEY="$GROK_API_KEY" \
python3 - <<'PYEOF'
import json, os, sys, time, urllib.request

base_url = os.environ['GROK_BASE_URL'].rstrip('/')
api_key = os.environ['GROK_API_KEY']
out_path = os.environ['CONFIG_PATH']

def label_for(mid):
    """Личность агента: красивое название от провайдера (без компании-префикса),
    иначе id модели с тире вместо пробелов."""
    name = names.get(mid, '')
    if '·' in name:
        name = name.split('·')[-1].strip()
    return name.replace('"', '') or mid.replace('-', ' ')

req = urllib.request.Request(base_url + '/models', headers={'Authorization': f'Bearer {api_key}'})
ids, names = [], {}
for attempt in range(1, 6):
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        for m in data.get('data', []):
            if isinstance(m, dict) and m.get('id'):
                ids.append(m['id'])
                name = m.get('display_name')
                if isinstance(name, str) and name.strip():
                    names[m['id']] = name.strip()
        break
    except Exception as e:
        print(f'catalog fetch attempt {attempt}/5 failed: {e}', file=sys.stderr)
        if attempt < 5:
            time.sleep(5)

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
