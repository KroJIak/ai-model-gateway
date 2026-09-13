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
    ('thought'|'text', дельта), ('end', None), ('failed', текст ошибки)."""
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
                yield 'end', None
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


class _ReasoningSplit:
    """Делит поток CLI: мышление агента и реплики между инструментами —
    рассуждения (<think>), текст после последней мысли — финальный ответ."""

    def __init__(self):
        self.reasoning = ''       # готовый текст рассуждений
        self.mode = None          # 'thought' | 'narration'
        self.pending = []         # неклассифицированные текстовые дельты
        self.failed = None

    def feed_thought(self, data):
        if self.pending:
            self._flush_pending_as_narration()
        if self.mode != 'thought':
            self.reasoning += ('\n\n' if self.reasoning else '')
            self.mode = 'thought'
        self.reasoning += data

    def feed_text(self, data):
        self.pending.append(data)

    def _flush_pending_as_narration(self):
        part = ''.join(self.pending)
        self.pending = []
        if not part.strip():
            return
        if self.mode != 'narration':
            self.reasoning += ('\n\n' if self.reasoning else '')
            self.mode = 'narration'
        self.reasoning += part

    def finish(self):
        """Финальный ответ = хвост pending после последней мысли;
        если хвост пуст — последняя реплика агента."""
        if self.pending:
            self._flush_pending_as_narration()
        tail = ''
        if self.mode == 'narration' and self.reasoning:
            idx = self.reasoning.rfind('\n\n')
            tail = self.reasoning[idx + 2:]
            self.reasoning = self.reasoning[:idx]
        return tail

    def failed_with(self, detail):
        self.failed = detail


app = FastAPI(title='grok-cli-api')


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
            split = _ReasoningSplit()
            async for kind, data in _run_adaptive_events(model, effort, prompt):
                if kind == 'thought':
                    split.feed_thought(data)
                elif kind == 'text':
                    split.feed_text(data)
                elif kind == 'failed':
                    split.failed_with(data)
            if split.failed:
                raise HTTPException(status_code=502, detail=split.failed)
            tail = split.finish()
            text = (f'<think>{split.reasoning}</think>' if split.reasoning else '') + tail
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
        pending = []
        split = _ReasoningSplit()
        failed = None

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
                        flushed = split.feed_thought(data)
                        if flushed:
                            if not reasoning_open:
                                reasoning_open = True
                                yield chunk({'role': 'assistant', 'content': '<think>'})
                            yield chunk({'content': flushed})
                        if not reasoning_open:
                            reasoning_open = True
                            yield chunk({'role': 'assistant', 'content': '<think>'})
                        yield chunk({'content': data})
                    elif kind == 'text':
                        split.feed_text(data)
                    elif kind == 'failed':
                        failed = data
        except HTTPException as exc:
            failed = str(exc.detail)

        if reasoning_open:
            yield chunk({'content': '</think>'})
        if split.pending:
            yield chunk({'content': ''.join(split.pending)})
        if failed:
            yield chunk({'content': failed})
        yield chunk({}, 'stop')
        yield b'data: [DONE]\n\n'

    return StreamingResponse(sse(), media_type='text/event-stream')


if __name__ == '__main__':
    uvicorn.run(app, host=HOST, port=PORT)
