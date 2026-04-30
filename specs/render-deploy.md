# Deploy da Mari na Render + testes no Agent OS

Objetivo: ter uma URL **HTTPS pública** (ex. `https://mari-agentos.onrender.com`) para usar no painel Agno em modo **Live**, sem depender de `localhost`.

---

## Pré-requisitos

- Conta [Render](https://render.com) e **repositório Git** (ex. **GitHub**) com este projeto — a Render faz pull do código a partir do Git.
- Chave **Mistral** (`MISTRAL_API_KEY`) e, se usares Supabase para leads, **`SUPABASE_URL`** + **`SUPABASE_SERVICE_ROLE_KEY`** (ver [**supabase-setup.md**](./supabase-setup.md)).

No menu **+ New** da Render usa **Blueprint** (com `render.yaml`) ou **Web Service** com Docker. **Não** precisas de criar **Postgres** na Render se a base for **Supabase**.

---

## Opção A — Blueprint (`render.yaml`)

1. Faz **push** deste repo com `render.yaml` e `Dockerfile` na raiz.

   **Se aparecer** “Blueprint file was found, but there was an issue”: voltaste a fazer **push** do `render.yaml` corrigido e **Retry** no wizard. Causa frequente em YAML: valores com **`:`** (ex. `mistral:modelo`) **sem aspas** quebram o ficheiro — no nosso blueprint o `MARIA_MODEL` já vai entre aspas.

2. Na Render: **New +** → **Blueprint**.
3. Liga o repositório e aplica o blueprint.
4. No serviço **mari-agentos** → **Environment**:
   - Adiciona **`MISTRAL_API_KEY`** (valor secreto).
   - Opcional: **`MARIA_MODEL`** (se quiseres outro modelo Mistral).
   - Opcional: **`OS_SECURITY_KEY`** — só se quiseres Bearer nas APIs; nesse caso tens de configurar o mesmo token ao ligar o OS no Agno (ver doc Agno).
5. **Deploy**. Espera ficar **Live**.
6. Copia a URL pública (ex. `https://mari-agentos.onrender.com`).

---

## Opção B — Web Service manual (Docker)

1. **New +** → **Web Service** → escolhe o repo.
2. **Runtime:** Docker (usa o `Dockerfile` da raiz).
3. **Instance type:** Free ou pago (no plano gratuito o serviço **hiberna** após inatividade; o primeiro pedido após dormir pode demorar ~1 min).
4. **Health check path:** `/health`
5. **Environment variables:**
   - `MISTRAL_API_KEY` = *(secret)*
   - `MARIA_MODEL` = `mistral:mistral-small-latest` *(opcional)*
6. **Create Web Service**.

---

## Ligar ao Agent OS (painel Agno)

1. Abre [os.agno.com](https://os.agno.com) → **Connect your AgentOS** / **Add new OS**.
2. **ENVIRONMENT:** escolhe **Live** (se o teu plano Agno pedir PRO para Live, segue o que a UI indicar).
3. **ENDPOINT URL:** `https://<o-teu-servico>.onrender.com` — **sem** barra final.
4. **TOKEN-BASED AUTHORIZATION:** mantém **OFF** a menos que tenhas configurado JWT na Agno doc + AgentOS.
5. Se usares **`OS_SECURITY_KEY`** na Render, o painel/consumidor tem de enviar **`Authorization: Bearer <valor>`** em cada chamada (consulta a doc Agno para onde meter isso no connect).

Validação rápida no browser: `https://<teu-dominio>/docs` e `https://<teu-dominio>/health` devem responder **200**.

---

## Notas importantes

| Tema | Detalhe |
|------|--------|
| **PORT** | O `Dockerfile` já usa a variável **`PORT`** que a Render define; não fixes só 8000 em produção. |
| **SQLite / disco** | O filesystem na Render é **efémero**: ao redeploy, `agno.db` pode ser recriado (sessões perdidas). Para sessões estáveis, migra depois para **Postgres** (Render Postgres + `PostgresDb` no Agno). |
| **Leads `data/`** | Idem — considera Supabase para persistência real. |
| **POP `specs/`** | Está **dentro da imagem Docker**; mudanças em `maria.md` exigem **novo deploy** (git push → auto-deploy). |

---

## Referências no repo

- Ligação local + troubleshooting: [`agno-agentos-connection.md`](./agno-agentos-connection.md)
- Implementação geral: [`implementation-guide.md`](./implementation-guide.md)
