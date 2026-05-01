"""Cliente HTTP mínimo UAZAPI (sync) — usado pelas tools Agno e reutilizável pelo webhook."""

from __future__ import annotations

import os
from typing import Any

import httpx


def _base_url() -> str:
    return (os.getenv("UAZAPI_BASE_URL") or "").strip().rstrip("/")


def _instance_token() -> str:
    return (os.getenv("UAZAPI_INSTANCE_TOKEN") or "").strip()


def uazapi_configured() -> bool:
    return bool(_base_url() and _instance_token())


def build_send_menu_body(number: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    """
    Converte um dict (modelo / tool) no corpo JSON de ``POST /send/menu``.
    Aceita chaves camelCase ou snake_case (``footer_text``, ``list_button``, …).
    """
    mt = spec.get("type") or spec.get("menu_type")
    if not mt:
        return None
    mt = str(mt).strip().lower()
    if mt not in ("button", "list", "poll", "carousel"):
        return None
    text = (spec.get("text") or "").strip()
    if not text:
        return None
    raw_choices = spec.get("choices")
    if not isinstance(raw_choices, list) or not raw_choices:
        return None
    choices: list[str] = []
    for c in raw_choices[:40]:
        if c is None:
            continue
        s = str(c).strip()
        if s:
            choices.append(s[:1024])
    if not choices:
        return None
    num = (number or "").strip()
    if not num:
        return None
    out: dict[str, Any] = {
        "number": num,
        "type": mt,
        "text": text[:4096],
        "choices": choices,
    }
    ft = spec.get("footerText") or spec.get("footer_text")
    if ft:
        out["footerText"] = str(ft).strip()[:1024]
    lb = spec.get("listButton") or spec.get("list_button")
    if lb:
        out["listButton"] = str(lb).strip()[:256]
    img = spec.get("imageButton") or spec.get("image_button")
    if img:
        out["imageButton"] = str(img).strip()[:2048]
    sc = spec.get("selectableCount") if spec.get("selectableCount") is not None else spec.get("selectable_count")
    if sc is not None:
        try:
            out["selectableCount"] = int(sc)
        except (TypeError, ValueError):
            pass
    dly = spec.get("delay") or spec.get("delay_ms")
    if dly is not None:
        try:
            out["delay"] = int(dly)
        except (TypeError, ValueError):
            pass
    if spec.get("readchat") is False:
        out["readchat"] = False
    else:
        out["readchat"] = True
    if mt == "list" and not out.get("listButton"):
        out["listButton"] = "Ver opções"
    if spec.get("readmessages") is not None:
        out["readmessages"] = bool(spec["readmessages"])
    if spec.get("replyid"):
        out["replyid"] = str(spec["replyid"]).strip()
    if spec.get("track_source"):
        out["track_source"] = str(spec["track_source"]).strip()
    if spec.get("track_id"):
        out["track_id"] = str(spec["track_id"]).strip()
    return out


def uazapi_post_json(path: str, body: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
    """
    POST JSON para a API UAZAPI. `path` sem barra inicial, ex. ``chat/details``.
    Header de autenticação: ``token`` (instância).
    """
    base = _base_url()
    token = _instance_token()
    if not base or not token:
        raise RuntimeError("UAZAPI_BASE_URL ou UAZAPI_INSTANCE_TOKEN não configurados")
    url = f"{base}/{path.lstrip('/')}"
    with httpx.Client(timeout=timeout) as client:
        r = client.post(
            url,
            headers={"token": token, "Content-Type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            return {"_raw": data}
        return data


def send_location_button_sync(
    number: str,
    text: str,
    *,
    delay: int | None = None,
    readchat: bool | None = None,
    readmessages: bool | None = None,
    replyid: str | None = None,
    track_source: str | None = None,
    track_id: str | None = None,
) -> dict[str, Any]:
    """
    ``POST /send/location-button`` — mensagem com botão nativo para o utilizador partilhar localização.
    Campos extra seguem a spec UAZAPI (opcionais).
    """
    num = (number or "").strip()
    body: dict[str, Any] = {"number": num, "text": (text or "").strip()}
    if not num or not body["text"]:
        raise ValueError("number e text são obrigatórios")
    if delay is not None:
        body["delay"] = int(delay)
    if readchat is not None:
        body["readchat"] = bool(readchat)
    if readmessages is not None:
        body["readmessages"] = bool(readmessages)
    if replyid:
        body["replyid"] = replyid.strip()
    if track_source:
        body["track_source"] = track_source.strip()
    if track_id:
        body["track_id"] = track_id.strip()
    return uazapi_post_json("send/location-button", body, timeout=60.0)


def fetch_chat_details_sync(number: str, *, preview_image: bool = False) -> dict[str, Any]:
    """
    ``POST /chat/details`` — detalhes completos do modelo Chat (spec UAZAPI).
    Body: ``{ "number": "<digits ou JID>", "preview": bool }``.
    """
    num = (number or "").strip()
    if not num:
        raise ValueError("number vazio")
    return uazapi_post_json(
        "chat/details",
        {"number": num, "preview": bool(preview_image)},
        timeout=60.0,
    )


def download_message_media_sync(message_id: str, *, generate_mp3: bool = True) -> dict[str, Any]:
    """
    ``POST /message/download`` — obtém URL pública (ou base64) da mídia associada ao ID da mensagem.
    Útil quando o webhook traz IDs criptografados sem URL HTTPS utilizável.
    """
    mid = (message_id or "").strip()
    if not mid:
        raise ValueError("message_id vazio")
    return uazapi_post_json(
        "message/download",
        {
            "id": mid,
            "return_link": True,
            "return_base64": False,
            "generate_mp3": bool(generate_mp3),
        },
        timeout=120.0,
    )


def send_menu_sync(
    number: str,
    *,
    menu_type: str,
    text: str,
    choices: list[str],
    footer_text: str | None = None,
    list_button: str | None = None,
    image_button: str | None = None,
    selectable_count: int | None = None,
    delay_ms: int | None = None,
    readchat: bool | None = True,
    readmessages: bool | None = None,
    replyid: str | None = None,
    track_source: str | None = None,
    track_id: str | None = None,
) -> dict[str, Any]:
    """
    ``POST /send/menu`` — botões, lista, enquete ou carrossel (spec UAZAPI).

    menu_type: ``button`` | ``list`` | ``poll`` | ``carousel``
    choices: formatos da doc UAZAPI (ex. botão ``"Rótulo|id"``; lista ``"[Secção]"`` ou ``"item|id|desc"``).
    list_button: obrigatório para type=list (texto do botão que abre a lista).
    """
    spec: dict[str, Any] = {
        "type": menu_type,
        "text": text,
        "choices": choices,
    }
    if footer_text:
        spec["footerText"] = footer_text
    if list_button:
        spec["listButton"] = list_button
    if image_button:
        spec["imageButton"] = image_button
    if selectable_count is not None:
        spec["selectableCount"] = int(selectable_count)
    if delay_ms is not None:
        spec["delay"] = int(delay_ms)
    if readchat is not None:
        spec["readchat"] = bool(readchat)
    if readmessages is not None:
        spec["readmessages"] = bool(readmessages)
    if replyid:
        spec["replyid"] = str(replyid).strip()
    if track_source:
        spec["track_source"] = str(track_source).strip()
    if track_id:
        spec["track_id"] = str(track_id).strip()
    body = build_send_menu_body(number, spec)
    if not body:
        raise ValueError("corpo do menu inválido — verifique type, text, choices")
    return uazapi_post_json("send/menu", body, timeout=120.0)
