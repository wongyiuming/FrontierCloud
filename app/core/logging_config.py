from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings


request_id_context: ContextVar[str] = ContextVar("request_id", default="")
trace_id_context: ContextVar[str] = ContextVar("trace_id", default="")


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "component": record.name,
            "event": record.getMessage(),
            "request_id": request_id_context.get(),
            "trace_id": trace_id_context.get(),
            "instance": settings.INSTANCE_NAME,
        }
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload.update(context)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        context = getattr(record, "context", None)
        suffix = f" {context}" if isinstance(context, dict) and context else ""
        return (
            f"{datetime.fromtimestamp(record.created, timezone.utc).isoformat()} "
            f"{record.levelname} {record.name} request_id={request_id_context.get() or '-'} "
            f"trace_id={trace_id_context.get() or '-'} {record.getMessage()}{suffix}"
        )


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter() if settings.LOG_FORMAT == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.LOG_LEVEL)
    for name in ("uvicorn", "uvicorn.error"):
        child = logging.getLogger(name)
        child.handlers.clear()
        child.propagate = True
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True


def bind_request_context(request_id: str, trace_id: str) -> tuple[Token[str], Token[str]]:
    return request_id_context.set(request_id), trace_id_context.set(trace_id)


def reset_request_context(tokens: tuple[Token[str], Token[str]]) -> None:
    request_id_context.reset(tokens[0])
    trace_id_context.reset(tokens[1])
