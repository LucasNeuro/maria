"""Contexto por pedido (ex.: WhatsApp) — usado pelas tools ao gravar leads."""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

_lead_ctx: ContextVar[dict[str, Any] | None] = ContextVar("maria_lead_ctx", default=None)


def get_lead_context() -> dict[str, Any] | None:
    return _lead_ctx.get()


@asynccontextmanager
async def lead_request_context(**fields: Any):
    """Anexa metadados ao ciclo de um `agent.arun` (canal, telefone UAZAPI, etc.)."""
    token = _lead_ctx.set(dict(fields))
    try:
        yield
    finally:
        _lead_ctx.reset(token)
