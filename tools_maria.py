"""Ferramentas da Mari — leads locais (JSONL) e opcionalmente Supabase."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from datetime import UTC, datetime
from pathlib import Path
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

        out = {
            "telefone_normalizado": phone,
            "variantes_consulta": variants,
            "leads_encontrados": len(matched_leads),
            "leads": [pack_lead(x) for x in matched_leads],
            "turnos_recentes_count": len(turn_rows),
            "turnos_recentes": [pack_turn(x) for x in turn_rows],
        }
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        logger.warning("contexto_lead_por_telefone falhou: %s", exc, exc_info=True)
        return json.dumps({"erro": str(exc)}, ensure_ascii=False)


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
