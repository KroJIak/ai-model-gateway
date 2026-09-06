"""Auto-discovery of the provider's model catalog (no hardcoded lists)."""

import json
import os
import time
import urllib.request
from threading import Lock

TTL = float(os.getenv('GROK_MODELS_TTL', '60'))
BASE = os.getenv('GROK_BASE_URL', '').rstrip('/')
KEY = os.getenv('GROK_API_KEY', '')
_cache = {'ts': 0.0, 'ids': []}
_lock = Lock()


def _fetch():
    if not (BASE and KEY):
        return []
    req = urllib.request.Request(BASE + '/models', headers={'Authorization': f'Bearer {KEY}'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception:
        return list(_cache['ids'])  # last known catalog on failure
    ids = [
        m.get('id') for m in data.get('data', [])
        if isinstance(m, dict) and m.get('id')
    ]
    return ids


def get_models():
    """Cached provider model ids."""
    with _lock:
        if time.time() - _cache['ts'] < TTL and _cache['ids']:
            return list(_cache['ids'])
    ids = _fetch()
    with _lock:
        _cache.update(ts=time.time(), ids=ids)
        return list(ids)


def is_supported(model_id: str) -> bool:
    """Known id from the catalog; unknown ids are still tried if the
    catalog is temporarily unavailable (graceful degradation)."""
    ids = get_models()
    return not ids or model_id in ids
