"""API над Grok CLI. Модели и провайдер — из окружения (env)."""

import asyncio
import json
import os
import re
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import provider_models

GROK_BIN = os.getenv('GROK_BIN', 'grok')
GROK_CONFIG_DIR = os.getenv('GROK_CONFIG_DIR', str(Path.home() / '.grok'))
TIMEOUT = float(os.getenv('GROK_API_TIMEOUT', '240'))
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', '8090'))
SEM = asyncio.Semaphore(int(os.getenv('MAX_CONCURRENCY', '4')))


def label_for(model_id):
    """Имя личности агента = семейство модели (Claude/Grok/Qwen/...)."""
    m = re.match(r'[a-z]+', model_id.lower())
    prefix = m.group(0) if m else 'AI'
    return {'gpt': 'GPT', 'glm': 'GLM', 'deepseek': 'DeepSeek'}.get(prefix, prefix.capitalize())


# Полный системный промпт агента CLI, но без упоминания компании-разработчика;
# {{LABEL}} заменяется на семейство выбранной модели.
try:
    AGENT_PROMPT_TEMPLATE = (Path(__file__).parent / 'agent_prompt.txt').read_text(encoding='utf-8')
except OSError:
    AGENT_PROMPT_TEMPLATE = ''

app = FastAPI(title='grok-cli-api')


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


async def _run_grok(prompt: str, model: str | None = None, effort: str | None = None):
    env = dict(os.environ)
    env['HOME'] = GROK_CONFIG_DIR.rsplit('/.grok', 1)[0] if '/.grok' in GROK_CONFIG_DIR else str(Path.home())
    env.setdefault('TERM', 'dumb')
    args = [GROK_BIN]
    if model:
        args += ['-m', model]
    if effort:
        args += ['--effort', effort]
    if AGENT_PROMPT_TEMPLATE:
        # компания-разработчик из встроенного шаблона не нужна — только имя модели
        args += ['--system-prompt-override', AGENT_PROMPT_TEMPLATE.replace('{{LABEL}}', label_for(model or ''))]
    args += ['-p', prompt]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd='/tmp',
        env=env,
    )
    out_chunks, err_chunks = [], []

    async def _read(stream, sink):
        while True:
            line = await stream.readline()
            if not line:
                break
            sink.append(line.decode(errors='replace'))

    try:
        await asyncio.wait_for(
            asyncio.gather(_read(proc.stdout, out_chunks), _read(proc.stderr, err_chunks)),
            timeout=TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail='grok CLI timeout')
    code = await proc.wait()
    text = ''.join(out_chunks).strip()
    errors = ''.join(err_chunks).strip()
    if code != 0:
        # CLI печатает одну ошибку дважды (лог + финальная строка) — дубли убираем
        detail = errors or text
        lines, seen = [], set()
        for line in detail.splitlines():
            clean = line.strip().removeprefix('Error: ').strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            lines.append(clean)
        raise HTTPException(status_code=502, detail='\n'.join(lines)[-400:] or f'grok CLI exited {code}')
    return text


@app.get('/health')
async def health():
    return {'status': 'ok'}


@app.get('/v1/models')
async def models():
    return {'object': 'list', 'data': [{'id': m, 'object': 'model', 'owned_by': 'grok-cli'} for m in provider_models.get_models()]}


@app.post('/v1/chat/completions')
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get('model')
    if not provider_models.is_supported(model):
        raise HTTPException(status_code=400, detail=f'Unsupported model: {model}')
    messages = body.get('messages') or []
    if not messages:
        raise HTTPException(status_code=400, detail='messages required')
    stream = bool(body.get('stream'))
    prompt = _build_prompt(messages)
    effort = body.get('reasoning_effort')
    effort = effort if isinstance(effort, str) and effort else None
    created = int(time.time())

    if not stream:
        async with SEM:
            text = await _run_grok(prompt, model, effort)
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
                text = await _run_grok(prompt, model, effort)
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
