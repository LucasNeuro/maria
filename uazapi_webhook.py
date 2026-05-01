"""Pont WhatsApp UAZAPI ↔ Mari: recebe webhook POST e responde com `/send/text` (+ multimodal → Agno)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import Request

from agno.agent import Agent
from agno.media import File, Image

from maria_context import lead_request_context
from tools_maria import persist_conversation_turn_supabase, persist_uazapi_downloaded_images_sync
from uazapi_client import (
    build_send_menu_body,
    download_message_media_sync,
    fetch_chat_details_sync,
    uazapi_configured,
)

logger = logging.getLogger(__name__)

UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "").strip().rstrip("/")
UAZAPI_INSTANCE_TOKEN = os.getenv("UAZAPI_INSTANCE_TOKEN", "").strip()
UAZAPI_WEBHOOK_SECRET = os.getenv("UAZAPI_WEBHOOK_SECRET", "").strip()

# Alinhado ao POP (secção 10) — sem STT por agora.
MARIA_AUDIO_UNSUPPORTED_REPLY_PT = (
    "Recebi seu áudio. Por enquanto ainda não consigo ouvir mensagens de voz por aqui — "
    "pode enviar o mesmo pedido por texto? Assim consigo te ajudar melhor."
)


def _persist_uazapi_download_media_enabled() -> bool:
    """Gravar fotos do ``message/download`` no Storage + mari_lead_media (default ligado)."""
    return os.getenv("MARIA_PERSIST_UAZAPI_DOWNLOAD_MEDIA", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


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
    audioish = sum(1 for m in msgs if _is_audio_message(m))
    return {
        "msg_dicts": len(msgs),
        "with_text": with_text,
        "fromMe": from_me,
        "wasSentByApi": by_api,
        "audio_like": audioish,
    }


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


def _content_as_dict(content: Any) -> dict[str, Any] | None:
    if content is None:
        return None
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        s = content.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                parsed = json.loads(s)
            except (json.JSONDecodeError, TypeError):
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def _parse_content_text(content: Any) -> str:
    if content is None:
        return ""
    c = _content_as_dict(content)
    if c:
        if isinstance(c.get("extendedTextMessage"), dict):
            t = c["extendedTextMessage"].get("text")
            if isinstance(t, str) and t.strip():
                return t.strip()
        for key in ("conversation", "text", "caption"):
            v = c.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for media_key in ("imageMessage", "videoMessage", "documentMessage"):
            sub = c.get(media_key)
            if isinstance(sub, dict):
                cap = sub.get("caption")
                if isinstance(cap, str) and cap.strip():
                    return cap.strip()
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


def _parse_location_from_content(content: Any) -> dict[str, Any] | None:
    c = _content_as_dict(content)
    if not c:
        return None
    lm = c.get("locationMessage")
    if not isinstance(lm, dict):
        return None
    lat, lng = lm.get("degreesLatitude"), lm.get("degreesLongitude")
    if lat is None and lng is None:
        return None
    try:
        flat = float(lat) if lat is not None else None
        flng = float(lng) if lng is not None else None
    except (TypeError, ValueError):
        return None
    name = lm.get("name") or lm.get("title") or ""
    addr = lm.get("address") or ""
    return {
        "latitude": flat,
        "longitude": flng,
        "name": str(name).strip() if name is not None else "",
        "address": str(addr).strip() if addr is not None else "",
    }


def _wa_message_id(msg: dict[str, Any]) -> str | None:
    for key in ("messageid", "messageId", "id"):
        v = msg.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _message_type_lower(msg: dict[str, Any]) -> str:
    mt = msg.get("messageType")
    return str(mt or "").strip().lower()


def _effective_media_type(msg: dict[str, Any]) -> str:
    """Tipo de mídia: campo messageType ou inferido de ``content`` (UAZAPI nem sempre preenche o tipo)."""
    mt = _message_type_lower(msg)
    if mt in (
        "image",
        "sticker",
        "document",
        "video",
        "audio",
        "ptt",
        "pttmessage",
        "voice",
        "myaudio",
    ):
        return mt
    c = _content_as_dict(msg.get("content")) or {}
    if c.get("stickerMessage"):
        return "sticker"
    if c.get("imageMessage"):
        return "image"
    if c.get("videoMessage"):
        return "video"
    if c.get("documentMessage"):
        return "document"
    if c.get("audioMessage") or c.get("pttMessage"):
        return "ptt"
    return ""


def _has_visual_or_file_media(msg: dict[str, Any]) -> bool:
    """Há imagem/documento/vídeo para tratar (mesmo sem URL HTTPS no JSON)."""
    c = _content_as_dict(msg.get("content")) or {}
    if any(c.get(k) for k in ("imageMessage", "stickerMessage", "documentMessage", "videoMessage")):
        return True
    mt = _message_type_lower(msg)
    if mt in ("image", "sticker", "document", "video"):
        return True
    if bool(_http_urls_from_message(msg)) or _document_http_url(msg):
        return True
    return False


def _is_audio_message(msg: dict[str, Any]) -> bool:
    mt = _message_type_lower(msg)
    if mt in ("audio", "ptt", "pttmessage", "voice", "myaudio"):
        return True
    c = _content_as_dict(msg.get("content"))
    if not c:
        return False
    return bool(c.get("audioMessage") or c.get("pttMessage"))


def _http_urls_from_message(msg: dict[str, Any]) -> list[str]:
    """URLs HTTPS de imagem / sticker / vídeo (não inclui áudio — evita confundir com visão)."""
    seen: set[str] = set()
    out: list[str] = []

    def add(u: Any) -> None:
        if not isinstance(u, str):
            return
        s = u.strip()
        if not s.startswith(("http://", "https://")):
            return
        if s not in seen:
            seen.add(s)
            out.append(s)

    mt = _message_type_lower(msg)
    if mt not in ("audio", "ptt", "pttmessage", "voice", "myaudio"):
        add(msg.get("fileURL"))

    c = _content_as_dict(msg.get("content"))
    if c:
        for key in ("imageMessage", "stickerMessage"):
            sub = c.get(key)
            if isinstance(sub, dict):
                add(sub.get("url"))
        if mt == "video" or c.get("videoMessage"):
            sub = c.get("videoMessage")
            if isinstance(sub, dict):
                add(sub.get("url"))
    return out


def _document_http_url(msg: dict[str, Any]) -> str | None:
    c = _content_as_dict(msg.get("content"))
    if not c:
        return None
    dm = c.get("documentMessage")
    if not isinstance(dm, dict):
        return None
    u = dm.get("url")
    if isinstance(u, str) and u.startswith(("http://", "https://")):
        return u.strip()
    return None


def _document_filename_hint(msg: dict[str, Any]) -> str:
    c = _content_as_dict(msg.get("content"))
    if not c:
        return "documento"
    dm = c.get("documentMessage")
    if isinstance(dm, dict):
        fn = dm.get("fileName") or dm.get("filename") or dm.get("title")
        if isinstance(fn, str) and fn.strip():
            return fn.strip()[:180]
    return "documento"


def _message_timestamp_ms(msg: dict[str, Any]) -> int:
    ts = msg.get("messageTimestamp")
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, str) and ts.isdigit():
        return int(ts)
    return 0


def _multimodal_vision_enabled() -> bool:
    return os.getenv("MARIA_MULTIMODAL_VISION", "1").strip().lower() not in ("0", "false", "no", "off")


def _max_inbound_images() -> int:
    try:
        n = int((os.getenv("MARIA_MULTIMODAL_MAX_IMAGES") or "4").strip())
    except ValueError:
        n = 4
    return max(1, min(n, 8))


def _max_inbound_media_bytes() -> int:
    try:
        return max(512_000, min(int((os.getenv("MARIA_MAX_INBOUND_MEDIA_BYTES") or str(12 * 1024 * 1024)).strip()), 30 * 1024 * 1024))
    except ValueError:
        return 12 * 1024 * 1024


def _sniff_image_mime(data: bytes) -> str:
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 6 and (data[:6] in (b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _build_agent_user_text(
    text_raw: str,
    location: dict[str, Any] | None,
    n_images: int,
    *,
    n_files: int = 0,
    video_note: bool = False,
) -> str:
    parts: list[str] = []
    t = (text_raw or "").strip()
    if t:
        parts.append(t)
    if location:
        lat = location.get("latitude")
        lng = location.get("longitude")
        nm = str(location.get("name") or "").strip()
        addr = str(location.get("address") or "").strip()
        geo = f"[Localização partilhada pelo cliente: latitude={lat} longitude={lng}"
        if nm:
            geo += f" nome_do_local={nm!r}"
        if addr:
            geo += f" endereco={addr!r}"
        parts.append(geo + "]")
    if n_images:
        if not t and not location:
            parts.append(
                "O cliente enviou uma imagem pelo WhatsApp (sem texto). "
                "Analisa a imagem no contexto imobiliário e responde em português do Brasil, de forma curta."
            )
        elif t:
            parts.append(
                "(O cliente também enviou imagem(ns) em anexo — considera o conteúdo visual quando for relevante.)"
            )
    if n_files:
        parts.append(
            "(O cliente enviou um ficheiro/documento em anexo — usa o conteúdo se o modelo suportar, senão pede um resumo breve por texto.)"
        )
    if video_note and not n_images:
        parts.append(
            "[O cliente enviou vídeo; se não vires frames, acolhe e pede o essencial por escrito ou imagem, conforme o POP.]"
        )
    return "\n\n".join(parts).strip()


def _enrich_inbound_with_uazapi_download(inb: "InboundTurn") -> None:
    """Preenche ``vision_images`` / ``file_inputs`` via ``POST /message/download`` quando a URL do webhook falha."""
    if inb.audio_only:
        return
    eff = _effective_media_type(inb.source_msg)
    if eff in ("audio", "ptt", "pttmessage", "voice", "myaudio"):
        return
    if eff not in ("image", "sticker", "document", "video"):
        return
    mid = _wa_message_id(inb.source_msg)
    if not mid:
        if _has_visual_or_file_media(inb.source_msg):
            logger.warning(
                "[uazapi] mídia sem messageid no webhook — message/download impossível | eff=%s",
                eff,
            )
        return
    if not uazapi_configured():
        return
    # Já temos bytes tratados (evita duplicar).
    if eff in ("image", "sticker") and inb.vision_images:
        return
    if eff == "document" and inb.file_inputs:
        return
    if eff == "video" and inb.video_attachment_note:
        return

    try:
        out = download_message_media_sync(mid, generate_mp3=False)
    except Exception as e:
        logger.warning("[uazapi] message/download | id=%s | err=%s", mid, e)
        return

    file_url = out.get("fileURL")
    if not isinstance(file_url, str) or not file_url.startswith("http"):
        return

    mime = str(out.get("mimetype") or "").strip().lower()
    max_b = _max_inbound_media_bytes()
    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as c:
            r = c.get(file_url)
            r.raise_for_status()
            body = r.content
    except Exception as e:
        logger.warning("[uazapi] fetch fileURL | err=%s", e)
        return

    if len(body) > max_b:
        logger.warning("[uazapi] media too large | bytes=%s", len(body))
        return

    if mime.startswith("image/") or eff in ("image", "sticker"):
        img_mime = mime if mime.startswith("image/") else _sniff_image_mime(body)
        inb.vision_images.append(Image(content=body, mime_type=img_mime))
        return

    if eff == "video" or mime.startswith("video/"):
        logger.info("[uazapi] video inbound | bytes=%s (sem pipeline de visão de vídeo dedicada)", len(body))
        inb.video_attachment_note = True
        return

    if eff == "document" or mime == "application/pdf" or "pdf" in mime:
        fname = _document_filename_hint(inb.source_msg)
        if mime == "application/pdf" or fname.lower().endswith(".pdf"):
            try:
                inb.file_inputs.append(
                    File(content=body, mime_type="application/pdf", filename=fname[-200:])
                )
            except Exception as e:
                logger.warning("[uazapi] File(pdf) | err=%s", e)
        else:
            # docx / outros tipos suportados pelo Agno File
            valid = set(File.valid_mime_types())
            if mime in valid:
                try:
                    inb.file_inputs.append(File(content=body, mime_type=mime, filename=fname[-200:]))
                except Exception as e:
                    logger.warning("[uazapi] File | err=%s", e)
            else:
                logger.info("[uazapi] document mime not passed to model | mime=%s", mime)


@dataclass
class InboundTurn:
    number: str
    text_raw: str
    image_urls: list[str]
    document_url: str | None
    location: dict[str, Any] | None
    source_msg: dict[str, Any]
    audio_only: bool = False
    vision_images: list[Image] = field(default_factory=list)
    file_inputs: list[File] = field(default_factory=list)
    video_attachment_note: bool = False


def _pick_inbound_turn(body: Any) -> InboundTurn | None:
    """Última mensagem elegível por ``messageTimestamp``."""
    best: tuple[int, InboundTurn] | None = None
    for msg in _collect_dicts(body):
        if not isinstance(msg, dict):
            continue
        if msg.get("wasSentByApi") or msg.get("fromMe"):
            continue
        dest = _destination_number_or_jid(msg)
        if not dest:
            continue
        text_raw = _message_text(msg)
        loc = _parse_location_from_content(msg.get("content"))
        urls = _http_urls_from_message(msg)
        doc_url = _document_http_url(msg)
        media_signal = _has_visual_or_file_media(msg)
        audio_pure = (
            _is_audio_message(msg)
            and not text_raw.strip()
            and not urls
            and not loc
            and not doc_url
            and not media_signal
        )
        has_actionable = (
            audio_pure
            or bool(text_raw.strip())
            or bool(urls)
            or bool(loc)
            or bool(doc_url)
            or bool(media_signal)
        )
        if not has_actionable:
            continue
        ts = _message_timestamp_ms(msg)
        cand = InboundTurn(
            number=dest,
            text_raw=text_raw,
            image_urls=urls,
            document_url=doc_url,
            location=loc,
            source_msg=dict(msg),
            audio_only=bool(audio_pure),
        )
        if best is None or ts >= best[0]:
            best = (ts, cand)
    return best[1] if best else None


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


def _download_url_to_image(url: str) -> Image | None:
    max_b = _max_inbound_media_bytes()
    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as c:
            r = c.get(url.strip())
            r.raise_for_status()
            body = r.content
        if len(body) > max_b:
            return None
        ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        mime = ct if ct.startswith("image/") else _sniff_image_mime(body)
        return Image(content=body, mime_type=mime)
    except Exception as e:
        logger.warning("[uazapi] download image url | err=%s", e)
        return None


def _try_load_document_bytes(url: str, filename: str) -> File | None:
    max_b = _max_inbound_media_bytes()
    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as c:
            r = c.get(url.strip())
            r.raise_for_status()
            body = r.content
        if len(body) > max_b:
            return None
        ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        valid = set(File.valid_mime_types())
        if ct in valid:
            return File(content=body, mime_type=ct, filename=filename[-200:])
        if filename.lower().endswith(".pdf"):
            return File(content=body, mime_type="application/pdf", filename=filename[-200:])
    except Exception as e:
        logger.warning("[uazapi] download document | err=%s", e)
    return None


MARIA_MENU_JSON_BEGIN = "<<<MARIA_MENU_JSON>>>"
MARIA_MENU_JSON_END = "<<<END_MARIA_MENU_JSON>>>"


def _interactive_menu_enabled() -> bool:
    return os.getenv("MARIA_INTERACTIVE_MENU", "1").strip().lower() not in ("0", "false", "no", "off")


def _strip_menu_markers_fallback(s: str) -> str:
    """Remove bloco de menu malformado para não mostrar marcadores ao utilizador."""
    if MARIA_MENU_JSON_BEGIN not in s or MARIA_MENU_JSON_END not in s:
        return (s or "").strip()
    parts: list[str] = []
    rest = s
    while MARIA_MENU_JSON_BEGIN in rest and MARIA_MENU_JSON_END in rest:
        pre, _, tail = rest.partition(MARIA_MENU_JSON_BEGIN)
        parts.append(pre)
        _, _, rest = tail.partition(MARIA_MENU_JSON_END)
    parts.append(rest)
    return "".join(parts).strip()


def _split_reply_menu_json(reply: str) -> tuple[str, dict[str, Any] | None]:
    """
    Separa texto visível do bloco ``<<<MARIA_MENU_JSON>>>...<<<END_MARIA_MENU_JSON>>>``.
    O JSON segue a API UAZAPI ``/send/menu`` (type, text, choices, ...).
    """
    if not _interactive_menu_enabled():
        return (reply or "").strip(), None
    r = reply or ""
    if MARIA_MENU_JSON_BEGIN not in r or MARIA_MENU_JSON_END not in r:
        return r.strip(), None
    pre, _, mid = r.partition(MARIA_MENU_JSON_BEGIN)
    js, _, post = mid.partition(MARIA_MENU_JSON_END)
    pre_stripped = pre.strip()
    if post.strip():
        logger.info("[uazapi] texto após END_MARIA_MENU_JSON foi ignorado")
    try:
        spec = json.loads(js.strip())
    except json.JSONDecodeError as e:
        logger.warning("[uazapi] MARIA_MENU_JSON inválido | err=%s", e)
        fb = _strip_menu_markers_fallback(r)
        return (fb if fb else pre_stripped, None)
    if not isinstance(spec, dict):
        return (_strip_menu_markers_fallback(r), None)
    return pre_stripped, spec


async def _uazapi_send_menu_json(number: str, payload: dict[str, Any]) -> int:
    """POST ``/send/menu`` assíncrono."""
    if not UAZAPI_BASE_URL or not UAZAPI_INSTANCE_TOKEN:
        logger.warning("UAZAPI não configurado; não enviei menu interativo.")
        return 0
    url = f"{UAZAPI_BASE_URL}/send/menu"
    headers = {
        "token": UAZAPI_INSTANCE_TOKEN,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.status_code


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

    inbound = _pick_inbound_turn(body)
    if not inbound:
        stats = _scan_incoming_stats(body)
        logger.info(
            "[uazapi] no_actionable_inbound | client=%s | stats=%s | body=%s",
            peer,
            stats,
            _body_summary(body),
        )
        return 200, {"ok": True, "detail": "no_actionable_inbound_message"}

    if not UAZAPI_BASE_URL or not UAZAPI_INSTANCE_TOKEN:
        logger.warning("Webhook recebido mas UAZAPI não configurado para envio.")

    session_id = f"wa:{inbound.number}"
    number = inbound.number
    logger.info(
        "[uazapi] picked inbound | session=%s | eff_type=%s | msgId=%s | image_urls=%d | doc=%s | audio_only=%s",
        session_id,
        _effective_media_type(inbound.source_msg) or "-",
        _wa_message_id(inbound.source_msg) or "-",
        len(inbound.image_urls),
        bool(inbound.document_url),
        inbound.audio_only,
    )

    if inbound.audio_only:
        reply = MARIA_AUDIO_UNSUPPORTED_REPLY_PT
        user_text = (
            "[Cliente enviou mensagem de voz/áudio — canal ainda sem transcrição automática. "
            "Aviso enviado conforme POP.]"
        )
        logger.info("[uazapi] audio_only | session=%s", session_id)
        async with lead_request_context(canal="whatsapp", telefone_whatsapp=number):
            pass
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
        user_payload_audio: dict[str, Any] = {
            "text": user_text,
            "source": "whatsapp_inbound",
            "audio_received": True,
        }
        ok_turn, err_turn = persist_conversation_turn_supabase(
            canal="whatsapp",
            session_id=session_id,
            phone_e164=number,
            tag_name=tag_name,
            user_payload=user_payload_audio,
            assistant_payload={"text": reply, "source": "mari_agent"},
            webhook_payload=hook_payload,
            uazapi_chat_details=chat_details,
            metadata={"webhook": "uazapi", "audio_only": True},
        )
        if ok_turn:
            logger.info("[uazapi] persist_turn | session=%s | ok=true", session_id)
        else:
            logger.warning("[uazapi] persist_turn | session=%s | ok=false | err=%s", session_id, err_turn)
        try:
            await _uazapi_send_text(number, reply)
        except httpx.HTTPStatusError as e:
            logger.exception("UAZAPI send/text falhou: %s", e.response.text[:500])
            return 502, {"ok": False, "error": "uazapi_send_failed"}
        except httpx.RequestError as e:
            logger.exception("UAZAPI rede: %s", e)
            return 502, {"ok": False, "error": "uazapi_network_error"}
        return 200, {"ok": True, "detail": "audio_unsupported_notice_sent"}

    await asyncio.to_thread(_enrich_inbound_with_uazapi_download, inbound)
    logger.info(
        "[uazapi] enrich done | session=%s | vision_images=%d | files=%d | video_note=%s | image_urls=%d",
        session_id,
        len(inbound.vision_images),
        len(inbound.file_inputs),
        inbound.video_attachment_note,
        len(inbound.image_urls),
    )

    if _persist_uazapi_download_media_enabled() and inbound.vision_images:
        try:
            persist_out = await asyncio.to_thread(
                persist_uazapi_downloaded_images_sync,
                number,
                inbound.vision_images,
                _wa_message_id(inbound.source_msg),
            )
            logger.info("[uazapi] persist_download_media | session=%s | %s", session_id, _clip(persist_out, 480))
        except Exception as e:
            logger.warning("[uazapi] persist_download_media | session=%s | err=%s", session_id, e)

    vision_on = _multimodal_vision_enabled()
    cap_n = _max_inbound_images()

    # Documento por URL direta do webhook
    if inbound.document_url and not inbound.file_inputs:
        f = await asyncio.to_thread(
            _try_load_document_bytes,
            inbound.document_url,
            _document_filename_hint(inbound.source_msg),
        )
        if f:
            inbound.file_inputs.append(f)

    # Imagens: bytes via UAZAPI ``message/download`` têm prioridade; URLs do webhook só se ainda vazio.
    vision_list: list[Image] = list(inbound.vision_images)
    if vision_on and not vision_list:
        for u in inbound.image_urls[:cap_n]:
            if len(vision_list) >= cap_n:
                break
            img = await asyncio.to_thread(_download_url_to_image, u)
            if img:
                vision_list.append(img)
            else:
                try:
                    vision_list.append(Image(url=u))
                except Exception:
                    pass
    images_kw: list[Image] | None = vision_list[:cap_n] if vision_list and vision_on else None
    if inbound.image_urls and not vision_on:
        logger.info("[uazapi] multimodal_images_skipped | MARIA_MULTIMODAL_VISION=0 | n=%d", len(inbound.image_urls))

    files_kw: list[File] | None = inbound.file_inputs if inbound.file_inputs else None

    n_img = len(vision_list) if vision_on else len(inbound.image_urls)
    user_text = _build_agent_user_text(
        inbound.text_raw,
        inbound.location,
        n_img,
        n_files=len(inbound.file_inputs),
        video_note=inbound.video_attachment_note or (_message_type_lower(inbound.source_msg) == "video"),
    )
    if inbound.image_urls and not vision_on:
        user_text += (
            "\n\n[Nota: há imagem(ns) no WhatsApp; com MARIA_MULTIMODAL_VISION=0 o modelo não a vê — "
            "pede uma descrição breve por texto ou confirma que vais tratar sem detalhe visual.]"
        )

    logger.info(
        "[uazapi] inbound | session=%s | in_len=%d | urls=%d | vision_bytes=%d | files=%d | location=%s | in_preview=%r",
        session_id,
        len(user_text),
        len(inbound.image_urls),
        len(inbound.vision_images),
        len(inbound.file_inputs),
        bool(inbound.location),
        _clip(user_text, 200),
    )

    async with lead_request_context(canal="whatsapp", telefone_whatsapp=number):
        run_out = await agent.arun(
            input=user_text,
            session_id=session_id,
            user_id=number,
            stream=False,
            images=images_kw,
            files=files_kw,
        )
    reply_raw = (getattr(run_out, "content", None) or "").strip()
    if not reply_raw:
        logger.info("[uazapi] empty_agent_reply | session=%s | in_preview=%r", session_id, _clip(user_text, 120))
        return 200, {"ok": True, "detail": "empty_agent_reply"}

    plain_preface, menu_spec = _split_reply_menu_json(reply_raw)
    menu_payload = build_send_menu_body(number, menu_spec) if menu_spec else None
    if menu_spec and not menu_payload:
        logger.warning("[uazapi] menu JSON inválido como payload /send/menu | session=%s", session_id)

    if menu_payload:
        reply_for_log_parts: list[str] = []
        if plain_preface:
            reply_for_log_parts.append(plain_preface)
        reply_for_log_parts.append(
            f"[Menu WhatsApp — {menu_payload['type']}] {menu_payload.get('text', '')[:500]}"
        )
        reply_for_log = "\n".join(reply_for_log_parts).strip()
    elif menu_spec and not menu_payload:
        reply_for_log = _strip_menu_markers_fallback(reply_raw)
    else:
        reply_for_log = reply_raw

    logger.info(
        "[uazapi] agent_reply | session=%s | out_len=%d | has_menu=%s | out_preview=%r",
        session_id,
        len(reply_raw),
        bool(menu_payload),
        _clip(reply_raw, 220),
    )

    chat_details = None
    if uazapi_configured():
        try:
            chat_details = await asyncio.to_thread(fetch_chat_details_sync, number)
        except Exception as e:
            logger.warning("[uazapi] chat/details | digits=%s | err=%s", number, e)

    tag_name = None
    if isinstance(chat_details, dict):
        raw_tag = (
            chat_details.get("wa_name")
            or chat_details.get("name")
            or chat_details.get("lead_name")
            or chat_details.get("wa_contactName")
        )
        if raw_tag:
            tag_name = str(raw_tag).strip()[:200]

    hook_payload = body if isinstance(body, (dict, list)) else {"_repr": str(body)[:12000]}

    user_payload: dict[str, Any] = {"text": user_text, "source": "whatsapp_inbound"}
    if inbound.image_urls:
        user_payload["images"] = inbound.image_urls
    if inbound.location:
        user_payload["location"] = inbound.location
    if inbound.document_url:
        user_payload["document_url"] = inbound.document_url
    if inbound.file_inputs:
        user_payload["documents"] = [
            {"filename": getattr(f, "filename", None), "mime_type": getattr(f, "mime_type", None)} for f in inbound.file_inputs
        ]

    assistant_payload: dict[str, Any] = {"text": reply_for_log, "source": "mari_agent"}
    if menu_payload:
        assistant_payload["uazapi_menu_type"] = menu_payload.get("type")

    ok_turn, err_turn = persist_conversation_turn_supabase(
        canal="whatsapp",
        session_id=session_id,
        phone_e164=number,
        tag_name=tag_name,
        user_payload=user_payload,
        assistant_payload=assistant_payload,
        webhook_payload=hook_payload,
        uazapi_chat_details=chat_details,
        metadata={"webhook": "uazapi"},
    )
    if ok_turn:
        logger.info("[uazapi] persist_turn | session=%s | ok=true", session_id)
    else:
        logger.warning("[uazapi] persist_turn | session=%s | ok=false | err=%s", session_id, err_turn)

    try:
        if menu_payload:
            if plain_preface:
                st_pre = await _uazapi_send_text(number, plain_preface)
                logger.info(
                    "[uazapi] send_text_preface | session=%s | http=%s | len=%d",
                    session_id,
                    st_pre,
                    len(plain_preface),
                )
            st_menu = await _uazapi_send_menu_json(number, menu_payload)
            logger.info(
                "[uazapi] send_menu | session=%s | http=%s | type=%s | choices=%d",
                session_id,
                st_menu,
                menu_payload.get("type"),
                len(menu_payload.get("choices") or []),
            )
        elif menu_spec and not menu_payload:
            fb = _strip_menu_markers_fallback(reply_raw)
            send_status = await _uazapi_send_text(number, fb)
            logger.info("[uazapi] send_text | session=%s | http=%s | fallback_strip_menu | len=%d", session_id, send_status, len(fb))
        else:
            send_status = await _uazapi_send_text(number, reply_raw)
            logger.info("[uazapi] send_text | session=%s | http=%s | out_len=%d", session_id, send_status, len(reply_raw))
    except httpx.HTTPStatusError as e:
        logger.exception("UAZAPI envio falhou: %s", e.response.text[:500])
        return 502, {"ok": False, "error": "uazapi_send_failed"}
    except httpx.RequestError as e:
        logger.exception("UAZAPI rede: %s", e)
        return 502, {"ok": False, "error": "uazapi_network_error"}

    return 200, {"ok": True, "detail": "sent_menu" if menu_payload else "sent"}
