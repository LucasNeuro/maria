"""Ferramentas da Mari — leads locais (JSONL) e opcionalmente Supabase."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

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
    if not (str(ut or "").strip() and str(ar or "").strip()):
        return False, "user_payload.text ou assistant_payload.text vazio"

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
