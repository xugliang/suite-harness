"""Bounded, content-free diagnostics. No prompts, arguments, HTTP bodies or locals.

This module is opt-in at the deployment boundary and never configures the root
logger. Business/audit decisions must not depend on successful diagnostic I/O.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import functools
import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from suiteharness import observability
from suiteharness.context_labels import CHARACTER_CATEGORIES

_context = contextvars.ContextVar("suite_diagnostics", default={})
_release = "unconfigured"
_logger = logging.getLogger("suiteharness.diagnostics")
_logger.addHandler(logging.NullHandler())
_logger.propagate = False
_ID = re.compile(r"^[\w.:/-]{1,160}$", re.ASCII)
_FIELDS = frozenset("request_id run_id correlation_id product_id channel_id principal_hash iteration model_call_id tool_call_id tool_name route_id profile_id provider_id model attempt retry_limit stage status code reason operation product_filter result_count candidate_count filtered_count limit truncated seconds first_event_seconds input_tokens output_tokens message_count tool_count estimated_input_tokens removed_rounds truncated_tools emitted finish_reason side_effect_committed version input_digest query_digest arguments_digest result_digest source_digest exception_type status_code retryable batch_number business_status event_id".split())
_FIELDS |= frozenset(
    f"{prefix}_{category}_chars"
    for prefix in ("before", "after", "reduced")
    for category in (*CHARACTER_CATEGORIES, "total")
) | frozenset("before_nontext_parts after_nontext_parts character_unit after_known provider_invoked streaming max_output_tokens".split())
_FIELDS |= frozenset({"validation_path", "validation_rule", "validation_constraint"})
_FIELDS |= frozenset({"skill_id", "skill_source", "instructions_sha256", "characters"})


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe(fields):
    result = {}
    for key, value in fields.items():
        if key not in _FIELDS or value is None:
            continue
        if isinstance(value, bool) or isinstance(value, int):
            result[key] = value
        elif isinstance(value, float) and abs(value) < 1e15:
            result[key] = round(value, 6)
        elif isinstance(value, str) and _ID.fullmatch(value):
            result[key] = value
    return result


@contextmanager
def bind(**fields):
    token = _context.set({**_context.get(), **_safe(fields)})
    try:
        yield
    finally:
        _context.reset(token)


def update(**fields):
    _context.set({**_context.get(), **_safe(fields)})


def current_fields() -> dict:
    """Correlation fields only; never prompt or tool content."""
    return dict(_context.get())


def _exception_chain(exc):
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen and len(chain) < 4:
        seen.add(id(exc))
        frames, tb = [], exc.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            # Do not include source lines, locals, absolute host paths or messages.
            name = code.co_filename.replace("\\", "/")
            relative = next((name.split(marker, 1)[1] for marker in (
                "/suiteharness/", "/products/feature_assistant/", "/deployment/"
            ) if marker in name), None)
            if relative and re.fullmatch(r"[a-zA-Z0-9_./-]{1,200}", relative):
                frames.append({"file": relative, "function": code.co_name[:80], "line": tb.tb_lineno})
            tb = tb.tb_next
        item = {"type": type(exc).__name__[:80], "frames": frames[-8:]}
        # Exception text is deliberately never read, even for wrapped HTTP errors.
        from suiteharness.models.errors import ModelProviderError
        if isinstance(exc, ModelProviderError):
            item.update(_safe({"code": exc.code.value, "status_code": exc.status_code,
                               "retryable": exc.retryable}))
        chain.append(item)
        exc = exc.__cause__ if exc.__cause__ is not None else (
            None if exc.__suppress_context__ else exc.__context__)
    return chain


def emit(event: str, *, error: BaseException | None = None, **fields):
    try:
        record = {"schema": "suiteharness.diagnostic.v1",
                  "time": datetime.now(timezone.utc).isoformat(),
                  "version": _release,
                  "event": event if _ID.fullmatch(event) else "invalid_event",
                  **_context.get(), **_safe(fields)}
        if error is not None:
            record["exceptions"] = _exception_chain(error)
        _logger.info(json.dumps(record, ensure_ascii=True, separators=(",", ":")))
        observability.publish("diagnostic", diagnostic=record)
    except Exception:
        _warn()


def _warn():
    try:
        sys.stderr.write('suiteharness diagnostics output unavailable\n')
    except Exception:
        pass


class _FileHandler(RotatingFileHandler):
    def _open(self):
        import os
        fd = os.open(self.baseFilename, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)

    def handleError(self, record):
        _warn()


class _StreamHandler(logging.StreamHandler):
    def handleError(self, record):
        _warn()


def configure(path: Path | None = None, *, max_bytes=10 * 1024 * 1024, backups=5, release="unversioned"):
    """Called by the application entrypoint, not during library import."""
    global _release
    _release = release if isinstance(release, str) and _ID.fullmatch(release) else "unversioned"
    for handler in list(_logger.handlers):
        _logger.removeHandler(handler)
        handler.close()
    _logger.setLevel(logging.INFO)
    _logger.addHandler(_StreamHandler())
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _logger.addHandler(_FileHandler(path, maxBytes=max_bytes, backupCount=backups,
                                           encoding="utf-8"))
        except Exception:
            _warn()
    emit("diagnostics.ready")


def traced_run(function):
    @functools.wraps(function)
    async def wrapped(self, request, *args, **kwargs):
        scope = request.scope
        fields = dict(run_id=request.run_id, product_id=scope.product_id,
                      principal_hash=digest(scope.principal_id),
                      request_id=scope.request_id, correlation_id=scope.correlation_id,
                      channel_id=scope.channel_id)
        started = time.monotonic()
        with bind(**fields):
            emit("run.started")
            try:
                result = await function(self, request, *args, **kwargs)
                failure = getattr(result, "failure", None)
                emit("run.finished", status=result.status.value,
                     code=failure.code.value if failure else None,
                     seconds=time.monotonic() - started)
                return result
            except BaseException as exc:
                emit("run.failed", error=exc, status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                     seconds=time.monotonic() - started)
                raise
    return wrapped


def traced_tool(function):
    @functools.wraps(function)
    async def wrapped(self, request, intent, iteration, *args, **kwargs):
        started = time.monotonic()
        with bind(tool_call_id=intent.call_id, tool_name=intent.tool_name, iteration=iteration):
            observability.capture("tool.request", lambda: {
                "call_id": intent.call_id, "tool_name": intent.tool_name,
                "iteration": iteration,
                "arguments": observability.safe_value(intent.arguments),
            })
            try:
                result = await function(self, request, intent, iteration, *args, **kwargs)
                payload = getattr(result, "result", None)
                business = payload.get("status") if isinstance(payload, dict) else None
                # Only known statuses, never arbitrary strings returned by a tool.
                if not isinstance(business, str) or business not in {"ok", "rejected", "failed", "unknown", "pending", "graded", "sent", "complete", "completed", "already_graded"}:
                    business = None
                count = None
                if isinstance(payload, dict):
                    for key in ("hits", "matches", "results", "citations", "repositories", "documents", "items", "collections"):
                        if isinstance(payload.get(key), list):
                            count = len(payload[key])
                            break
                emit("tool.completed", status=result.status.value, business_status=business,
                     result_count=count, seconds=time.monotonic() - started)
                observability.capture("tool.result", lambda: {
                    "call_id": intent.call_id, "tool_name": intent.tool_name,
                    "iteration": iteration, "status": result.status.value,
                    "arguments": observability.safe_value(intent.arguments),
                    "result": observability.safe_value(payload),
                    "failure": observability.safe_value(getattr(result, "failure", None)),
                    "seconds": time.monotonic() - started,
                })
                return result
            except BaseException as exc:
                emit("tool.interrupted", error=exc, seconds=time.monotonic() - started)
                observability.capture("tool.error", lambda: {
                    "call_id": intent.call_id, "tool_name": intent.tool_name,
                    "iteration": iteration, "error_type": type(exc).__name__,
                    "seconds": time.monotonic() - started,
                })
                raise
    return wrapped


def traced_reply(function):
    @functools.wraps(function)
    async def wrapped(self, message, event):
        started = time.monotonic()
        with bind(request_id=event.request_id, channel_id="feishu", stage="reply_delivery"):
            try:
                result = await function(self, message, event)
                if event.kind.value != "delta":
                    emit("channel.reply_handled", operation=event.kind.value,
                         seconds=time.monotonic() - started)
                return result
            except BaseException as exc:
                emit("channel.reply_failed", error=exc, seconds=time.monotonic() - started)
                raise
    return wrapped


def main(argv=None):
    """Read-only export of a single request; never dump raw audit payloads."""
    parser = argparse.ArgumentParser(description="Export safe diagnostics for one request")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--audit", type=Path)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"request-[a-f0-9]{16,80}", args.request_id):
        parser.error("invalid request ID")
    for path in sorted(args.logs.glob("diagnostics.jsonl*")):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if len(line) > 65536:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("schema") == "suiteharness.diagnostic.v1" and event.get("request_id") == args.request_id:
                    cleaned = {"schema": "suiteharness.diagnostic.v1", **_safe(event)}
                    for key in ("time", "event"):
                        value = event.get(key)
                        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:+-]{1,80}", value):
                            cleaned[key] = value
                    cleaned["exceptions"] = []
                    errors = event.get("exceptions", [])
                    for error in (errors[:4] if isinstance(errors, list) else []):
                        if not isinstance(error, dict):
                            continue
                        item = _safe(error)
                        if isinstance(error.get("type"), str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", error["type"]):
                            item["type"] = error["type"]
                        item["frames"] = []
                        frames = error.get("frames", [])
                        for frame in (frames[-8:] if isinstance(frames, list) else []):
                            if (isinstance(frame, dict) and isinstance(frame.get("line"), int)
                                and all(isinstance(frame.get(k), str) and re.fullmatch(r"[A-Za-z0-9_./<>-]{1,200}", frame[k]) for k in ("file", "function"))):
                                item["frames"].append({k: frame[k] for k in ("file", "function", "line")})
                        cleaned["exceptions"].append(item)
                    print(json.dumps(cleaned, ensure_ascii=False))
    if args.audit:
        with sqlite3.connect(args.audit.resolve().as_uri() + "?mode=ro", uri=True) as db:
            for (raw,) in db.execute("SELECT event_json FROM audit_events WHERE json_extract(event_json,'$.request_id')=? ORDER BY sequence", (args.request_id,)):
                event = json.loads(raw)
                record = {"time": event.get("occurred_at"), "event": event.get("event_type"),
                          **_safe(event), "audit": True, **_safe(event.get("data", {}))}
                print(json.dumps(record, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
