"""Logging com Rich — cores e tracebacks legíveis (local + Render com FORCE_COLOR=1)."""

from __future__ import annotations

import logging
import os
import sys

from rich.console import Console
from rich.logging import RichHandler


def _stderr_console() -> Console:
    # Render muitas vezes não é TTY; FORCE_COLOR=1 mantém ANSI nos logs web.
    force = os.getenv("FORCE_COLOR", "").strip().lower() in ("1", "true", "yes") or sys.stderr.isatty()
    try:
        width = int(os.getenv("LOG_WIDTH", "120"))
    except ValueError:
        width = 120
    width = max(80, min(width, 200))
    return Console(stderr=True, force_terminal=force, width=width, highlight=False)


def rich_handler_factory(*args: object, **kwargs: object) -> RichHandler:
    """Factory para logging.config.dictConfig (`\"()\": \"maria_logging.rich_handler_factory\"`)."""
    return RichHandler(
        console=_stderr_console(),
        show_time=True,
        show_path=False,
        rich_tracebacks=True,
        markup=False,
        tracebacks_show_locals=False,
        omit_repeated_times=False,
    )


def quiet_noisy_loggers() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)


# Configuração consumida por uvicorn.run(..., log_config=UVICORN_RICH_LOG_CONFIG)
UVICORN_RICH_LOG_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "rich": {
            "format": "%(message)s",
            "datefmt": "%H:%M:%S",
        },
    },
    "handlers": {
        "rich": {
            "()": "maria_logging.rich_handler_factory",
            "formatter": "rich",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["rich"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["rich"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["rich"], "level": "INFO", "propagate": False},
        "maria": {"handlers": ["rich"], "level": "INFO", "propagate": False},
        "tools_maria": {"handlers": ["rich"], "level": "INFO", "propagate": False},
        "uazapi_webhook": {"handlers": ["rich"], "level": "INFO", "propagate": False},
        "maria_admin_api": {"handlers": ["rich"], "level": "INFO", "propagate": False},
    },
    "root": {"handlers": ["rich"], "level": "INFO"},
}
