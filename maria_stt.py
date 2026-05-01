"""Transcrição de voz (STT) via API Mistral — uso com áudio WhatsApp descarregado pela UAZAPI.

Implementação com ``httpx`` (multipart ``POST /v1/audio/transcriptions``) para não depender da versão
exposta pelo pacote ``mistralai`` no ``import`` — evita ``ImportError`` em deploy quando o SDK não
exporta ``Mistral`` como esperado.
"""

from __future__ import annotations

import logging
import os

import httpx

from uazapi_client import download_message_media_sync, uazapi_configured

logger = logging.getLogger(__name__)


def mistral_stt_enabled_env() -> bool:
    """STT ligado por defeito se existir ``MISTRAL_API_KEY`` e ``MARIA_STT_ENABLED`` não for off."""
    if not (os.getenv("MISTRAL_API_KEY") or "").strip():
        return False
    return os.getenv("MARIA_STT_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def _stt_model() -> str:
    return (os.getenv("MARIA_STT_MODEL") or "voxtral-mini-latest").strip()


def _stt_max_bytes() -> int:
    try:
        return max(
            512_000,
            min(int((os.getenv("MARIA_STT_MAX_BYTES") or str(25 * 1024 * 1024)).strip()), 40 * 1024 * 1024),
        )
    except ValueError:
        return 25 * 1024 * 1024


def _filename_from_mime(mime: str) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    if "mpeg" in m or m == "audio/mp3" or m == "audio/mpeg":
        return "voice.mp3"
    if "ogg" in m:
        return "voice.ogg"
    if "mp4" in m or "m4a" in m or "aac" in m:
        return "voice.m4a"
    if "wav" in m:
        return "voice.wav"
    if "webm" in m:
        return "voice.webm"
    return "voice.bin"


def _mistral_audio_transcriptions_url() -> str:
    base = (os.getenv("MISTRAL_API_BASE_URL") or "https://api.mistral.ai").strip().rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/audio/transcriptions"
    return f"{base}/v1/audio/transcriptions"


def transcribe_audio_bytes_sync(
    body: bytes,
    *,
    filename: str = "voice.ogg",
    content_type: str | None = None,
) -> str | None:
    """
    ``POST /v1/audio/transcriptions`` (API Mistral, multipart via httpx).
    Modelo por defeito: ``voxtral-mini-latest`` (``MARIA_STT_MODEL``).
    """
    key = (os.getenv("MISTRAL_API_KEY") or "").strip()
    if not key or not body:
        return None
    fn = (filename or "voice.bin").strip()[-200:] or "voice.bin"
    ct = (content_type or "application/octet-stream").split(";")[0].strip() or "application/octet-stream"
    url = _mistral_audio_transcriptions_url()
    headers = {"Authorization": f"Bearer {key}"}
    files = {"file": (fn, body, ct)}
    form: list[tuple[str, str]] = [("model", _stt_model())]
    lang = (os.getenv("MARIA_STT_LANGUAGE") or "pt").strip()
    if lang:
        form.append(("language", lang))
    if os.getenv("MARIA_STT_TIMESTAMP_SEGMENTS", "1").strip().lower() not in ("0", "false", "no", "off"):
        form.append(("timestamp_granularities", "segment"))
    try:
        with httpx.Client(timeout=120.0) as client:
            r = client.post(url, headers=headers, data=form, files=files)
    except Exception as exc:
        logger.warning("mistral STT request failed: %s", exc, exc_info=True)
        return None

    if r.status_code >= 400:
        logger.warning("mistral STT HTTP %s | %s", r.status_code, _clip_response_text(r.text))
        return None
    try:
        payload = r.json()
    except Exception as exc:
        logger.warning("mistral STT JSON parse failed: %s", exc)
        return None
    text = payload.get("text") if isinstance(payload, dict) else None
    out = str(text or "").strip()
    return out if out else None


def _clip_response_text(t: str, n: int = 400) -> str:
    s = (t or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def transcribe_voice_uazapi_sync(message_id: str) -> str | None:
    """
    Obtém o áudio via ``POST /message/download`` (``generate_mp3=True``) e transcreve.
    """
    mid = (message_id or "").strip()
    if not mid or not uazapi_configured():
        return None
    try:
        out = download_message_media_sync(mid, generate_mp3=True)
    except Exception as exc:
        logger.warning("STT: message/download | id=%s | err=%s", mid, exc)
        return None
    file_url = out.get("fileURL")
    if not isinstance(file_url, str) or not file_url.startswith("http"):
        logger.warning("STT: sem fileURL no download | id=%s", mid)
        return None
    mime = str(out.get("mimetype") or "").strip().lower() or "audio/mpeg"
    max_b = _stt_max_bytes()
    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as c:
            r = c.get(file_url)
            r.raise_for_status()
            body = r.content
    except Exception as exc:
        logger.warning("STT: fetch fileURL | err=%s", exc)
        return None
    if len(body) > max_b:
        logger.warning("STT: áudio maior que limite | bytes=%s", len(body))
        return None
    fname = _filename_from_mime(mime)
    return transcribe_audio_bytes_sync(body, filename=fname, content_type=mime.split(";")[0].strip())
