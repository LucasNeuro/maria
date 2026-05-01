"""Ferramentas da Mari — leads locais (JSONL) e opcionalmente Supabase."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid4

import httpx

from maria_context import get_lead_context

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LEADS_FILE = DATA_DIR / "leads.jsonl"


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _supabase_configured() -> bool:
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    return bool(url and key)


_ALLOWED_TIPOS = frozenset({"cliente_final", "proprietario", "parceiro"})


def _normalize_tipo_lead(raw: str) -> str | None:
    t = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "cliente": "cliente_final",
        "comprador": "cliente_final",
        "proprietário": "proprietario",
        "proprietária": "proprietario",
        "parceira": "parceiro",
        "corretor": "parceiro",
        "imobiliaria": "parceiro",
        "imobiliária": "parceiro",
    }
    t = aliases.get(t, t)
    return t if t in _ALLOWED_TIPOS else None


def _non_empty_text(val: object, fallback: str) -> str:
    s = (str(val).strip() if val is not None else "") or ""
    return s if s else fallback


def _insert_lead_supabase(row: dict, extra: dict) -> tuple[bool, str | None]:
    """Insert na tabela public.leads. Retorna (ok, mensagem_erro)."""
    try:
        from supabase import create_client
    except ImportError:
        return False, "pacote supabase não instalado"

    tipo = _normalize_tipo_lead(str(row.get("tipo_lead") or ""))
    if not tipo:
        return False, (
            f"tipo_lead inválido {row.get('tipo_lead')!r} — use exatamente: "
            "cliente_final | proprietario | parceiro"
        )

    nome = _non_empty_text(row.get("nome"), "Não informado")
    telefone = _non_empty_text(row.get("telefone"), "Não informado")
    resumo = _non_empty_text(row.get("resumo_geral"), "(resumo não informado)")
    potencial = _non_empty_text(row.get("potencial"), "BAIXO")

    try:
        client = create_client(
            os.environ["SUPABASE_URL"].strip(),
            os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip(),
        )
        # created_at: omitir para usar default now() do Postgres (evita parsing ISO edge-case).
        payload: dict = {
            "tipo_lead": tipo,
            "nome": nome,
            "telefone": telefone,
            "potencial": potencial,
            "resumo_geral": resumo,
            "dados": extra or {},
        }
        opt_keys = (
            "email",
            "origem",
            "imovel_interesse",
            "perguntas_resumo",
            "midias_enviadas",
            "pediu_visita",
            "urgencia",
        )
        for k in opt_keys:
            if k in row and row[k] is not None:
                payload[k] = row[k]

        client.table("leads").insert(payload).execute()
        return True, None
    except Exception as exc:  # noqa: BLE001 — tool deve devolver texto ao modelo
        logger.warning("Supabase insert leads falhou: %s", exc, exc_info=True)
        return False, str(exc)


_JSONB_MAX_BYTES = 900_000


def truncate_jsonb_payload(data: object) -> object | None:
    """Limita tamanho de payloads gravados em jsonb (webhook /chat/details)."""
    if data is None:
        return None
    try:
        raw = json.dumps(data, ensure_ascii=False, default=str)
    except TypeError:
        raw = json.dumps({"_non_serializable": str(data)[:8000]}, ensure_ascii=False)
    if len(raw.encode("utf-8")) <= _JSONB_MAX_BYTES:
        return data if isinstance(data, (dict, list)) else json.loads(raw)
    return {"_truncated": True, "approx_chars": len(raw), "json_head": raw[:80000]}


def persist_conversation_turn_supabase(
    *,
    canal: str,
    session_id: str,
    phone_e164: str,
    user_payload: dict,
    assistant_payload: dict,
    webhook_payload: dict | list | None = None,
    uazapi_chat_details: dict | list | None = None,
    tag_name: str | None = None,
    metadata: dict | None = None,
) -> tuple[bool, str | None]:
    """
    Grava um turno em ``public.mari_conversation_turns`` (schema v2: jsonb + phone_e164).
    Chamado pelo webhook WhatsApp após cada resposta gerada.
    """
    if not _supabase_configured():
        return False, "supabase não configurado"
    try:
        from supabase import create_client
    except ImportError:
        return False, "pacote supabase não instalado"

    phone = "".join(c for c in (phone_e164 or "") if c.isdigit())
    if not phone:
        return False, "phone_e164 vazio"

    ut = user_payload.get("text") if isinstance(user_payload, dict) else None
    ar = assistant_payload.get("text") if isinstance(assistant_payload, dict) else None
    user_has_signal = bool(str(ut or "").strip())
    if isinstance(user_payload, dict):
        loc = user_payload.get("location")
        if isinstance(loc, dict) and (loc.get("latitude") is not None or loc.get("longitude") is not None):
            user_has_signal = True
        imgs = user_payload.get("images")
        if isinstance(imgs, list) and any(str(x).strip() for x in imgs):
            user_has_signal = True
        if user_payload.get("audio_received") is True:
            user_has_signal = True
        docs = user_payload.get("documents")
        if isinstance(docs, list) and len(docs) > 0:
            user_has_signal = True
    if not user_has_signal or not str(ar or "").strip():
        return False, "user_payload sem texto/localização/imagens ou assistant_payload.text vazio"

    try:
        client = create_client(
            os.environ["SUPABASE_URL"].strip(),
            os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip(),
        )
        row = {
            "canal": (canal or "whatsapp").strip() or "whatsapp",
            "phone_e164": phone[:32],
            "session_id": (session_id or "").strip()[:2048],
            "tag_name": (tag_name or "").strip()[:200] or None,
            "user_payload": truncate_jsonb_payload(user_payload) or user_payload,
            "assistant_payload": truncate_jsonb_payload(assistant_payload) or assistant_payload,
            "webhook_payload": truncate_jsonb_payload(webhook_payload),
            "uazapi_chat_details": truncate_jsonb_payload(uazapi_chat_details),
            "metadata": truncate_jsonb_payload(metadata if metadata is not None else {}) or {},
        }
        client.table("mari_conversation_turns").insert(row).execute()
        return True, None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase insert mari_conversation_turns falhou: %s", exc, exc_info=True)
        return False, str(exc)


def _digits_only(s: str) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def _phone_variants_for_query(digits: str) -> list[str]:
    """Chaves possíveis em ``phone_e164`` / comparação com ``leads.telefone`` formatado."""
    d = _digits_only(digits)
    if not d:
        return []
    variants = {d}
    if len(d) >= 11:
        variants.add(d[-11:])
    if d.startswith("55") and len(d) >= 12:
        variants.add(d[2:])
    if len(d) >= 10:
        variants.add(d[-10:])
    return sorted(variants, key=len, reverse=True)


def _same_contact_phone(stored: str, canonical_digits: str) -> bool:
    """Compara telefone CRM/WhatsApp com tolerância a DDI e máscaras."""
    a = _digits_only(stored)
    b = _digits_only(canonical_digits)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 11 and len(b) >= 11 and a[-11:] == b[-11:]:
        return True
    if len(a) >= 10 and len(b) >= 10 and a[-10:] == b[-10:]:
        return True
    return a.endswith(b) or b.endswith(a)


_MEMORY_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,20}$")


def _normalize_agno_memory_email(raw: str) -> str | None:
    s = (raw or "").strip().lower()
    if not s or _MEMORY_EMAIL_RE.match(s) is None:
        return None
    return s


def _extract_email_from_uazapi_chat_details(obj: object, *, _depth: int = 0) -> str | None:
    if _depth > 8:
        return None
    if isinstance(obj, dict):
        for k in (
            "lead_email",
            "leadEmail",
            "email",
            "wa_email",
            "contact_email",
            "ContactEmail",
            "e_mail",
            "mail",
        ):
            v = obj.get(k)
            if v is None:
                continue
            ne = _normalize_agno_memory_email(str(v))
            if ne:
                return ne
        for v in obj.values():
            ne = _extract_email_from_uazapi_chat_details(v, _depth=_depth + 1)
            if ne:
                return ne
    elif isinstance(obj, list):
        for item in obj[:40]:
            ne = _extract_email_from_uazapi_chat_details(item, _depth=_depth + 1)
            if ne:
                return ne
    return None


def _lead_email_for_phone_from_supabase(phone_digits: str) -> str | None:
    """Primeiro e-mail não vazio em ``leads`` cujo telefone casa com ``phone_digits``."""
    if not _supabase_configured():
        return None
    phone = _digits_only(phone_digits)
    if not phone:
        return None
    try:
        from supabase import create_client
    except ImportError:
        return None
    try:
        client = create_client(
            os.environ["SUPABASE_URL"].strip(),
            os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip(),
        )
        lead_resp = (
            client.table("leads")
            .select("email,telefone,created_at")
            .order("created_at", desc=True)
            .limit(400)
            .execute()
        )
        for row in lead_resp.data or []:
            if not _same_contact_phone(str(row.get("telefone") or ""), phone):
                continue
            ne = _normalize_agno_memory_email(str(row.get("email") or ""))
            if ne:
                return ne
    except Exception as exc:  # noqa: BLE001
        logger.warning("lead_email_for_phone_from_supabase: %s", exc, exc_info=True)
    return None


def resolve_agno_user_id_for_whatsapp(
    telefone_whatsapp: str,
    chat_details: dict[str, Any] | None = None,
) -> str:
    """
    ``user_id`` do Agno (MemoryManager) no WhatsApp.

    Por defeito: só dígitos do telefone. Com ``MARIA_WHATSAPP_MEMORY_USER_ID_STRATEGY=email_first``,
    usa o e-mail vindo do UAZAPI ``/chat/details`` ou do Supabase ``public.leads`` (mesmo contacto),
    alinhando com o Agno Studio quando o utilizador é identificado pelo mesmo e-mail.
    """
    phone = _digits_only(telefone_whatsapp or "")
    if not phone:
        return ""
    strategy = (os.getenv("MARIA_WHATSAPP_MEMORY_USER_ID_STRATEGY") or "phone").strip().lower()
    if strategy in ("email_first", "email", "studio", "prefer_email"):
        email = _extract_email_from_uazapi_chat_details(chat_details) if chat_details else None
        if not email:
            email = _lead_email_for_phone_from_supabase(phone)
        if email:
            return email
    return phone


def _payload_text(payload: object, max_len: int = 4000) -> str:
    if not isinstance(payload, dict):
        return ""
    raw = payload.get("text")
    if raw is None:
        return ""
    s = str(raw).strip()
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def _json_preview(val: object, max_chars: int = 6000) -> object:
    try:
        raw = json.dumps(val, ensure_ascii=False, default=str)
    except TypeError:
        return str(val)[:2000]
    if len(raw) <= max_chars:
        return json.loads(raw)
    return {"_truncado": True, "preview": raw[:max_chars]}


def contexto_lead_por_telefone(telefone: str, max_turnos: int = 25, max_leads: int = 8) -> str:
    """
    Lê **apenas** ``public.leads`` e ``public.mari_conversation_turns`` no Supabase para este telefone
    (só dígitos ou misturado com máscara). Use quando o cliente voltar ao WhatsApp ou precisares de
    histórico registado + últimos turnos Mari — **não** expõe outras tabelas do Hub.

    telefone: ex. ``5511999999999`` ou formato local com máscara.
    max_turnos / max_leads: limites de linhas devolvidas (cap interno por segurança).
    """
    phone = _digits_only(telefone or "")
    if not phone:
        return json.dumps({"erro": "telefone vazio ou sem dígitos"}, ensure_ascii=False)

    mt = max(1, min(int(max_turnos), 80))
    ml = max(1, min(int(max_leads), 30))

    if not _supabase_configured():
        return json.dumps(
            {"erro": "Supabase não configurado (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)"},
            ensure_ascii=False,
        )

    try:
        from supabase import create_client
    except ImportError:
        return json.dumps({"erro": "pacote supabase não instalado"}, ensure_ascii=False)

    variants = _phone_variants_for_query(phone)
    try:
        client = create_client(
            os.environ["SUPABASE_URL"].strip(),
            os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip(),
        )

        turn_resp = (
            client.table("mari_conversation_turns")
            .select("id,created_at,canal,session_id,tag_name,user_payload,assistant_payload")
            .in_("phone_e164", variants)
            .order("created_at", desc=True)
            .limit(mt)
            .execute()
        )
        turn_rows = list(reversed(turn_resp.data or []))

        lead_resp = (
            client.table("leads")
            .select(
                "id,created_at,tipo_lead,nome,telefone,email,origem,imovel_interesse,"
                "perguntas_resumo,potencial,resumo_geral,dados"
            )
            .order("created_at", desc=True)
            .limit(500)
            .execute()
        )
        all_leads = lead_resp.data or []
        matched_leads = [row for row in all_leads if _same_contact_phone(str(row.get("telefone") or ""), phone)][
            :ml
        ]

        def pack_turn(r: dict) -> dict:
            up = r.get("user_payload") or {}
            ap = r.get("assistant_payload") or {}
            ca = r.get("created_at")
            if ca is None:
                cat = ""
            elif hasattr(ca, "isoformat"):
                cat = ca.isoformat()
            else:
                cat = str(ca)
            return {
                "created_at": cat,
                "canal": r.get("canal"),
                "session_id": r.get("session_id"),
                "tag_name": r.get("tag_name"),
                "user_text": _payload_text(up),
                "assistant_text": _payload_text(ap),
            }

        def pack_lead(r: dict) -> dict:
            return {
                "id": str(r.get("id")) if r.get("id") is not None else None,
                "created_at": str(r.get("created_at")),
                "tipo_lead": r.get("tipo_lead"),
                "nome": r.get("nome"),
                "telefone": r.get("telefone"),
                "email": r.get("email"),
                "origem": r.get("origem"),
                "imovel_interesse": r.get("imovel_interesse"),
                "perguntas_resumo": r.get("perguntas_resumo"),
                "potencial": r.get("potencial"),
                "resumo_geral": r.get("resumo_geral"),
                "dados": _json_preview(r.get("dados")),
            }

        imoveis_ctx: list[dict] = []
        try:
            imovel_resp = (
                client.table("mari_imoveis")
                .select(
                    "id,updated_at,status,tipo_imovel,operacao,condicao_imovel,"
                    "metragem_total_m2,cidade,uf,valor_pretendido_reais"
                )
                .in_("phone_e164", variants[:24])
                .order("updated_at", desc=True)
                .limit(5)
                .execute()
            )
            for ir in imovel_resp.data or []:
                imoveis_ctx.append(
                    {
                        "id": str(ir.get("id")) if ir.get("id") else None,
                        "updated_at": str(ir.get("updated_at")),
                        "status": ir.get("status"),
                        "tipo_imovel": ir.get("tipo_imovel"),
                        "operacao": ir.get("operacao"),
                        "condicao_imovel": ir.get("condicao_imovel"),
                        "metragem_total_m2": ir.get("metragem_total_m2"),
                        "cidade": ir.get("cidade"),
                        "uf": ir.get("uf"),
                        "valor_pretendido_reais": ir.get("valor_pretendido_reais"),
                    }
                )
        except Exception as exc:
            logger.warning("contexto: mari_imoveis omitido (%s)", exc)

        out = {
            "telefone_normalizado": phone,
            "variantes_consulta": variants,
            "leads_encontrados": len(matched_leads),
            "leads": [pack_lead(x) for x in matched_leads],
            "imoveis_cadastro": imoveis_ctx,
            "turnos_recentes_count": len(turn_rows),
            "turnos_recentes": [pack_turn(x) for x in turn_rows],
        }
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        logger.warning("contexto_lead_por_telefone falhou: %s", exc, exc_info=True)
        return json.dumps({"erro": str(exc)}, ensure_ascii=False)


def enviar_menu_interativo_uazapi(menu_json: str, numero: str = "") -> str:
    """
    Envia **menu interativo** no WhatsApp (botões, lista, enquete ou carrossel) via UAZAPI ``POST /send/menu``.

    menu_json: objeto JSON em string com ``type`` = ``button`` | ``list`` | ``poll`` | ``carousel``, ``text``, ``choices`` (array).
    Botões de resposta: cada item como ``"Texto visível|id_interno"`` (ver spec UAZAPI em ``specs/uazapi-openapi-spec``).
    Para ``type=list`` podes usar ``listButton`` e secções ``"[Título]"`` nas choices.

    numero: se vazio, usa o telefone do contexto (webhook WhatsApp).
    """
    from uazapi_client import build_send_menu_body, uazapi_configured, uazapi_post_json

    if not uazapi_configured():
        return json.dumps({"ok": False, "erro": "UAZAPI_BASE_URL ou UAZAPI_INSTANCE_TOKEN não configurados"}, ensure_ascii=False)
    try:
        spec = json.loads(menu_json) if (menu_json or "").strip() else {}
    except json.JSONDecodeError:
        return json.dumps({"ok": False, "erro": "menu_json não é JSON válido"}, ensure_ascii=False)
    if not isinstance(spec, dict):
        return json.dumps({"ok": False, "erro": "menu_json deve ser um objeto JSON"}, ensure_ascii=False)

    ctx = get_lead_context() or {}
    num = (numero or "").strip() or str(ctx.get("telefone_whatsapp") or "").strip()
    if not num:
        return json.dumps(
            {"ok": False, "erro": "numero vazio — passe no Studio ou use no WhatsApp com contexto."},
            ensure_ascii=False,
        )
    body = build_send_menu_body(num, spec)
    if not body:
        return json.dumps(
            {"ok": False, "erro": "campos inválidos — precisa type, text e choices (non-empty)"},
            ensure_ascii=False,
        )
    try:
        out = uazapi_post_json("send/menu", body, timeout=120.0)
        return json.dumps({"ok": True, "uazapi": out}, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        logger.warning("enviar_menu_interativo_uazapi: %s", exc, exc_info=True)
        return json.dumps({"ok": False, "erro": str(exc)}, ensure_ascii=False)


def solicitar_localizacao_whatsapp(texto_para_cliente: str, numero: str = "") -> str:
    """
    Envia pelo WhatsApp (UAZAPI) a mensagem com **botão para o cliente partilhar a localização atual**.
    Só funciona quando há instância UAZAPI configurada e, em geral, com ``telefone_whatsapp`` no contexto
    (canal WhatsApp). Se ``numero`` vier vazio, usa o telefone do contexto da sessão.

    texto_para_cliente: texto curto explicando por que precisas da localização (ex.: raio de imóveis).
    """
    from uazapi_client import send_location_button_sync, uazapi_configured

    if not uazapi_configured():
        return json.dumps(
            {"ok": False, "erro": "UAZAPI_BASE_URL ou UAZAPI_INSTANCE_TOKEN não configurados"},
            ensure_ascii=False,
        )
    ctx = get_lead_context() or {}
    num = (numero or "").strip() or str(ctx.get("telefone_whatsapp") or "").strip()
    if not num:
        return json.dumps(
            {
                "ok": False,
                "erro": "Número vazio — no Studio passa ``numero``; no WhatsApp o contexto traz o telefone.",
            },
            ensure_ascii=False,
        )
    msg = (texto_para_cliente or "").strip()
    if not msg:
        return json.dumps({"ok": False, "erro": "texto_para_cliente vazio"}, ensure_ascii=False)
    try:
        out = send_location_button_sync(num, msg, readchat=True)
        return json.dumps({"ok": True, "uazapi": out}, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        logger.warning("solicitar_localizacao_whatsapp falhou: %s", exc, exc_info=True)
        return json.dumps({"ok": False, "erro": str(exc)}, ensure_ascii=False)


def consultar_cep_viacep(cep: str) -> str:
    """
    Consulta endereço através do CEP (API pública ViaCEP). Devolve JSON com logradouro, bairro, localidade, uf ou erro.
    """
    d = "".join(c for c in (cep or "") if c.isdigit())
    if len(d) != 8:
        return json.dumps({"erro": "CEP deve ter exatamente 8 dígitos"}, ensure_ascii=False)
    try:
        url = f"https://viacep.com.br/ws/{d}/json/"
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.json()
        if not isinstance(data, dict):
            return json.dumps({"erro": "resposta inválida da ViaCEP"}, ensure_ascii=False)
        if data.get("erro") is True:
            return json.dumps({"erro": "CEP não encontrado"}, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("consultar_cep_viacep falhou: %s", exc, exc_info=True)
        return json.dumps({"erro": str(exc)}, ensure_ascii=False)


def _media_max_bytes() -> int:
    try:
        return max(256_000, min(int((os.getenv("MARIA_MEDIA_MAX_BYTES") or str(15 * 1024 * 1024)).strip()), 40 * 1024 * 1024))
    except ValueError:
        return 15 * 1024 * 1024


def _storage_safe_segment(s: str, max_len: int = 96) -> str:
    t = (s or "").strip()[:max_len]
    out = "".join(c if c.isalnum() or c in "-_" else "_" for c in t)
    return out or "x"


def _filename_for_media(url: str, nome_arquivo_sugerido: str) -> str:
    sug = (nome_arquivo_sugerido or "").strip()
    if sug:
        base = sug.rsplit("/", 1)[-1]
        return base[:200] if base else "arquivo.bin"
    path = urlparse(url).path
    base = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return base[:200] if base else "arquivo.bin"


def _resolve_lead_uuid(client: object, phone_digits: str, explicit_lead_id: str) -> str | None:
    lid = (explicit_lead_id or "").strip()
    if lid:
        try:
            UUID(lid)
            return lid
        except ValueError:
            return None
    if not phone_digits:
        return None
    try:
        lead_resp = (
            client.table("leads")
            .select("id,telefone,created_at")
            .order("created_at", desc=True)
            .limit(450)
            .execute()
        )
        for row in lead_resp.data or []:
            if _same_contact_phone(str(row.get("telefone") or ""), phone_digits):
                rid = row.get("id")
                return str(rid) if rid else None
    except Exception:
        logger.warning("_resolve_lead_uuid falhou", exc_info=True)
    return None


def registar_midia_url_no_lead(
    url_midia: str,
    tipo_lead: str,
    nome_arquivo_sugerido: str = "",
    content_type: str = "",
    lead_id: str = "",
    notas: str = "",
) -> str:
    """
    Descarrega uma URL de mídia (ex.: ficheiro WhatsApp com HTTPS), envia para o bucket Supabase Storage
    e regista linha em ``public.mari_lead_media``. Usa o telefone do contexto (WhatsApp) para pasta e para
    ligar ao último ``lead`` compatível quando ``lead_id`` não é passado.

    Requer Supabase + bucket criado (ver ``supabase/sql/mari_lead_media.sql``). Nome do bucket: env ``MARIA_STORAGE_BUCKET`` ou ``maria-lead-media``.
    """
    if not _supabase_configured():
        return json.dumps({"ok": False, "erro": "Supabase não configurado"}, ensure_ascii=False)
    tipo = _normalize_tipo_lead(str(tipo_lead or ""))
    if not tipo:
        return json.dumps(
            {"ok": False, "erro": "tipo_lead inválido — use cliente_final | proprietario | parceiro"},
            ensure_ascii=False,
        )
    url = (url_midia or "").strip()
    if not url.startswith(("http://", "https://")):
        return json.dumps({"ok": False, "erro": "url_midia deve ser http(s)"}, ensure_ascii=False)

    ctx = get_lead_context() or {}
    phone_raw = str(ctx.get("telefone_whatsapp") or "").strip()
    phone_digits = _digits_only(phone_raw)
    if not phone_digits:
        return json.dumps(
            {"ok": False, "erro": "Telefone ausente no contexto — disponível no webhook WhatsApp ou passe via Studio."},
            ensure_ascii=False,
        )

    max_b = _media_max_bytes()
    try:
        from supabase import create_client
    except ImportError:
        return json.dumps({"ok": False, "erro": "pacote supabase não instalado"}, ensure_ascii=False)

    bucket = (os.getenv("MARIA_STORAGE_BUCKET") or "maria-lead-media").strip()
    client = create_client(
        os.environ["SUPABASE_URL"].strip(),
        os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip(),
    )

    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as hc:
            r = hc.get(url)
            r.raise_for_status()
            body = r.content
        if len(body) > max_b:
            return json.dumps({"ok": False, "erro": f"download maior que {max_b} bytes"}, ensure_ascii=False)

        mime = (content_type or "").strip() or r.headers.get("content-type", "").split(";")[0].strip()
        if not mime:
            mime, _ = mimetypes.guess_type(_filename_for_media(url, nome_arquivo_sugerido))
        mime = mime or "application/octet-stream"

        fname = _filename_for_media(url, nome_arquivo_sugerido)
        uid = uuid4().hex[:12]
        seg_phone = _storage_safe_segment(phone_digits, 24)
        seg_tipo = _storage_safe_segment(tipo, 24)
        object_path = f"{seg_tipo}/{seg_phone}/{uid}_{_storage_safe_segment(fname, 160)}"

        file_opts = {"content-type": mime, "upsert": "false"}
        client.storage.from_(bucket).upload(object_path, body, file_opts)

        lid_resolved = _resolve_lead_uuid(client, phone_digits, lead_id)
        row = {
            "phone_e164": seg_phone[:32],
            "lead_id": lid_resolved,
            "tipo_lead": tipo,
            "storage_bucket": bucket,
            "object_path": object_path,
            "source_url": url[:2048],
            "content_type": mime[:200],
            "bytes_size": len(body),
            "notas": (notas or "").strip()[:2000] or None,
            "metadata": {},
        }
        ins = client.table("mari_lead_media").insert(row).execute()

        signed_url: str | None = None
        try:
            sign = client.storage.from_(bucket).create_signed_url(object_path, 604800)
            signed_url = sign.get("signedURL") or sign.get("signedUrl")
        except Exception:
            pass

        out = {
            "ok": True,
            "id": (ins.data or [{}])[0].get("id") if ins.data else None,
            "bucket": bucket,
            "object_path": object_path,
            "tipo_lead": tipo,
            "lead_id": lid_resolved,
            "bytes_size": len(body),
            "signed_url_7d": signed_url,
        }
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        logger.warning("registar_midia_url_no_lead falhou: %s", exc, exc_info=True)
        return json.dumps({"ok": False, "erro": str(exc)}, ensure_ascii=False)


def _norm_imovel_operacao(v: object) -> str | None:
    if v is None or v is False:
        return None
    s = str(v).strip().lower().replace("locação", "locacao").replace(" ", "_")
    if s in ("venda",):
        return "venda"
    if s in ("locacao", "aluguel", "aluguer"):
        return "locacao"
    if s in ("venda_e_locacao", "venda_elocacao", "venda+locacao"):
        return "venda_e_locacao"
    if "venda" in s and "loc" in s:
        return "venda_e_locacao"
    return None


def _norm_imovel_condicao(v: object) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("novo", "usado", "na_planta", "em_construcao"):
        return s
    if s in ("na planta", "em construção", "em construcao"):
        s2 = s.replace(" ", "_").replace("ç", "c").replace("ã", "a")
        if s2 == "em_construcao":
            return "em_construcao"
        if "planta" in s:
            return "na_planta"
    return None


def _to_float_opt(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int_opt(v: object) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_bool_opt(v: object) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "sim", "s"):
        return True
    if s in ("0", "false", "no", "não", "nao", "n"):
        return False
    return None


def _imovel_patch_from_dict(raw: dict) -> dict[str, object]:
    """Campos permitidos para insert/update em public.mari_imoveis."""
    out: dict[str, object] = {}
    str_fields = (
        "tipo_imovel",
        "endereco_completo",
        "cep",
        "logradouro",
        "numero",
        "complemento",
        "bairro",
        "cidade",
        "uf",
        "descricao_livre",
        "notas_internas",
    )
    for k in str_fields:
        if k not in raw:
            continue
        v = raw[k]
        if v is None:
            out[k] = None
        else:
            t = str(v).strip()
            out[k] = t if t else None
    if "uf" in out and isinstance(out["uf"], str) and out["uf"]:
        out["uf"] = out["uf"][:2].upper()

    if "operacao" in raw:
        no = _norm_imovel_operacao(raw["operacao"])
        if no:
            out["operacao"] = no
    if "condicao_imovel" in raw:
        nc = _norm_imovel_condicao(raw["condicao_imovel"])
        if nc:
            out["condicao_imovel"] = nc

    for k in ("metragem_total_m2", "metragem_util_m2", "valor_pretendido_reais", "condominio_reais", "iptu_reais"):
        if k in raw:
            out[k] = _to_float_opt(raw[k])

    for k in ("quartos", "banheiros", "vagas_garagem"):
        if k in raw:
            out[k] = _to_int_opt(raw[k])

    if "latitude" in raw:
        out["latitude"] = _to_float_opt(raw["latitude"])
    if "longitude" in raw:
        out["longitude"] = _to_float_opt(raw["longitude"])

    if "mobiliado" in raw:
        out["mobiliado"] = _to_bool_opt(raw["mobiliado"])
    if "aceita_permuta" in raw:
        out["aceita_permuta"] = _to_bool_opt(raw["aceita_permuta"])

    if "extras" in raw and raw["extras"] is not None:
        ex = raw["extras"]
        if isinstance(ex, dict):
            out["extras"] = ex

    if "status" in raw and raw["status"] is not None:
        st = str(raw["status"]).strip().lower()
        if st in ("rascunho", "pendente_validacao", "publicado", "arquivado"):
            out["status"] = st

    if "lead_id" in raw and raw["lead_id"]:
        lid = str(raw["lead_id"]).strip()
        try:
            UUID(lid)
            out["lead_id"] = lid
        except ValueError:
            pass

    return out


def _imovel_rows_for_phone(client: object, variants: list[str], *, limit: int) -> list[dict]:
    try:
        resp = (
            client.table("mari_imoveis")
            .select(
                "id,created_at,updated_at,status,phone_e164,tipo_imovel,operacao,condicao_imovel,"
                "metragem_total_m2,cidade,uf,valor_pretendido_reais,lead_id"
            )
            .in_("phone_e164", variants[:24])
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return list(resp.data or [])
    except Exception as exc:
        logger.warning("mari_imoveis list falhou (tabela criada?): %s", exc)
        return []


def _imovel_id_belongs_to_phone(client: object, imovel_id: str, variants: list[str]) -> bool:
    try:
        resp = (
            client.table("mari_imoveis")
            .select("id,phone_e164")
            .eq("id", imovel_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return False
        ph = str(rows[0].get("phone_e164") or "")
        if ph in variants:
            return True
        return any(_same_contact_phone(ph, v) for v in variants)
    except Exception:
        return False


def salvar_rascunho_imovel(dados_json: str = "{}", imovel_id: str = "") -> str:
    """
    Cria ou atualiza um **rascunho de imóvel** em ``public.mari_imoveis`` (Supabase).
    Usa o telefone do contexto (WhatsApp) como ``phone_e164``. Execute o SQL ``mari_imoveis.sql`` antes.

    dados_json: objeto JSON com campos opcionais, ex.:
    ``tipo_imovel``, ``operacao`` (venda|locacao|venda_e_locacao), ``condicao_imovel`` (novo|usado|na_planta|em_construcao),
    ``metragem_total_m2``, ``metragem_util_m2``, ``quartos``, ``banheiros``, ``vagas_garagem``,
    ``cep``, ``logradouro``, ``numero``, ``complemento``, ``bairro``, ``cidade``, ``uf``, ``endereco_completo``,
    ``latitude``, ``longitude``, ``valor_pretendido_reais``, ``condominio_reais``, ``iptu_reais``,
    ``descricao_livre``, ``mobiliado``, ``aceita_permuta``, ``extras`` (objeto), ``lead_id`` (uuid), ``status``.
    imovel_id: se vazio, insere novo rascunho; caso contrário atualiza esse id (tem de ser do mesmo contacto).
    """
    if not _supabase_configured():
        return json.dumps({"ok": False, "erro": "Supabase não configurado"}, ensure_ascii=False)
    try:
        from supabase import create_client
    except ImportError:
        return json.dumps({"ok": False, "erro": "pacote supabase não instalado"}, ensure_ascii=False)

    ctx = get_lead_context() or {}
    phone_raw = str(ctx.get("telefone_whatsapp") or "").strip()
    phone_digits = _digits_only(phone_raw)
    if not phone_digits:
        return json.dumps(
            {"ok": False, "erro": "Telefone ausente no contexto — use no WhatsApp ou passe sessão com telefone."},
            ensure_ascii=False,
        )
    variants = _phone_variants_for_query(phone_digits)
    seg_phone = variants[0] if variants else phone_digits[:32]

    try:
        raw = json.loads(dados_json) if dados_json else {}
    except json.JSONDecodeError:
        return json.dumps({"ok": False, "erro": "dados_json inválido"}, ensure_ascii=False)
    if not isinstance(raw, dict):
        return json.dumps({"ok": False, "erro": "dados_json deve ser objeto JSON"}, ensure_ascii=False)

    patch = _imovel_patch_from_dict(raw)
    client = create_client(
        os.environ["SUPABASE_URL"].strip(),
        os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip(),
    )

    iid = (imovel_id or "").strip()
    if iid:
        try:
            UUID(iid)
        except ValueError:
            return json.dumps({"ok": False, "erro": "imovel_id não é UUID válido"}, ensure_ascii=False)
        if not _imovel_id_belongs_to_phone(client, iid, variants):
            return json.dumps(
                {"ok": False, "erro": "imovel_id não encontrado ou não pertence a este contacto"},
                ensure_ascii=False,
            )
        if not patch:
            row_one = (
                client.table("mari_imoveis")
                .select(
                    "id,status,tipo_imovel,operacao,condicao_imovel,metragem_total_m2,cidade,uf,valor_pretendido_reais"
                )
                .eq("id", iid)
                .limit(1)
                .execute()
            )
            data = (row_one.data or [{}])[0]
            return json.dumps({"ok": True, "acao": "nada_a_atualizar", "imovel": data}, ensure_ascii=False, default=str)
        upd = client.table("mari_imoveis").update(patch).eq("id", iid).execute()
        row = (upd.data or [{}])[0] if upd.data else {}
        return json.dumps({"ok": True, "acao": "atualizado", "imovel_id": iid, "imovel": row}, ensure_ascii=False, default=str)

    row_ins: dict[str, object] = {"phone_e164": seg_phone[:64]}
    row_ins.update(patch)
    ins = client.table("mari_imoveis").insert(row_ins).execute()
    new_row = (ins.data or [{}])[0]
    nid = new_row.get("id")
    return json.dumps(
        {"ok": True, "acao": "criado", "imovel_id": str(nid) if nid else None, "imovel": new_row},
        ensure_ascii=False,
        default=str,
    )


def listar_imoveis_contato(telefone: str = "", limite: int = 8) -> str:
    """
    Lista rascunhos/registos de ``mari_imoveis`` para o telefone (contexto WhatsApp se ``telefone`` vazio).
    """
    if not _supabase_configured():
        return json.dumps({"ok": False, "erro": "Supabase não configurado"}, ensure_ascii=False)
    try:
        from supabase import create_client
    except ImportError:
        return json.dumps({"ok": False, "erro": "pacote supabase não instalado"}, ensure_ascii=False)

    phone_raw = (telefone or "").strip()
    if not phone_raw:
        ctx = get_lead_context() or {}
        phone_raw = str(ctx.get("telefone_whatsapp") or "").strip()
    phone_digits = _digits_only(phone_raw)
    if not phone_digits:
        return json.dumps({"ok": False, "erro": "telefone vazio"}, ensure_ascii=False)
    variants = _phone_variants_for_query(phone_digits)
    try:
        lim = int(limite)
    except (TypeError, ValueError):
        lim = 8
    lim = max(1, min(lim, 30))

    client = create_client(
        os.environ["SUPABASE_URL"].strip(),
        os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip(),
    )
    rows = _imovel_rows_for_phone(client, variants, limit=lim)
    return json.dumps(
        {"ok": True, "count": len(rows), "imoveis": rows},
        ensure_ascii=False,
        default=str,
    )


def anexar_midia_imovel(
    url_midia: str,
    imovel_id: str,
    legenda: str = "",
    nome_arquivo_sugerido: str = "",
    content_type: str = "",
) -> str:
    """
    Descarrega uma URL HTTPS (ex. ficheiro WhatsApp), envia para o Storage Supabase e regista em
    ``public.mari_imovel_midia`` ligado ao ``imovel_id``. O imóvel tem de pertencer ao telefone do contexto.
    """
    if not _supabase_configured():
        return json.dumps({"ok": False, "erro": "Supabase não configurado"}, ensure_ascii=False)
    iid = (imovel_id or "").strip()
    try:
        UUID(iid)
    except ValueError:
        return json.dumps({"ok": False, "erro": "imovel_id inválido"}, ensure_ascii=False)

    ctx = get_lead_context() or {}
    phone_raw = str(ctx.get("telefone_whatsapp") or "").strip()
    phone_digits = _digits_only(phone_raw)
    if not phone_digits:
        return json.dumps({"ok": False, "erro": "Telefone ausente no contexto"}, ensure_ascii=False)
    variants = _phone_variants_for_query(phone_digits)

    url = (url_midia or "").strip()
    if not url.startswith(("http://", "https://")):
        return json.dumps({"ok": False, "erro": "url_midia deve ser http(s)"}, ensure_ascii=False)

    try:
        from supabase import create_client
    except ImportError:
        return json.dumps({"ok": False, "erro": "pacote supabase não instalado"}, ensure_ascii=False)

    client = create_client(
        os.environ["SUPABASE_URL"].strip(),
        os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip(),
    )
    if not _imovel_id_belongs_to_phone(client, iid, variants):
        return json.dumps(
            {"ok": False, "erro": "imovel_id não encontrado ou não pertence a este contacto"},
            ensure_ascii=False,
        )

    max_b = _media_max_bytes()
    bucket = (os.getenv("MARIA_STORAGE_BUCKET") or "maria-lead-media").strip()
    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as hc:
            r = hc.get(url)
            r.raise_for_status()
            body = r.content
        if len(body) > max_b:
            return json.dumps({"ok": False, "erro": f"ficheiro maior que {max_b} bytes"}, ensure_ascii=False)

        mime = (content_type or "").strip() or r.headers.get("content-type", "").split(";")[0].strip()
        if not mime:
            mime, _ = mimetypes.guess_type(_filename_for_media(url, nome_arquivo_sugerido))
        mime = mime or "application/octet-stream"

        fname = _filename_for_media(url, nome_arquivo_sugerido)
        uid = uuid4().hex[:12]
        object_path = f"imovel/{iid}/{uid}_{_storage_safe_segment(fname, 160)}"
        file_opts = {"content-type": mime, "upsert": "false"}
        client.storage.from_(bucket).upload(object_path, body, file_opts)

        row = {
            "imovel_id": iid,
            "phone_e164": _storage_safe_segment(phone_digits, 32),
            "storage_bucket": bucket,
            "object_path": object_path,
            "source_url": url[:2048],
            "content_type": mime[:200],
            "bytes_size": len(body),
            "legenda": (legenda or "").strip()[:2000] or None,
            "metadata": {},
        }
        ins = client.table("mari_imovel_midia").insert(row).execute()
        signed_url: str | None = None
        try:
            sign = client.storage.from_(bucket).create_signed_url(object_path, 604800)
            signed_url = sign.get("signedURL") or sign.get("signedUrl")
        except Exception:
            pass
        out = {
            "ok": True,
            "id": (ins.data or [{}])[0].get("id") if ins.data else None,
            "imovel_id": iid,
            "bucket": bucket,
            "object_path": object_path,
            "signed_url_7d": signed_url,
            "bytes_size": len(body),
        }
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.warning("anexar_midia_imovel falhou: %s", exc, exc_info=True)
        return json.dumps({"ok": False, "erro": str(exc)}, ensure_ascii=False)


def _extension_for_image_mime(mime: str) -> str:
    base = (mime or "").split(";")[0].strip().lower()
    tab = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }
    return tab.get(base, "jpg")


def _attach_uazapi_media_to_latest_imovel() -> bool:
    return os.getenv("MARIA_ATTACH_UAZAPI_MEDIA_TO_LATEST_IMOVEL", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def persist_uazapi_downloaded_images_sync(
    phone_e164: str,
    images: list[Any],
    wa_message_id: str | None = None,
) -> str:
    """
    Grava no Storage + ``mari_lead_media`` imagens já obtidas via UAZAPI ``message/download``
    (Agno ``Image`` com ``content`` em bytes). Usado pelo webhook WhatsApp — **não** precisa de URL pública.

    Desliga com ``MARIA_PERSIST_UAZAPI_DOWNLOAD_MEDIA=0``. Tipo de lead no CRM: ``MARIA_INBOUND_DOWNLOAD_MEDIA_TIPO_LEAD``
    (default ``proprietario``). Com ``MARIA_ATTACH_UAZAPI_MEDIA_TO_LATEST_IMOVEL=1`` também insere em
    ``mari_imovel_midia`` quando existir rascunho recente do contacto.
    """
    if not _supabase_configured():
        return json.dumps({"ok": False, "skipped": True, "motivo": "supabase_desligado"}, ensure_ascii=False)
    if os.getenv("MARIA_PERSIST_UAZAPI_DOWNLOAD_MEDIA", "1").strip().lower() in ("0", "false", "no", "off"):
        return json.dumps(
            {"ok": True, "skipped": True, "motivo": "MARIA_PERSIST_UAZAPI_DOWNLOAD_MEDIA=0"},
            ensure_ascii=False,
        )

    phone_digits = _digits_only(phone_e164 or "")
    if not phone_digits:
        return json.dumps({"ok": False, "erro": "phone_e164 vazio"}, ensure_ascii=False)

    tipo_raw = (os.getenv("MARIA_INBOUND_DOWNLOAD_MEDIA_TIPO_LEAD") or "proprietario").strip()
    tipo = _normalize_tipo_lead(tipo_raw) or "proprietario"

    try:
        from supabase import create_client
    except ImportError:
        return json.dumps({"ok": False, "erro": "supabase não instalado"}, ensure_ascii=False)

    max_b = _media_max_bytes()
    bucket = (os.getenv("MARIA_STORAGE_BUCKET") or "maria-lead-media").strip()
    variants = _phone_variants_for_query(phone_digits)
    seg_phone = _storage_safe_segment(phone_digits, 24)
    seg_tipo = _storage_safe_segment(tipo, 24)
    mid = (wa_message_id or "").strip() or "sem_id"
    mid_seg = _storage_safe_segment(mid, 64)

    client = create_client(
        os.environ["SUPABASE_URL"].strip(),
        os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip(),
    )
    lid_resolved = _resolve_lead_uuid(client, phone_digits, "")

    imovel_latest: str | None = None
    if _attach_uazapi_media_to_latest_imovel():
        rows = _imovel_rows_for_phone(client, variants, limit=1)
        if rows and rows[0].get("id"):
            imovel_latest = str(rows[0]["id"])

    saved: list[dict[str, Any]] = []
    for img in images or []:
        content = getattr(img, "content", None)
        if not isinstance(content, (bytes, bytearray)) or len(content) == 0:
            continue
        body = bytes(content)
        if len(body) > max_b:
            logger.warning("persist_uazapi_download: skip oversized | bytes=%s", len(body))
            continue
        mime = getattr(img, "mime_type", None) or ""
        mime = str(mime).split(";")[0].strip() or "image/jpeg"
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        ext = _extension_for_image_mime(mime)
        uid = uuid4().hex[:12]
        object_path = f"uazapi_dl/{seg_tipo}/{seg_phone}/{mid_seg}_{uid}.{ext}"
        try:
            file_opts = {"content-type": mime, "upsert": "false"}
            client.storage.from_(bucket).upload(object_path, body, file_opts)
        except Exception as exc:
            logger.warning("persist_uazapi_download: upload falhou | path=%s | err=%s", object_path, exc)
            continue

        meta: dict[str, Any] = {"source": "uazapi_message_download"}
        if mid != "sem_id":
            meta["wa_message_id"] = mid
        row_lead = {
            "phone_e164": seg_phone[:32],
            "lead_id": lid_resolved,
            "tipo_lead": tipo,
            "storage_bucket": bucket,
            "object_path": object_path,
            "source_url": None,
            "content_type": mime[:200],
            "bytes_size": len(body),
            "notas": "auto webhook (message/download)",
            "metadata": meta,
        }
        lid_media = None
        try:
            ins_lead = client.table("mari_lead_media").insert(row_lead).execute()
            lid_media = (ins_lead.data or [{}])[0].get("id") if ins_lead.data else None
        except Exception as exc:
            logger.warning("persist_uazapi_download: mari_lead_media insert | err=%s", exc)

        im_mid: str | None = None
        if imovel_latest:
            try:
                meta_im: dict[str, Any] = {"source": "uazapi_message_download"}
                if lid_media:
                    meta_im["mari_lead_media_id"] = str(lid_media)
                row_im = {
                    "imovel_id": imovel_latest,
                    "phone_e164": seg_phone[:32],
                    "storage_bucket": bucket,
                    "object_path": object_path,
                    "source_url": None,
                    "content_type": mime[:200],
                    "bytes_size": len(body),
                    "legenda": None,
                    "metadata": meta_im,
                }
                ins_im = client.table("mari_imovel_midia").insert(row_im).execute()
                im_mid = (ins_im.data or [{}])[0].get("id") if ins_im.data else None
            except Exception as exc:
                logger.warning("persist_uazapi_download: mari_imovel_midia | err=%s", exc)

        saved.append(
            {
                "mari_lead_media_id": str(lid_media) if lid_media else None,
                "mari_imovel_midia_id": str(im_mid) if im_mid else None,
                "object_path": object_path,
                "bytes_size": len(body),
            }
        )

    return json.dumps(
        {"ok": True, "count": len(saved), "imovel_id_attached": imovel_latest, "items": saved},
        ensure_ascii=False,
        default=str,
    )


def obter_detalhes_chat_uazapi(numero: str, preview_imagem: bool = False) -> str:
    """
    UAZAPI ``POST /chat/details`` — devolve JSON com o modelo Chat completo (wa_name, lead_*, grupo, etc.).
    Usa o mesmo token da instância que o webhook. Útil para memória / contexto antes de responder ou registar lead.

    numero: telefone com país, ex. ``5511999999999`` ou JID de grupo conforme spec.
    preview_imagem: ``true`` para URL de foto menor; ``false`` (padrão) para URL full.
    """
    try:
        from uazapi_client import fetch_chat_details_sync

        out = fetch_chat_details_sync((numero or "").strip(), preview_image=bool(preview_imagem))
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"erro": str(exc)}, ensure_ascii=False)


def obter_fatos_do_anuncio(identificador_anuncio: str) -> str:
    """
    Retorna fatos confiáveis do imóvel (condomínio, disponibilidade, preço anunciado, etc.).
    Use apenas estes valores ao citar números ou disponibilidade ao cliente.
    Se o backend não estiver ligado, não haverá valores numéricos — nunca invente.
    """
    payload = {
        "identificador_anuncio": identificador_anuncio,
        "condominio_reais": None,
        "disponivel": None,
        "detalhes": (
            "Backend de anúncios não conectado. Não cite valores de condomínio nem confirme "
            "disponibilidade; informe que vai confirmar com o corretor."
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def registrar_lead_rascunho(
    tipo_lead: str,
    nome: str,
    telefone: str,
    resumo_geral: str,
    dados_json: str = "{}",
    email: str | None = None,
    origem: str | None = None,
    imovel_interesse: str | None = None,
    perguntas_resumo: str | None = None,
    midias_enviadas: bool | None = None,
    pediu_visita: bool | None = None,
    urgencia: bool | None = None,
) -> str:
    """
    Registra o lead em data/leads.jsonl e, se SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY
    estiverem definidos, também insere na tabela public.leads (ver supabase/sql/leads.sql).

    tipo_lead: cliente_final | proprietario | parceiro
    dados_json: JSON com campos extras (cidade_bairro, valor_pedido, operacao, intencao, etc.)
    """
    _ensure_data_dir()
    try:
        extra = json.loads(dados_json) if dados_json else {}
    except json.JSONDecodeError:
        extra = {"_parse_error": "dados_json inválido"}

    ctx = get_lead_context()
    if ctx:
        extra.setdefault("canal", ctx.get("canal"))
        if ctx.get("telefone_whatsapp"):
            extra.setdefault("telefone_whatsapp", ctx["telefone_whatsapp"])

    tipo_raw = (tipo_lead or "").strip()
    tipo_for_storage = _normalize_tipo_lead(tipo_raw) or "cliente_final"
    if _normalize_tipo_lead(tipo_raw) is None and tipo_raw:
        extra.setdefault("_tipo_lead_original_invalido", tipo_raw)

    potencial = calcular_potencial_sdr(
        tipo_lead=tipo_for_storage,
        dados_completos=_infer_completeness(extra, tipo_for_storage),
        pediu_visita=bool(pediu_visita),
        urgencia=bool(urgencia),
        midias=bool(midias_enviadas),
    )

    tel = (telefone or "").strip()
    if not tel and ctx and ctx.get("telefone_whatsapp"):
        tel = str(ctx["telefone_whatsapp"]).strip()

    row = {
        "ts": datetime.now(UTC).isoformat(),
        "tipo_lead": tipo_for_storage,
        "nome": nome,
        "telefone": tel or telefone,
        "email": email,
        "origem": origem,
        "imovel_interesse": imovel_interesse,
        "perguntas_resumo": perguntas_resumo,
        "midias_enviadas": midias_enviadas,
        "pediu_visita": pediu_visita,
        "urgencia": urgencia,
        "potencial": potencial,
        "resumo_geral": resumo_geral,
        "dados": extra,
    }
    with LEADS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    out: dict = {
        "status": "gravado_localmente",
        "arquivo": str(LEADS_FILE),
        "potencial": potencial,
    }

    if _supabase_configured():
        ok, err = _insert_lead_supabase(row, extra)
        if ok:
            out["supabase"] = "gravado_em_public.leads"
        else:
            out["supabase"] = "falhou"
            out["supabase_erro"] = err
            logger.warning("registrar_lead_rascunho: Supabase falhou — %s", err)

    return json.dumps(out, ensure_ascii=False)


def calcular_potencial_sdr(
    tipo_lead: str,
    dados_completos: bool,
    pediu_visita: bool,
    urgencia: bool,
    midias: bool,
) -> str:
    """
    Retorna potencial ALTO, MEDIO ou BAIXO com regras determinísticas (alinhado ao POP).
    Use após coletar os sinais do atendimento ou para conferir consistência.
    """
    score = 0
    if dados_completos:
        score += 2
    if pediu_visita:
        score += 2
    if urgencia:
        score += 1
    if midias:
        score += 1
    if tipo_lead == "cliente_final" and pediu_visita:
        score += 1

    if score >= 4:
        return "ALTO"
    if score >= 2:
        return "MEDIO"
    return "BAIXO"


def _infer_completeness(extra: dict, tipo_lead: str) -> bool:
    if tipo_lead == "proprietario":
        keys = ["cidade_bairro", "tamanho_aproximado", "valor_pedido", "operacao"]
        return sum(1 for k in keys if extra.get(k) not in (None, "", "Não informado")) >= 3
    if tipo_lead == "parceiro":
        return bool(extra.get("intencao"))
    if tipo_lead == "cliente_final":
        return bool(extra.get("interesse_claro") or extra.get("nome_confirmado"))
    return False
