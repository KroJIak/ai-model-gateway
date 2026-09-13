"""Auto-discovery of the provider's model catalog (no hardcoded lists)."""

import json
import os
import time
import urllib.request
from threading import Lock

TTL = float(os.getenv('GROK_MODELS_TTL', '60'))
BASE = os.getenv('GROK_BASE_URL', '').rstrip('/')
KEY = os.getenv('GROK_API_KEY', '')
_cache = {'ts': 0.0, 'ids': [], 'names': {}}
_lock = Lock()


def _fetch():
    if not (BASE and KEY):
        return [], {}
    req = urllib.request.Request(BASE + '/models', headers={'Authorization': f'Bearer {KEY}'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception:
        return list(_cache['ids']), dict(_cache['names'])  # last known catalog on failure
    ids, names = [], {}
    for m in data.get('data', []):
        if isinstance(m, dict) and m.get('id'):
            ids.append(m['id'])
            if isinstance(m.get('display_name'), str) and m['display_name'].strip():
                names[m['id']] = m['display_name'].strip()
    return ids, names


def get_models():
    """Cached provider model ids."""
    with _lock:
        if time.time() - _cache['ts'] < TTL and _cache['ids']:
            return list(_cache['ids'])
    ids, names = _fetch()
    with _lock:
        _cache.update(ts=time.time(), ids=ids, names=names)
        return list(ids)


def get_display_name(model_id: str):
    """Provider's display name for the model, if known."""
    with _lock:
        return _cache['names'].get(model_id)


def is_supported(model_id: str) -> bool:
    """Known id from the catalog; unknown ids are still tried if the
    catalog is temporarily unavailable (graceful degradation)."""
    ids = get_models()
    return not ids or model_id in ids
