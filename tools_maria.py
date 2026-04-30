"""Ferramentas da Mari — leads locais (JSONL) e opcionalmente Supabase."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LEADS_FILE = DATA_DIR / "leads.jsonl"


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _supabase_configured() -> bool:
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    return bool(url and key)


def _insert_lead_supabase(row: dict, extra: dict) -> tuple[bool, str | None]:
    """Insert na tabela public.leads. Retorna (ok, mensagem_erro)."""
    try:
        from supabase import create_client
    except ImportError:
        return False, "pacote supabase não instalado"

    try:
        client = create_client(
            os.environ["SUPABASE_URL"].strip(),
            os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip(),
        )
        payload = {
            "created_at": row["ts"],
            "tipo_lead": row["tipo_lead"],
            "nome": row["nome"],
            "telefone": row["telefone"],
            "email": row["email"],
            "origem": row["origem"],
            "imovel_interesse": row["imovel_interesse"],
            "perguntas_resumo": row["perguntas_resumo"],
            "midias_enviadas": row["midias_enviadas"],
            "pediu_visita": row["pediu_visita"],
            "urgencia": row["urgencia"],
            "potencial": row["potencial"],
            "resumo_geral": row["resumo_geral"],
            "dados": extra,
        }
        client.table("leads").insert(payload).execute()
        return True, None
    except Exception as exc:  # noqa: BLE001 — tool deve devolver texto ao modelo
        return False, str(exc)


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

    potencial = calcular_potencial_sdr(
        tipo_lead=tipo_lead,
        dados_completos=_infer_completeness(extra, tipo_lead),
        pediu_visita=bool(pediu_visita),
        urgencia=bool(urgencia),
        midias=bool(midias_enviadas),
    )

    row = {
        "ts": datetime.now(UTC).isoformat(),
        "tipo_lead": tipo_lead,
        "nome": nome,
        "telefone": telefone,
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
