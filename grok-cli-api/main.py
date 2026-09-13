"""API над Grok CLI. Модели и провайдер — из окружения (env)."""

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

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
    """Имя личности агента: красивое название от провайдера (без компании-префикса),
    иначе id модели с тире вместо пробелов."""
    name = provider_models.get_display_name(model_id)
    if name and '·' in name:
        name = name.split('·')[-1].strip()
    return name or model_id.replace('-', ' ')


# Полный системный промпт агента CLI, но без упоминания компании-разработчика;
# {{LABEL}} заменяется на семейство выбранной модели.
try:
    AGENT_PROMPT_TEMPLATE = (Path(__file__).parent / 'agent_prompt.txt').read_text(encoding='utf-8')
except OSError:
    AGENT_PROMPT_TEMPLATE = ''

# Самоадаптация: если стриминг chat_completions у модели нестандартный
# (ошибка сериализации), секция модели в конфиге CLI переключается на
# Responses API — без ручных списков.
_flipped = set()
CONFIG_TOML = Path(GROK_CONFIG_DIR) / 'config.toml'


def _flip_to_responses(model):
    _flipped.add(model)
    try:
        text = CONFIG_TOML.read_text(encoding='utf-8')
        m = re.search(rf'(\[model\."{re.escape(model)}"\][^\[]*)', text)
        if m:
            text = text.replace(
                m.group(1),
                m.group(1).replace('api_backend = "chat_completions"', 'api_backend = "responses"'),
            )
            CONFIG_TOML.write_text(text, encoding='utf-8')
    except OSError:
        pass


async def _run_adaptive(model, effort, prompt):
    try:
        return await _run_grok(prompt, model, effort)
    except HTTPException as exc:
        if (model not in _flipped and exc.status_code == 502
                and 'serialization error' in str(exc.detail)):
            _flip_to_responses(model)
            return await _run_grok(prompt, model, effort)
        raise

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


async def _grok_events(model, effort, prompt):
    """Запускает CLI в streaming-json. Выдаёт события:
    ('thought'|'text', дельта), ('end', session_id), ('failed', текст ошибки)."""
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
    args += ['--output-format', 'streaming-json', '-p', prompt]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd='/tmp',
        env=env,
    )
    err_chunks = []

    async def _drain_err():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            err_chunks.append(line.decode(errors='replace'))

    err_task = asyncio.create_task(_drain_err())
    deadline = time.monotonic() + TIMEOUT
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                yield 'failed', 'grok CLI timeout'
                return
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                proc.kill()
                yield 'failed', 'grok CLI timeout'
                return
            if not line:
                break
            try:
                event = json.loads(line.decode(errors='replace'))
            except Exception:
                continue
            etype = event.get('type')
            if etype == 'end':
                await proc.wait()
                yield 'end', event.get('sessionId')
                return
            if etype in ('thought', 'text'):
                data = event.get('data')
                if isinstance(data, str) and data:
                    yield etype, data
        code = await proc.wait()
        if code != 0:
            # CLI упал без end-события — отдаём stderr (дубли убираем)
            errors = ''.join(err_chunks).strip()
            lines, seen = [], set()
            for line in errors.splitlines():
                clean = line.strip().removeprefix('Error: ').strip()
                if clean and clean not in seen:
                    seen.add(clean)
                    lines.append(clean)
            yield 'failed', '\n'.join(lines)[-400:] or f'grok CLI exited {code}'
        else:
            yield 'end', None
    finally:
        err_task.cancel()


async def _run_adaptive_events(model, effort, prompt):
    """Самоадаптация: serialization error в chat_completions — переключаем
    секцию модели на Responses API и повторяем один раз."""
    retried = False
    async for kind, data in _grok_events(model, effort, prompt):
        if kind == 'failed' and not retried and 'serialization error' in data:
            retried = True
            _flip_to_responses(model)
            async for kind, data in _grok_events(model, effort, prompt):
                yield kind, data
            return
        yield kind, data


def _session_split(session_id):
    """Из истории сессии CLI: (финальный ответ, реплики-рассуждения агента)."""
    if not session_id:
        return None, None
    path = Path(GROK_CONFIG_DIR) / 'sessions' / quote('/tmp', safe='') / session_id / 'chat_history.jsonl'
    for _ in range(3):
        try:
            if path.exists():
                assistant = []
                for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
                    try:
                        m = json.loads(line)
                    except Exception:
                        continue
                    if m.get('role') == 'assistant':
                        content = m.get('content')
                        if isinstance(content, str) and content.strip():
                            assistant.append(content.strip())
                if assistant:
                    return assistant[-1], assistant[:-1]
                return None, None
        except OSError:
            pass
        time.sleep(0.3)
    return None, None


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
            thought, narration, text_buf = [], [], []
            session_id, failed = None, None
            async for kind, data in _run_adaptive_events(model, effort, prompt):
                if kind == 'thought':
                    thought.append(data)
                elif kind == 'text':
                    text_buf.append(data)
                elif kind == 'end':
                    session_id = data
                elif kind == 'failed':
                    failed = data
            if failed:
                raise HTTPException(status_code=502, detail=failed)
        final, agent_notes = _session_split(session_id)
        agent_notes = agent_notes or []
        if final is None:
            final = ''.join(text_buf).strip()
        reasoning = ''.join(thought)
        for part in agent_notes:
            reasoning += ('\n\n' + part if reasoning else part)
        text = (f'<think>{reasoning}</think>' if reasoning.strip() else '') + final
        return JSONResponse({
            'id': f'chatcmpl-grok-{created}',
            'object': 'chat.completion',
            'created': created,
            'model': model,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
        })

    async def sse():
        reasoning_open = False
        text_buf, thought = [], []
        session_id, failed = None, None

        def chunk(delta, finish=None):
            return b'data: ' + json.dumps({
                'id': f'chatcmpl-grok-{created}',
                'object': 'chat.completion.chunk',
                'created': created,
                'model': model,
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}],
            }, ensure_ascii=False).encode() + b'\n\n'

        try:
            async with SEM:
                async for kind, data in _run_adaptive_events(model, effort, prompt):
                    if kind == 'thought':
                        if not reasoning_open:
                            reasoning_open = True
                            yield chunk({'role': 'assistant', 'content': '<think>'})
                        yield chunk({'content': data})
                    elif kind == 'text':
                        text_buf.append(data)
                    elif kind == 'end':
                        session_id = data
                    elif kind == 'failed':
                        failed = data
        except HTTPException as exc:
            failed = str(exc.detail)

        final, agent_notes = _session_split(session_id)
        agent_notes = agent_notes or []
        if agent_notes:
            if not reasoning_open:
                reasoning_open = True
                yield chunk({'role': 'assistant', 'content': '<think>'})
            yield chunk({'content': '\n\n' + '\n\n'.join(agent_notes)})
        if reasoning_open:
            yield chunk({'content': '</think>'})
        if failed:
            yield chunk({'content': failed})
        answer = final if final is not None else ''.join(text_buf)
        if answer:
            yield chunk({'content': answer})
        yield chunk({}, 'stop')
        yield b'data: [DONE]\n\n'

    return StreamingResponse(sse(), media_type='text/event-stream')


if __name__ == '__main__':
    uvicorn.run(app, host=HOST, port=PORT)
