"""API над Grok CLI. Модели и провайдер — из окружения (env)."""

import asyncio
import json
import os
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

GROK_BIN = os.getenv('GROK_BIN', 'grok')
GROK_CONFIG_DIR = os.getenv('GROK_CONFIG_DIR', str(Path.home() / '.grok'))
import provider_models
TIMEOUT = float(os.getenv('GROK_API_TIMEOUT', '180'))
API_AUTH_KEY = os.getenv('API_AUTH_KEY')
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', '8090'))
SEM = asyncio.Semaphore(int(os.getenv('MAX_CONCURRENCY', '4')))

app = FastAPI(title='grok-cli-api')


def _check_auth(request: Request):
    if not API_AUTH_KEY:
        return
    auth = request.headers.get('authorization', '')
    if auth != f'Bearer {API_AUTH_KEY}':
        raise HTTPException(status_code=401, detail='Unauthorized')


def _build_prompt(messages) -> str:
    """Flatten an OpenAI message list into a single instruction prompt."""
    parts = []
    for m in messages:
        role = m.get('role')
        content = m.get('content')
        if isinstance(content, list):  # multimodal: keep text parts only
            content = ' '.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
        if not content:
            continue
        if role == 'system':
            parts.append(f'[System instructions]\n{content}')
        elif role == 'assistant':
            parts.append(f'[Previous assistant reply]\n{content}')
        else:
            parts.append(str(content))
    return '\n\n'.join(parts)


async def _run_grok(prompt: str):
    env = dict(os.environ)
    env['HOME'] = GROK_CONFIG_DIR.rsplit('/.grok', 1)[0] if '/.grok' in GROK_CONFIG_DIR else str(Path.home())
    env.setdefault('TERM', 'dumb')
    proc = await asyncio.create_subprocess_exec(
        GROK_BIN, '-p', prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd='/tmp',
        env=env,
    )
    chunks = []

    async def _read():
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            chunks.append(line.decode(errors='replace'))

    try:
        await asyncio.wait_for(_read(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail='grok CLI timeout')
    code = await proc.wait()
    text = ''.join(chunks).strip()
    if code != 0:
        raise HTTPException(status_code=502, detail=text[-400:] or f'grok CLI exited {code}')
    return text


@app.get('/health')
async def health():
    return {'status': 'ok'}


@app.get('/v1/models')
async def models(request: Request):
    _check_auth(request)
    return {'object': 'list', 'data': [{'id': m, 'object': 'model', 'owned_by': 'grok-cli'} for m in provider_models.get_grok_models()]}


@app.post('/v1/chat/completions')
async def chat_completions(request: Request):
    _check_auth(request)
    body = await request.json()
    model = body.get('model', 'grok-4.6')
    if not provider_models.is_supported(model):
        raise HTTPException(status_code=400, detail=f'Unsupported model: {model}')
    messages = body.get('messages') or []
    if not messages:
        raise HTTPException(status_code=400, detail='messages required')
    stream = bool(body.get('stream'))
    prompt = _build_prompt(messages)
    created = int(time.time())

    async with SEM:
        if not stream:
            text = await _run_grok(prompt)
            return JSONResponse({
                'id': f'chatcmpl-grok-{created}',
                'object': 'chat.completion',
                'created': created,
                'model': model,
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
            })

        async def sse():
            try:
                async with SEM:
                    text = await _run_grok(prompt)
            except HTTPException as exc:
                payload = json.dumps({'error': {'message': exc.detail}}).encode()
                yield b'data: ' + payload + b'\n\n'
                yield b'data: [DONE]\n\n'
                return
            base = {
                'id': f'chatcmpl-grok-{created}',
                'object': 'chat.completion.chunk',
                'created': created,
                'model': model,
            }
            for i in range(0, len(text), 160):
                chunk = dict(base)
                chunk['choices'] = [{'index': 0, 'delta': {'content': text[i:i + 160]}, 'finish_reason': None}]
                yield b'data: ' + json.dumps(chunk, ensure_ascii=False).encode() + b'\n\n'
            chunk = dict(base)
            chunk['choices'] = [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]
            yield b'data: ' + json.dumps(chunk).encode() + b'\n\n'
            yield b'data: [DONE]\n\n'

        return StreamingResponse(sse(), media_type='text/event-stream')


if __name__ == '__main__':
    uvicorn.run(app, host=HOST, port=PORT)
