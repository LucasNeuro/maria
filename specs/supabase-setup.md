# Supabase + Render + GitHub (Mari)

Este documento junta **onde criar o quê** e **que chaves** pões na Render.

---

## Precisas de repositório Git (ex. GitHub)?

**Sim.** A Render faz deploy a partir de código em **Git** (GitHub, GitLab ou Bitbucket): fizeste push do projeto **maria** para um repo e ligas esse repo ao serviço na Render. Sem repo na nuvem, não há deploy automático típico.

---

## Na Render: que opção escolher no menu **+ New**?

| Opção | Quando usar para a Mari |
|--------|-------------------------|
| **Blueprint** | Já tens **`render.yaml`** na raiz — a Render cria o **Web Service** Docker sozinha. Bom para repetir ambiente. |
| **Web Service** | Mesmo resultado manual: runtime **Docker**, `Dockerfile` na raiz, health **`/health`**. |
| **Postgres** | **Não é obrigatório** se usares **Supabase**: a base de dados fica no Supabase (Postgres gerido). Só criarias Postgres na Render se quisesses BD na própria Render em vez do Supabase. |

**Static Site**, **Worker**, **Cron**, **Key Value** não são o serviço principal da API AgentOS da Mari.

---

## Fluxo Supabase (projeto)

1. Cria projeto em [supabase.com](https://supabase.com).
2. No **SQL Editor**, executa por ordem (ou de uma vez): [`supabase/sql/leads.sql`](../supabase/sql/leads.sql), [`supabase/sql/mari_conversation_turns.sql`](../supabase/sql/mari_conversation_turns.sql), [`supabase/sql/mari_lead_media.sql`](../supabase/sql/mari_lead_media.sql), e — para cadastro estruturado de imóveis — [`supabase/sql/mari_imoveis.sql`](../supabase/sql/mari_imoveis.sql).
3. Em **Project Settings → API** copia:
   - **Project URL** → usar como `SUPABASE_URL` na Render (e localmente no `.env`).
4. Em **Project Settings → API** copia **`service_role`** (secret):
   - usar como `SUPABASE_SERVICE_ROLE_KEY` na Render (**nunca** no frontend / browser).

**Por que service_role e não `anon`?**  
O backend da Mari insere leads com privilégios de servidor. A chave `anon` fica sujeita a RLS e não deve ter permissão ampla para inserts de CRM. Mantém `service_role` só na Render (e no `.env` local de desenvolvimento, fora do Git).

---

## Variáveis na Render (Environment)

No serviço **Web** da Mari, define pelo menos:

| Variável | Origem |
|----------|--------|
| `MISTRAL_API_KEY` | Mistral AI |
| `SUPABASE_URL` | Supabase → Settings → API → Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase → Settings → API → service_role |

Opcional:

| Variável | Função |
|----------|--------|
| `MARIA_MODEL` | Ex.: `mistral:mistral-small-latest` |
| `DATABASE_URL` | URI Postgres do Supabase (Session pooler ou direct) para **sessões Agno** em Postgres em vez de SQLite — ver secção abaixo |

Depois de guardar, faz **Manual Deploy** ou espera o redeploy automático.

---

## Comportamento do código

- Se **`SUPABASE_URL`** e **`SUPABASE_SERVICE_ROLE_KEY`** estiverem definidos, cada **`registrar_lead_rascunho`** também faz **insert** na tabela **`leads`**.
- Continua a existir fallback **`data/leads.jsonl`** local (útil em dev ou se Supabase falhar parcialmente — ver resposta da tool).

### Sessões do agente (memória Agno) em Supabase

Por defeito a Mari usa **SQLite** (`agno.db`). Em containers Render o disco é efémero.

Para persistir **sessões Agno** em Postgres Supabase, define **`DATABASE_URL`** com a connection string do projeto (Supabase → **Database** → *Connection string* URI, modo **Session** ou **Transaction**, com password). No código, quando `DATABASE_URL` existe, o agente usa **`PostgresDb`**.

Requer dependências Postgres no ambiente (o pacote `agno` com drivers via `sqlalchemy` / `psycopg` — já coberto ao instalar extras se necessário).

---

## Ligação ao Agent OS

Com o deploy Live na Render, no [os.agno.com](https://os.agno.com) usa **Live** e o URL `https://<teu-serviço>.onrender.com` (sem barra final).

---

Ver também: [`render-deploy.md`](./render-deploy.md), [`agno-agentos-connection.md`](./agno-agentos-connection.md).
