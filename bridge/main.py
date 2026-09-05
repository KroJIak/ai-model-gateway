"""Model Bridge — агрегатор CLI-API сервисов в единый OpenAI-совместимый endpoint.

Правила маршрутизации (задаются в config.yaml):
- бэкенды опрашиваются в порядке конфигурации; модель закрепляется за первым
  бэкендом, который её отдаёт (приоритет специализированных сервисов);
- include/exclude — glob-паттерны имён моделей (fnmatch);
- enabled: false — бэкенд полностью выключен;
- model_meta — metadata, добавляемая всем моделям бэкенда (фичи, пометки).

Env:
  BRIDGE_CONFIG      путь к конфигу              (default: config.yaml)
  BRIDGE_CACHE_TTL   TTL скана бэкендов, сек     (default: 30)
  BRIDGE_READ_TIMEOUT таймаут чтения ответа, сек (default: 600)
  HOST / PORT        адрес сервиса               (default: 0.0.0.0:8080)
"""

import asyncio
import fnmatch
import json
import os
import time
from contextlib import asynccontextmanager

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

CONFIG_PATH = os.getenv('BRIDGE_CONFIG', 'config.yaml')
CACHE_TTL = float(os.getenv('BRIDGE_CACHE_TTL', '30'))
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', '8080'))
API_AUTH_KEY = os.getenv('BRIDGE_API_KEY', '')
CLIENT_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=float(os.getenv('BRIDGE_READ_TIMEOUT', '600')),
    write=30.0,
    pool=10.0,
)

_state = {
    'client': None,
    'lock': asyncio.Lock(),
    'ts': 0.0,
    'map': {},
    'backends': [],
    'down': [],
}


def _load_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        # ${VAR} в значениях конфига раскрываются из окружения контейнера
        return yaml.safe_load(os.path.expandvars(f.read())) or {}


def _compile_backends(cfg):
    backends = []
    for i, b in enumerate(cfg.get('backends', [])):
        if not b.get('enabled', True):
            continue
        overrides = {}
        for m in b.get('models', []) or []:
            if isinstance(m, dict) and m.get('id'):
                overrides[m['id']] = m
        backends.append({
            'name': b.get('name', f'backend-{i + 1}'),
            'base_url': b['base_url'].rstrip('/'),
            'api_key': b.get('api_key', ''),
            'include': b.get('include', []),
            'exclude': b.get('exclude', []),
            'model_meta': b.get('model_meta', {}),
            'overrides': overrides,
        })
    return backends


def _headers(backend):
    if backend['api_key']:
        return {'Authorization': f"Bearer {backend['api_key']}"}
    return {}


def _glob_any(model_id, patterns):
    return any(fnmatch.fnmatch(model_id, p) for p in patterns or [])


async def _scan_backends(client, backends):
    """Опрашивает бэкенды в порядке конфигурации. Возвращает {id: (backend, meta)}."""
    claimed = {}
    down = []
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=CLIENT_TIMEOUT)
    for backend in backends:
        try:
            resp = await client.get(
                f"{backend['base_url']}/models", headers=_headers(backend)
            )
            resp.raise_for_status()
            ids = [
                m['id'] for m in resp.json().get('data', [])
                if isinstance(m, dict) and m.get('id')
            ]
        except Exception as exc:
            down.append(f"{backend['name']}: {type(exc).__name__}: {exc}")
            continue
        for model_id in ids:
            if model_id in claimed:
                continue  # приоритет: первый по порядку конфигурации
            if backend['include'] and not _glob_any(model_id, backend['include']):
                continue
            if _glob_any(model_id, backend['exclude']):
                continue
            override = backend['overrides'].get(model_id, {})
            meta = dict(backend['model_meta'])
            meta.update(override.get('meta', {}))
            claimed[model_id] = (backend, meta)
    if own_client:
        await client.aclose()
    return claimed, down


async def _resolve(force=False):
    """Возвращает (backends, {id: (backend, meta)}, down) из кэша или свежим сканом."""
    async with _state['lock']:
        if not force and time.time() - _state['ts'] < CACHE_TTL:
            return _state['backends'], _state['map'], _state['down']
        cfg = _load_config()
        backends = _compile_backends(cfg)
        model_map, down = await _scan_backends(_state['client'], backends)
        _state.update(ts=time.time(), map=model_map, backends=backends, down=down)
        return backends, model_map, down


@asynccontextmanager
async def lifespan(_app):
    _state['client'] = httpx.AsyncClient(timeout=CLIENT_TIMEOUT)
    yield
    await _state['client'].aclose()


app = FastAPI(title='Model Bridge', docs_url=None, redoc_url=None)
app.router.lifespan_context = lifespan


def _check_auth(request: Request):
    if not API_AUTH_KEY:
        return
    auth = request.headers.get('authorization', '')
    if auth != f'Bearer {API_AUTH_KEY}':
        raise HTTPException(status_code=401, detail='Unauthorized')


@app.get('/health')
async def health():
    return {'status': 'ok'}


@app.get('/debug/connect')
async def debug_connect():
    import traceback
    results = {}
    async with httpx.AsyncClient(timeout=5.0) as c:
        for url in ('http://gemini-cli-api:8765/v1/models', 'http://gpt-api:8317/v1/models'):
            try:
                r = await c.get(url)
                results[url] = f'HTTP {r.status_code}'
            except Exception as exc:
                tb = ''.join(traceback.format_exception(type(exc), exc, exc.__cause__)[-3:])
                results[url] = f'{type(exc).__name__}: {exc} | cause: {tb[-200:]}'
    return results


@app.get('/v1/models')
async def list_models(request: Request):
    _check_auth(request)
    _, model_map, down = await _resolve()
    data = []
    for model_id in sorted(model_map):
        backend, meta = model_map[model_id]
        entry = {'id': model_id, 'object': 'model', 'owned_by': backend['name']}
        if meta:
            entry['meta'] = meta
        data.append(entry)
    payload = {'object': 'list', 'data': data}
    if down:
        payload['bridge'] = {'backends_down': down}
    return JSONResponse(payload)


@app.post('/v1/chat/completions')
async def chat_completions(request: Request):
    _check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON body')

    model_id = body.get('model')
    if not model_id:
        raise HTTPException(status_code=400, detail='model required')

    _, model_map, down = await _resolve()
    route = model_map.get(model_id)
    if not route:
        detail = f"Model '{model_id}' not found"
        if down:
            detail += f"; backends down: {', '.join(down)}"
        return JSONResponse(
            {'error': {'message': detail, 'type': 'bridge_model_not_found'}},
            status_code=404,
        )

    backend, _meta = route
    upstream_model = backend['overrides'].get(model_id, {}).get('upstream_model', model_id)
    payload = dict(body)
    payload['model'] = upstream_model
    headers = {'Content-Type': 'application/json', **_headers(backend)}
    url = f"{backend['base_url']}/chat/completions"
    client = _state['client']

    tool_payload = 'tools' in payload or 'tool_choice' in payload

    async def _stream_attempt(body):
        upstream = client.build_request('POST', url, json=body, headers=headers)
        return await client.send(upstream, stream=True)

    try:
        if body.get('stream'):
            resp = await _stream_attempt(payload)
            fallback = False
            # Не все каналы переваривают инструменты: повторяем без них.
            if resp.status_code >= 400 and tool_payload:
                await resp.aclose()
                stripped = {k: v for k, v in payload.items() if k not in ('tools', 'tool_choice')}
                resp = await _stream_attempt(stripped)
                fallback = True
            if resp.status_code >= 400:
                raw = (await resp.aread()).decode(errors='replace')[:400]
                await resp.aclose()
                return JSONResponse(
                    {'error': {
                        'message': f"backend '{backend['name']}' error: {raw}",
                        'type': 'bridge_backend_error',
                    }},
                    status_code=max(resp.status_code, 500),
                )
            extra_headers = {'X-Bridge-Backend': backend['name']}
            if fallback:
                extra_headers['X-Bridge-Fallback'] = 'tools-stripped'
            return StreamingResponse(
                resp.aiter_raw(),
                status_code=resp.status_code,
                media_type=resp.headers.get('content-type', 'text/event-stream'),
                headers=extra_headers,
            )
        stripped_payload = {k: v for k, v in payload.items() if k not in ('tools', 'tool_choice')}
        resp = await client.post(url, json=stripped_payload, headers=headers)
        fallback = 'tools' in payload
    except httpx.HTTPError as exc:
        return JSONResponse(
            {'error': {
                'message': f"backend '{backend['name']}' unavailable: {type(exc).__name__}",
                'type': 'bridge_backend_error',
            }},
            status_code=502,
        )

    if resp.status_code >= 400:
        return JSONResponse(
            {'error': {
                'message': f"backend '{backend['name']}' error: {resp.text[:400]}",
                'type': 'bridge_backend_error',
            }},
            status_code=max(resp.status_code, 500),
        )
    result = json.loads(resp.content)
    if fallback:
        result['bridge'] = {'fallback': 'tools_stripped'}
    return JSONResponse(result)


if __name__ == '__main__':
    uvicorn.run(app, host=HOST, port=PORT)
