"""Мост: один API для всех моделей. Маршрутизация — в config.yaml."""

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
TOOLS_STATE_PATH = os.getenv('TOOLS_STATE_PATH', '/app/data/tools_state.json')
TOOLS_RETRY_TTL = float(os.getenv('TOOLS_RETRY_TTL_DAYS', '7')) * 86400


def _load_tools_state():
    try:
        with open(TOOLS_STATE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_tools_state(state):
    os.makedirs(os.path.dirname(TOOLS_STATE_PATH), exist_ok=True)
    with open(TOOLS_STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f)


def _tools_marked(model_id):
    state = _load_tools_state()
    mark = state.get(model_id)
    if not mark:
        return False
    if mark.get('until', 0) <= time.time():
        state.pop(model_id)
        _save_tools_state(state)
        return False
    return True


def _mark_tools_failed(model_id):
    state = _load_tools_state()
    state[model_id] = {'until': time.time() + TOOLS_RETRY_TTL, 'at': int(time.time())}
    _save_tools_state(state)


def _clear_tools_mark(model_id):
    state = _load_tools_state()
    if model_id in state:
        state.pop(model_id)
        _save_tools_state(state)
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
    'down': [],
}


def _load_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return yaml.safe_load(f.read()) or {}


def _compile_backends(cfg):
    backends = []
    for i, b in enumerate(cfg.get('backends', [])):
        if not b.get('enabled', True):
            continue
        backends.append({
            'name': b.get('name', f'backend-{i + 1}'),
            'base_url': b['base_url'].rstrip('/'),
            'api_key': b.get('api_key', ''),
            'include': b.get('include', []),
            'exclude': b.get('exclude', []),
        })
    return backends


def _headers(backend):
    if backend['api_key']:
        return {'Authorization': f"Bearer {backend['api_key']}"}
    return {}


def _glob_any(model_id, patterns):
    return any(fnmatch.fnmatch(model_id, p) for p in patterns or [])


async def _scan_backends(client, backends):
    """Опрашивает бэкенды в порядке конфигурации. Возвращает {id: backend}."""
    claimed = {}
    down = []
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
                continue  # кто первый — тот и владеет моделью
            if backend['include'] and not _glob_any(model_id, backend['include']):
                continue
            if _glob_any(model_id, backend['exclude']):
                continue
            claimed[model_id] = backend
    return claimed, down


async def _resolve():
    """Возвращает ({id: backend}, down) из кэша или свежим сканом."""
    async with _state['lock']:
        if time.time() - _state['ts'] < CACHE_TTL:
            return _state['map'], _state['down']
        backends = _compile_backends(_load_config())
        model_map, down = await _scan_backends(_state['client'], backends)
        _state.update(ts=time.time(), map=model_map, down=down)
        return model_map, down


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


@app.get('/v1/models')
async def list_models(request: Request):
    _check_auth(request)
    model_map, down = await _resolve()
    data = []
    for model_id in sorted(model_map):
        backend = model_map[model_id]
        data.append({'id': model_id, 'object': 'model', 'owned_by': backend['name']})
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

    model_map, down = await _resolve()
    backend = model_map.get(model_id)
    if not backend:
        detail = f"Model '{model_id}' not found"
        if down:
            detail += f"; backends down: {', '.join(down)}"
        return JSONResponse(
            {'error': {'message': detail, 'type': 'bridge_model_not_found'}},
            status_code=404,
        )

    payload = dict(body)
    headers = {'Content-Type': 'application/json', **_headers(backend)}
    url = f"{backend['base_url']}/chat/completions"
    client = _state['client']

    tool_payload = 'tools' in payload or 'tool_choice' in payload
    tools_blocked = tool_payload and _tools_marked(model_id)

    async def _attempt(body):
        upstream = client.build_request('POST', url, json=body, headers=headers)
        return await client.send(upstream, stream=True)

    try:
        if body.get('stream'):
            attempt = (
                {k: v for k, v in payload.items() if k not in ('tools', 'tool_choice')}
                if tools_blocked else payload
            )
            resp = await _attempt(attempt)
            fallback = tools_blocked
            # не сработало с инструментами — запоминаем и пробуем без них
            if resp.status_code >= 400 and tool_payload:
                _mark_tools_failed(model_id)
                await resp.aclose()
                stripped = {k: v for k, v in payload.items() if k not in ('tools', 'tool_choice')}
                resp = await _attempt(stripped)
                fallback = True
            elif resp.status_code < 400 and tool_payload and not tools_blocked:
                _clear_tools_mark(model_id)  # инструменты дошли до бэкенда и приняты
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

        attempt = (
            {k: v for k, v in payload.items() if k not in ('tools', 'tool_choice')}
            if tools_blocked else payload
        )
        resp = await client.post(url, json=attempt, headers=headers)
        fallback = tools_blocked
        # не сработало с инструментами — запоминаем и пробуем без них
        if resp.status_code >= 400 and tool_payload:
            _mark_tools_failed(model_id)
            stripped = {k: v for k, v in payload.items() if k not in ('tools', 'tool_choice')}
            resp = await client.post(url, json=stripped, headers=headers)
            fallback = True
        elif resp.status_code < 400 and tool_payload and not tools_blocked:
            _clear_tools_mark(model_id)  # инструменты дошли до бэкенда и приняты
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
