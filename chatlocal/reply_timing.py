"""Bounded local timing receipts. Never persist text, URLs, tokens or images.

Nested spans overlap; their durations must not be summed. Host/model time is
outside this process and can only be observed as gaps between tool receipts.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import json
from pathlib import Path
import threading
import time
import uuid

from .config import DATA

PATH = DATA / 'diagnostics' / 'reply-timing.jsonl'
MAX_BYTES = 8 * 1024 * 1024
_current = ContextVar('reply_timing', default=None)
_lock = threading.Lock()
_IDS = ('session_id', 'batch_id', 'plan_id', 'event_id', 'index')


def note(**fields):
    receipt = _current.get()
    if receipt is not None:
        # Callers supply only explicitly enumerated timing/identity metadata.
        receipt['marks'].append(dict(at=time.time(), **fields))
        del receipt['marks'][:-256]


def _summary(value):
    if not isinstance(value, dict):
        return {}
    result = {k: value[k] for k in (*_IDS, 'state', 'error_code', 'event') if k in value}
    batch = value.get('input_batch')
    if isinstance(batch, dict):
        result['batch_id'] = batch.get('id')
        result['collection_reason'] = batch.get('collection', {}).get('reason')
    messages = value.get('messages')
    if isinstance(messages, list):
        result['message_count'] = len(messages)
        result['event_ids'] = [m['id'] for m in messages if 'id' in m][:50]
        times = [m['received_at'] for m in messages if isinstance(m.get('received_at'), (int, float))]
        if times:
            result.update(first_received_at=min(times), last_received_at=max(times))
    return result


@contextmanager
def span(name):
    receipt = _current.get()
    if receipt is None:
        yield
        return
    start, tick = time.time(), time.perf_counter()
    # A long poll can check hundreds of times. Preserve the entire wait without
    # letting polling spans crowd out the later batch/serialization stages.
    if name in ('wait.event_sleep', 'chat.validate'):
        try:
            yield
        finally:
            aggregate = receipt['polling'].setdefault(name, dict(count=0, seconds=0.0))
            aggregate['count'] += 1
            aggregate['seconds'] = round(aggregate['seconds']+time.perf_counter()-tick, 6)
        return
    parent = receipt['stack'][-1] if receipt['stack'] else None
    index = len(receipt['spans'])
    if index >= 512:
        receipt['truncated'] = True
        yield
        return
    item = dict(name=name, start=start, parent=parent)
    receipt['spans'].append(item)
    receipt['stack'].append(index)
    try:
        yield
    except BaseException as exc:
        item['error_type'] = type(exc).__name__
        raise
    finally:
        item['seconds'] = round(time.perf_counter()-tick, 6)
        receipt['stack'].pop()


def stage(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with span(name):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def _write(receipt):
    # Observability must never change dispatch success or trigger write retries.
    try:
        receipt = {k:v for k,v in receipt.items() if k != 'stack'}
        line = json.dumps(receipt, ensure_ascii=False, separators=(',', ':')) + '\n'
        with _lock:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            if PATH.exists() and PATH.stat().st_size >= MAX_BYTES:
                older = PATH.with_suffix('.jsonl.2')
                previous = PATH.with_suffix('.jsonl.1')
                if previous.exists():
                    previous.replace(older)
                PATH.replace(previous)
            with PATH.open('a', encoding='utf-8') as stream:
                stream.write(line)
    except Exception:
        pass


def trace(name, *, tool=False):
    def decorate(fn):
        signature = inspect.signature(fn)
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if _current.get() is not None:
                with span(name):
                    return fn(*args, **kwargs)
            bound = signature.bind(*args, **kwargs).arguments
            arguments = bound.get('arguments', {})
            receipt = dict(version=1, trace_id=uuid.uuid4().hex, kind=name,
                           started_at=time.time(), spans=[], marks=[], stack=[], polling={})
            if tool:
                receipt['tool'] = bound['name']
                receipt['links'] = {k:arguments[k] for k in _IDS if k in arguments}
            token = _current.set(receipt)
            tick = time.perf_counter()
            try:
                with span(name):
                    value = fn(*args, **kwargs)
                # MCP CallToolResult exposes the original structured dict.
                result = getattr(value, 'structuredContent', value)
                receipt['result'] = _summary(result)
                receipt['status'] = 'error' if isinstance(result, dict) and result.get('error') else 'returned'
                return value
            except BaseException as exc:
                receipt.update(status='raised', error_type=type(exc).__name__)
                raise
            finally:
                receipt.update(finished_at=time.time(), seconds=round(time.perf_counter()-tick, 6))
                _current.reset(token)
                _write(receipt)
        return wrapped
    return decorate
