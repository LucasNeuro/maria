"""Pont WhatsApp UAZAPI ↔ Mari: recebe webhook POST e responde com `/send/text`."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx
from fastapi import Request

from agno.agent import Agent

from maria_context import lead_request_context
from tools_maria import persist_conversation_turn_supabase
from uazapi_client import fetch_chat_details_sync, uazapi_configured

logger = logging.getLogger(__name__)

UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "").strip().rstrip("/")
UAZAPI_INSTANCE_TOKEN = os.getenv("UAZAPI_INSTANCE_TOKEN", "").strip()
UAZAPI_WEBHOOK_SECRET = os.getenv("UAZAPI_WEBHOOK_SECRET", "").strip()


def _log_webhook_full_body() -> bool:
    """MARIA_LOG_WEBHOOK_BODY=1 — JSON completo (truncado) nos logs; desliga em produção se houver dados sensíveis."""
    return os.getenv("MARIA_LOG_WEBHOOK_BODY", "").strip().lower() in ("1", "true", "yes")


def _clip(s: str, max_len: int = 160) -> str:
    s = (s or "").replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _json_preview(obj: Any, max_chars: int = 3500) -> str:
    try:
        raw = json.dumps(obj, ensure_ascii=False, default=str)
    except TypeError:
        raw = repr(obj)
    if len(raw) <= max_chars:
        return raw
    return raw[: max_chars] + "…"


def _body_summary(body: Any) -> str:
    if _log_webhook_full_body():
        return _json_preview(body, 6000)
    if isinstance(body, dict):
        keys = list(body.keys())
        hint = f"keys={keys[:30]}{'…' if len(keys) > 30 else ''}"
        for k in ("event", "EventType", "type", "action"):
            if k in body:
                hint += f" | {k}={body[k]!r}"
        return hint
    if isinstance(body, list):
        return f"list[len={len(body)}]"
    return type(body).__name__


def _scan_incoming_stats(body: Any) -> dict[str, int]:
    """Contagens úteis quando não há mensagem utilizável."""
    msgs = [m for m in _collect_dicts(body) if isinstance(m, dict)]
    with_text = sum(1 for m in msgs if _message_text(m))
    from_me = sum(1 for m in msgs if m.get("fromMe"))
    by_api = sum(1 for m in msgs if m.get("wasSentByApi"))
    return {"msg_dicts": len(msgs), "with_text": with_text, "fromMe": from_me, "wasSentByApi": by_api}


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


async def _uazapi_send_text(number: str, text: str) -> int:
    """Envia texto via UAZAPI; devolve HTTP status ou 0 se não enviou."""
    if not UAZAPI_BASE_URL or not UAZAPI_INSTANCE_TOKEN:
        logger.warning("UAZAPI_BASE_URL ou UAZAPI_INSTANCE_TOKEN ausentes; não enviei resposta.")
        return 0
    url = f"{UAZAPI_BASE_URL}/send/text"
    headers = {
        "token": UAZAPI_INSTANCE_TOKEN,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(url, headers=headers, json={"number": number, "text": text})
        r.raise_for_status()
        return r.status_code


async def handle_uazapi_whatsapp_event(
    request: Request,
    agent: Agent,
    body: Any,
) -> tuple[int, dict[str, Any]]:
    peer = request.client.host if request.client else "-"
    if not _verify_webhook_secret(request):
        logger.warning("[uazapi] 401 unauthorized | client=%s", peer)
        return 401, {"ok": False, "error": "unauthorized"}

    logger.info("[uazapi] webhook | client=%s | %s", peer, _body_summary(body))

    number, user_text = _pick_incoming_message(body)
    if not number or not user_text:
        stats = _scan_incoming_stats(body)
        logger.info(
            "[uazapi] no_actionable_text | client=%s | stats=%s | body=%s",
            peer,
            stats,
            _body_summary(body),
        )
        return 200, {"ok": True, "detail": "no_actionable_text_message"}

    if not UAZAPI_BASE_URL or not UAZAPI_INSTANCE_TOKEN:
        logger.warning("Webhook recebido mas UAZAPI não configurado para envio.")

    session_id = f"wa:{number}"
    logger.info(
        "[uazapi] inbound | session=%s | digits=%s | in_len=%d | in_preview=%r",
        session_id,
        number,
        len(user_text),
        _clip(user_text, 200),
    )

    async with lead_request_context(canal="whatsapp", telefone_whatsapp=number):
        run_out = await agent.arun(
            input=user_text,
            session_id=session_id,
            user_id=number,
            stream=False,
        )
    reply = (getattr(run_out, "content", None) or "").strip()
    if not reply:
        logger.info("[uazapi] empty_agent_reply | session=%s | in_preview=%r", session_id, _clip(user_text, 120))
        return 200, {"ok": True, "detail": "empty_agent_reply"}

    logger.info(
        "[uazapi] agent_reply | session=%s | out_len=%d | out_preview=%r",
        session_id,
        len(reply),
        _clip(reply, 220),
    )

    chat_details: dict[str, Any] | None = None
    if uazapi_configured():
        try:
            chat_details = await asyncio.to_thread(fetch_chat_details_sync, number)
        except Exception as e:
            logger.warning("[uazapi] chat/details | digits=%s | err=%s", number, e)

    tag_name: str | None = None
    if isinstance(chat_details, dict):
        raw_tag = (
            chat_details.get("wa_name")
            or chat_details.get("name")
            or chat_details.get("lead_name")
            or chat_details.get("wa_contactName")
        )
        if raw_tag:
            tag_name = str(raw_tag).strip()[:200]

    hook_payload: dict | list = body if isinstance(body, (dict, list)) else {"_repr": str(body)[:12000]}

    ok_turn, err_turn = persist_conversation_turn_supabase(
        canal="whatsapp",
        session_id=session_id,
        phone_e164=number,
        tag_name=tag_name,
        user_payload={"text": user_text, "source": "whatsapp_inbound"},
        assistant_payload={"text": reply, "source": "mari_agent"},
        webhook_payload=hook_payload,
        uazapi_chat_details=chat_details,
        metadata={"webhook": "uazapi"},
    )
    if ok_turn:
        logger.info("[uazapi] persist_turn | session=%s | ok=true", session_id)
    else:
        logger.warning("[uazapi] persist_turn | session=%s | ok=false | err=%s", session_id, err_turn)

    try:
        send_status = await _uazapi_send_text(number, reply)
        logger.info("[uazapi] send_text | session=%s | http=%s | out_len=%d", session_id, send_status, len(reply))
    except httpx.HTTPStatusError as e:
        logger.exception("UAZAPI send/text falhou: %s", e.response.text[:500])
        return 502, {"ok": False, "error": "uazapi_send_failed"}
    except httpx.RequestError as e:
        logger.exception("UAZAPI rede: %s", e)
        return 502, {"ok": False, "error": "uazapi_network_error"}

    return 200, {"ok": True, "detail": "sent"}
