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
