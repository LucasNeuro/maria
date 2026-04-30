"""Pont WhatsApp UAZAPI ↔ Mari: recebe webhook POST e responde com `/send/text`."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from fastapi import Request

from agno.agent import Agent

logger = logging.getLogger(__name__)

UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "").strip().rstrip("/")
UAZAPI_INSTANCE_TOKEN = os.getenv("UAZAPI_INSTANCE_TOKEN", "").strip()
UAZAPI_WEBHOOK_SECRET = os.getenv("UAZAPI_WEBHOOK_SECRET", "").strip()


def _allow_groups() -> bool:
    return os.getenv("UAZAPI_ALLOW_GROUPS", "").strip().lower() in ("1", "true", "yes")


def _collect_dicts(obj: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        found.append(obj)
        for v in obj.values():
            found.extend(_collect_dicts(v))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_collect_dicts(item))
    return found


def _digits(local_part: str) -> str:
    return "".join(c for c in local_part if c.isdigit())


def _parse_content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, dict):
        if isinstance(content.get("extendedTextMessage"), dict):
            t = content["extendedTextMessage"].get("text")
            if isinstance(t, str) and t.strip():
                return t.strip()
        for key in ("conversation", "text", "caption"):
            v = content.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(content, str):
        s = content.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return _parse_content_text(json.loads(s))
            except (json.JSONDecodeError, TypeError):
                pass
        return s
    return ""


def _message_text(msg: dict[str, Any]) -> str:
    t = (msg.get("text") or "").strip()
    if t:
        return t
    return _parse_content_text(msg.get("content"))


def _destination_number_or_jid(msg: dict[str, Any]) -> str | None:
    raw = (msg.get("chatid") or msg.get("wa_chatid") or "").strip()
    if not raw:
        sender = msg.get("sender")
        if isinstance(sender, str) and sender.strip():
            raw = sender.strip()
    if not raw:
        return None
    if msg.get("isGroup"):
        if not _allow_groups():
            logger.info("UAZAPI webhook: ignorando grupo (UAZAPI_ALLOW_GROUPS não ativo)")
            return None
        return raw
    if "@g.us" in raw:
        if not _allow_groups():
            logger.info("UAZAPI webhook: chat de grupo detectado e ignorado")
            return None
        return raw
    local = raw.split("@", 1)[0]
    digits = _digits(local)
    return digits or None


def _pick_incoming_message(body: Any) -> tuple[str | None, str | None]:
    """Última mensagem de texto elegível (evita eco API / fromMe / opcional grupos)."""
    candidates = _collect_dicts(body)
    last_num: str | None = None
    last_text: str | None = None
    for msg in candidates:
        if not isinstance(msg, dict):
            continue
        if msg.get("wasSentByApi"):
            continue
        if msg.get("fromMe"):
            continue
        text = _message_text(msg)
        if not text:
            continue
        dest = _destination_number_or_jid(msg)
        if not dest:
            continue
        last_num, last_text = dest, text
    return last_num, last_text


def _verify_webhook_secret(request: Request) -> bool:
    if not UAZAPI_WEBHOOK_SECRET:
        return True
    got = (
        request.headers.get("x-uazapi-webhook-secret")
        or request.headers.get("X-UAZAPI-WEBHOOK-SECRET")
        or ""
    ).strip()
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        got = got or auth[7:].strip()
    return got == UAZAPI_WEBHOOK_SECRET


async def _uazapi_send_text(number: str, text: str) -> None:
    if not UAZAPI_BASE_URL or not UAZAPI_INSTANCE_TOKEN:
        logger.warning("UAZAPI_BASE_URL ou UAZAPI_INSTANCE_TOKEN ausentes; não enviei resposta.")
        return
    url = f"{UAZAPI_BASE_URL}/send/text"
    headers = {
        "token": UAZAPI_INSTANCE_TOKEN,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(url, headers=headers, json={"number": number, "text": text})
        r.raise_for_status()


async def handle_uazapi_whatsapp_event(
    request: Request,
    agent: Agent,
    body: Any,
) -> tuple[int, dict[str, Any]]:
    if not _verify_webhook_secret(request):
        return 401, {"ok": False, "error": "unauthorized"}

    number, user_text = _pick_incoming_message(body)
    if not number or not user_text:
        return 200, {"ok": True, "detail": "no_actionable_text_message"}

    if not UAZAPI_BASE_URL or not UAZAPI_INSTANCE_TOKEN:
        logger.warning("Webhook recebido mas UAZAPI não configurado para envio.")

    session_id = f"wa:{number}"
    run_out = await agent.arun(
        input=user_text,
        session_id=session_id,
        user_id=number,
        stream=False,
    )
    reply = (getattr(run_out, "content", None) or "").strip()
    if not reply:
        return 200, {"ok": True, "detail": "empty_agent_reply"}

    try:
        await _uazapi_send_text(number, reply)
    except httpx.HTTPStatusError as e:
        logger.exception("UAZAPI send/text falhou: %s", e.response.text[:500])
        return 502, {"ok": False, "error": "uazapi_send_failed"}
    except httpx.RequestError as e:
        logger.exception("UAZAPI rede: %s", e)
        return 502, {"ok": False, "error": "uazapi_network_error"}

    return 200, {"ok": True, "detail": "sent"}
