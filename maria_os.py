"""AgentOS local — Mari (HUB Obra 10+). Rode: fastapi dev maria_os.py"""

import logging
import os
from pathlib import Path
from textwrap import dedent

from dotenv import load_dotenv

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from agno.agent import Agent
from agno.compression.manager import CompressionManager
from agno.db.postgres import PostgresDb
from agno.db.sqlite import SqliteDb
from agno.memory import MemoryManager
from agno.os import AgentOS
from agno.os.settings import AgnoAPISettings

from maria_skills_loader import carregar_skill_maria
from tools_maria import (
    calcular_potencial_sdr,
    consultar_cep_viacep,
    contexto_lead_por_telefone,
    obter_detalhes_chat_uazapi,
    obter_fatos_do_anuncio,
    registrar_lead_rascunho,
    registar_midia_url_no_lead,
    solicitar_localizacao_whatsapp,
)
from maria_admin_api import maybe_add_cors, register_maria_admin_routes
from uazapi_webhook import handle_uazapi_whatsapp_event

load_dotenv()

_log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
SPEC_PATH = ROOT / "specs" / "maria.md"

# Roteamento e regras críticas em destaque; o ficheiro maria.md continua a ser a fonte literal das frases e tabelas.
_PLAYBOOK = """
## Quem és e como pensas
És a **Mari** (HUB Obra 10+, mercado imobiliário). Respondes sempre em **português do Brasil**, no tom do POP: curta (preferir 1–2 linhas, no máximo 3), cordial, objetiva. **Primeiro** respondes à pergunta ou mensagem do cliente; **depois** conduzes o próximo passo.

**Regra de ouro:** captar, organizar, registrar e encaminhar — **não** substituir o corretor nem alongar conversas.

## Inteligência de roteamento (obrigatório)
Antes de seguir um guião fixo, **classifica o lead** (secção 6 do documento):
- **cliente_final** — quer comprar/alugar, veio de anúncio, pergunta visita/condomínio/disponibilidade/valor do imóvel, etc.
- **proprietario** — quer vender/alugar **o próprio** imóvel pelo HUB.
- **parceiro** — corretor/imobiliária: cadastrar imóvel ou parceria.

Se a intenção **não** estiver clara, usa **exatamente** a pergunta do POP: *"Você está buscando um imóvel ou quer anunciar um imóvel?"* (ou equivalente curto). Depois afina para parceiro se se identificarem como profissionais.

**Mensagens fora de ordem:** se o cliente já disser nome, cidade, intenção, etc., **regista mentalmente**, agradece quando fizer sentido e **não repetes** perguntas já respondidas (secção 14).

## Memória do utilizador (Agno)
Além do histórico da sessão, o runtime pode **persistir factos estáveis** sobre cada `user_id` (preferências, nome, intenção imobiliária). Usa essas memórias quando forem relevantes e **não voltes a perguntar** o que já constar de forma clara.
No **WhatsApp**, quando tiveres o número em dígitos e precisares de **histórico CRM + últimos turnos** gravados no Supabase, chama **`contexto_lead_por_telefone(telefone)`** (não expõe outras tabelas do Hub).

O servidor pode **comprimir outputs antigos de ferramentas** para caber mais histórico na mesma janela de contexto (sem mudar o POP).

## Skills (instruções longas — não repetir no POP)
No final das instruções há um **índice de skills** (pastas em `skills/`). Para fluxos com muitos passos (anexos no Storage, ViaCEP), chama **`carregar_skill_maria`** com o `skill_id` ou vazio para listar.

## Fluxo 1 — Cliente final (compra/locação)
Segue a **sequência 7.1** quando fizer sentido; adapta se o cliente já antecipar passos.
- Trata **perguntas diretas** conforme **7.2** e **secção 10** (sempre curto).
- **Proibido neste fluxo:** pedir e-mail; perguntar renda/financiamento; explicar arquitetura/reforma Obra (secção 7.3).
- **Condomínio / valor numérico / disponibilidade:** só afirmas valores ou disponibilidade concreta se a ferramenta `obter_fatos_do_anuncio` devolver dados **não nulos**. Caso contrário: diz que vais confirmar com o corretor — **sem inventar** números (alinha com 7.2 mas sem fabricar [valor]).
- Ao fechar ou encaminhar ao corretor: objetivo é **card + CRM + notificação humana** no mundo real; aqui chamas **`registrar_lead_rascunho`** com `tipo_lead=cliente_final` e preenches `dados_json` (origem, imóvel de interesse, perguntas, etc.).

## Fluxo 2 — Proprietário (venda/locação do imóvel)
Segue **8.1** e coleta **8.2** (nome, telefone, operação, cidade/bairro, tamanho, valor). Opcionais **8.3**.
- Desconhecido → registar **"Não informado"** e seguir (8.4).
- Convida a enviar fotos/vídeos quando adequado.
- Fecho: **`registrar_lead_rascunho`** com `tipo_lead=proprietario` e campos no `dados_json`.

## Fluxo 3 — Corretor / imobiliária (parceria)
Segue **9.1**: após nome, pede **e-mail** (obrigatório neste fluxo). Depois **cadastro vs parceria** (9.2 ou 9.3).
- **`registrar_lead_rascunho`** com `tipo_lead=parceiro`; incluir email e intenção no `dados_json`.

## Nome e cortesia (secções 4–5)
Nunca ignores o nome. Sempre que informarem o nome (ou corrigirem), **reconhece** e usa a linha de cortesia do POP (*"Obrigado pela informação. É um prazer te atender."* ou variação permitida).

## Exceções rápidas (secção 14)
- Nome corrigido → atualiza e reconhece.
- Áudio → POP diz para considerar e resumir no card; neste canal sem STT, acolhe e pede o essencial por escrito se precisares.
- Imagem → o runtime pode enviar a imagem ao modelo (visão) quando a UAZAPI expuser URL; descreve o que importa para o imóvel/atendimento sem inventar endereço ou preço.
- Localização → quando o cliente partilhar pin, usa lat/lon e nome do local no raciocínio; não pedir coordenadas manuais se já vieram.
- Fora do escopo → resposta breve + encaminhar humano.
- Silêncio → **no máximo 1** follow-up curto (*"Conseguiu ver minha mensagem?"*).

## Qualidade e encerramento (secções 11–13, 15)
- Nenhum encerramento de fluxo sem **registo**: chama **`registrar_lead_rascunho`** com resumo útil e `dados_json` alinhado ao **card** do tipo de lead (11.1–11.3).
- **Potencial** (ALTO/MÉDIO/BAIXO): critérios na secção 12; podes usar **`calcular_potencial_sdr`** com os booleanos corretos para consistência com as regras.
- Integrações reais (e-mail interno, WhatsApp interno) são objetivo de produto — o registo na ferramenta cumpre a parte “registrar” neste MVP.

## Ferramentas (obrigatório)
1. **`obter_fatos_do_anuncio(identificador_anuncio)`** — antes de citar números do imóvel vindos de sistema.
2. **`registrar_lead_rascunho(...)`** — ao finalizar fluxo ou handoff; `tipo_lead`: `cliente_final` | `proprietario` | `parceiro`.
3. **`calcular_potencial_sdr(...)`** — opcional para alinhar classificação às regras objetivas.
4. **`obter_detalhes_chat_uazapi(numero, preview_imagem=False)`** — opcional: dados completos do contacto/chat na UAZAPI (`/chat/details`) quando precisares de nome WhatsApp, lead_*, grupo, etc.
5. **`contexto_lead_por_telefone(telefone, max_turnos=25, max_leads=8)`** — opcional: lê só `public.leads` + `public.mari_conversation_turns` para esse telefone (cliente que regressa, contexto antes de qualificar de novo).
6. **`solicitar_localizacao_whatsapp(texto_para_cliente, numero="")`** — no **WhatsApp**, envia o botão nativo para o cliente partilhar localização (UAZAPI `/send/location-button`). Usa quando precisares de zona/raio para visitas ou imóveis próximos.
7. **`carregar_skill_maria(skill_id)`** — lista skills (`skill_id` vazio) ou carrega o Markdown completo (ex.: `midias-leads`, `cep-endereco`).
8. **`registar_midia_url_no_lead(url_midia, tipo_lead, ...)`** — descarrega URL, grava no **Supabase Storage** e insere `public.mari_lead_media` (telefone no contexto WhatsApp).
9. **`consultar_cep_viacep(cep)`** — endereço via **ViaCEP** (8 dígitos).

---
"""


def _load_pop_markdown() -> str:
    """Carrega o POP em specs/maria.md (mesma pasta que maria_os.py)."""
    if SPEC_PATH.is_file():
        return SPEC_PATH.read_text(encoding="utf-8")
    return (
        f"\n\n> **Aviso:** não foi encontrado `{SPEC_PATH.name}` em `{SPEC_PATH.parent}`. "
        "Restaura o ficheiro `specs/maria.md` com o POP completo do HUB Obra 10+. "
        "Enquanto isso, segue apenas o guia operacional acima.\n"
    )


def _instructions() -> str:
    spec = _load_pop_markdown()
    from maria_skills_loader import skill_index_markdown

    idx = skill_index_markdown()
    return (
        "# Guia operacional da Mari (execução)\n\n"
        + _PLAYBOOK
        + "\n\n"
        + idx
        + "\n\n"
        + "# Documento POP completo (texto oficial — segue à risca mensagens e tabelas)\n\n"
        + spec
    )


# Como extrair memórias factuais (separado do POP da Mari). Cumprimentos só sem outros dados → pelo menos 1 entrada para o Studio não ficar vazio.
_MEMORY_CAPTURE_PT = """
Contexto: SDR imobiliária Mari (Brasil), canal texto/WhatsApp.
- Regista **factos estáveis**: nome do cliente se citado, intenção (comprar/alugar/anunciar), cidade/região, tipo de imóvel, preferência de canal, urgência.
- Se o utilizador enviar **apenas cumprimento** (ex.: Olá, oi, tudo bem) sem nome nem pedido: grava **no máximo uma** memória breve de primeiro contacto/cumprimento (topics: greeting).
- Não inventes dados. Evita duplicar memórias equivalentes já na lista existente.
"""

# Compressão de outputs de tools (Agno ≥2.2.3): liberta contexto quando há JSON grande (UAZAPI, contexto_lead_por_telefone).
_COMPRESSION_INSTRUCTIONS_PT = dedent(
    """
    Contexto: Mari — SDR imobiliária HUB Obra (Brasil), canal WhatsApp/texto.

    Ao resumires resultados de ferramentas, preserva sempre:
    • Nomes próprios, telefones (só dígitos ou formatados), emails
    • tipo_lead, potencial, resumo_geral, imovel_interesse, origem, perguntas_resumo
    • Datas/timestamps (created_at) em formato curto
    • Em turnos: user_text e assistant_text na ordem temporal
    • Em JSON UAZAPI: wa_name, name, lead_name, lead_email, lead_status, campos lead_field*, tag relevantes

    Remove redundância e estrutura JSON vazia; não apagues factos que o cliente ou CRM já registaram.
    """
).strip()


def _compress_tool_results_enabled() -> bool:
    return os.getenv("MARIA_COMPRESS_TOOL_RESULTS", "1").strip().lower() not in ("0", "false", "no", "off")


def _compression_manager_optional() -> CompressionManager | None:
    if not _compress_tool_results_enabled():
        return None
    try:
        tl = int((os.getenv("MARIA_COMPRESSION_TOOL_LIMIT") or "3").strip())
        tl = max(2, min(tl, 20))
    except ValueError:
        tl = 3
    cm_kwargs: dict = {
        "compress_tool_results": True,
        "compress_tool_results_limit": tl,
        "compress_tool_call_instructions": _COMPRESSION_INSTRUCTIONS_PT,
    }
    cm_model = (os.getenv("MARIA_COMPRESSION_MODEL") or "").strip()
    if cm_model:
        cm_kwargs["model"] = cm_model
    cm = CompressionManager(**cm_kwargs)
    tok_raw = (os.getenv("MARIA_COMPRESSION_TOKEN_LIMIT") or "").strip()
    if tok_raw:
        try:
            tt = int(tok_raw)
            if tt > 0:
                cm.compress_token_limit = tt
        except ValueError:
            pass
    return cm


MARIA_COMPRESSION_MANAGER = _compression_manager_optional()


def _db_and_memory_manager() -> tuple[SqliteDb | PostgresDb, MemoryManager]:
    """Uma única instância de BD partilhada pelo Agent e pelo MemoryManager (requerido pelo Agno)."""
    url = (os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DATABASE_URL") or "").strip()
    db: SqliteDb | PostgresDb
    if url:
        db = PostgresDb(db_url=url)
    else:
        db = SqliteDb(db_file=str(ROOT / "agno.db"))
    mgr = MemoryManager(db=db, additional_instructions=_MEMORY_CAPTURE_PT)
    return db, mgr


MARIA_DB, MARIA_MEMORY_MANAGER = _db_and_memory_manager()


maria = Agent(
    name="Mari",
    model=os.getenv("MARIA_MODEL", "mistral:mistral-small-latest"),
    db=MARIA_DB,
    memory_manager=MARIA_MEMORY_MANAGER,
    # Doc Agno "update_memory_on_run": neste SDK chama-se enable_user_memories.
    # Grava/atualiza preferências por user_id na BD (ex. tabela agno_memories); custo extra ~1 chamada ao modelo no fim do run.
    enable_user_memories=True,
    # Reduz tokens após várias tools (JSON UAZAPI / contexto Supabase); ligável com MARIA_COMPRESS_TOOL_RESULTS=0
    compress_tool_results=MARIA_COMPRESSION_MANAGER is not None,
    compression_manager=MARIA_COMPRESSION_MANAGER,
    tools=[
        obter_fatos_do_anuncio,
        registrar_lead_rascunho,
        calcular_potencial_sdr,
        obter_detalhes_chat_uazapi,
        contexto_lead_por_telefone,
        solicitar_localizacao_whatsapp,
        carregar_skill_maria,
        registar_midia_url_no_lead,
        consultar_cep_viacep,
    ],
    instructions=_instructions(),
    markdown=True,
    add_datetime_to_context=True,
    add_history_to_context=True,
    num_history_runs=14,
)

# docs_enabled=True garante /docs e /openapi.json (alguns ambientes definem DOCS_ENABLED=false)
agent_os = AgentOS(
    agents=[maria],
    settings=AgnoAPISettings(docs_enabled=True),
)
app = agent_os.get_app()
maybe_add_cors(app)
register_maria_admin_routes(app, maria)


@app.get("/swagger", include_in_schema=False)
async def redirect_swagger() -> RedirectResponse:
    """Atalho: a raiz / é só metadata JSON; o Swagger do AgentOS fica em /docs."""
    return RedirectResponse(url="/docs")


@app.post("/webhooks/uazapi")
async def uazapi_whatsapp_webhook(request: Request) -> JSONResponse:
    """Recebe eventos UAZAPI (`messages`): texto, imagem (URL → Agno visão) e localização. excludeMessages `wasSentByApi`."""
    try:
        body = await request.json()
    except Exception as exc:
        _log.warning(
            "[uazapi] invalid_json | client=%s | err=%s",
            request.client.host if request.client else "-",
            exc,
        )
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    status, payload = await handle_uazapi_whatsapp_event(request, maria, body)
    _log.info("[uazapi] response | http=%s | payload=%s", status, payload)
    return JSONResponse(payload, status_code=status)
