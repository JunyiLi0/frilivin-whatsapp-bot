"""Structured JSON logging shared by the api and the worker.

Everything — including uvicorn's and RQ's own records — is funnelled through
structlog's ``ProcessorFormatter`` so that a single line of stdout is always a
single JSON object. The bridge (Node/pino) emits the same field names.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

# Loggers whose records must reach the root handler instead of their own.
_ADOPTED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "rq",
    "rq.worker",
    "httpx",
)


def _service_processor(service: str) -> Processor:
    def add_service(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        return event_dict

    return add_service


def configure_logging(service: str, level: str = "info") -> None:
    """Install the JSON logging pipeline. Safe to call more than once."""
    level_no = _LEVELS.get(level.lower(), logging.INFO)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _service_processor(service),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
        )
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level_no)

    for name in _ADOPTED_LOGGERS:
        adopted = logging.getLogger(name)
        adopted.handlers = []
        adopted.propagate = True


def get_logger(name: str | None = None, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, optionally pre-loaded with context."""
    logger = structlog.stdlib.get_logger(name)
    if initial:
        logger = logger.bind(**initial)
    return logger


def redact(payload: MutableMapping[str, Any], *keys: str) -> dict[str, Any]:
    """Copy ``payload`` with ``keys`` masked — used before logging raw bodies."""
    safe = dict(payload)
    for key in keys:
        if key in safe:
            safe[key] = "***"
    return safe
