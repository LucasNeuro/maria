# Ligação da Mari ao AgentOS / painel Agno

A Mari **já corre como AgentOS** (`maria_os.py` expõe `app` FastAPI). “Conectar ao Agent OS” significa o **painel em [os.agno.com](https://os.agno.com)** (ou outro cliente) conseguir falar com esse endpoint HTTP.

---

## Testar já no painel (modal “Connect your AgentOS”)

Faz **nesta ordem**:

1. **Abre um terminal** na pasta do projeto e **deixa o servidor a correr** (enquanto testas não feches o terminal):
   ```powershell
   cd C:\Users\anima\OneDrive\Desktop\maria
   python run.py
   ```
   Deves ver algo como `Uvicorn running on http://127.0.0.1:8000`.

2. **Num browser**, abre [https://os.agno.com](https://os.agno.com), inicia sessão e escolhe ligar o OS (**Connect your AgentOS** / **Add new OS**, conforme o menu).

3. **Preenche o modal** como no teu ecrã:
   - **ENVIRONMENT:** **Local** (AgentOS na tua máquina).
   - **ENDPOINT URL:** `http://` + **`localhost:8000`** (equivale ao que o `run.py` usa na porta 8000).
   - **NAME:** `Maria` ou `Mari` — é só o nome **no painel**; o agente na API continua com id **`mari`**.
   - **TOKEN-BASED AUTHORIZATION (JWT):** pode ficar **OFF** para o primeiro teste (isto é JWT no OS; não é o `MISTRAL_API_KEY`).

4. Clica **CONNECT**. Se pedir confirmação de ambiente (“não podes editar depois”), confirma.

5. **Depois de ligado**, no painel abre o **chat / agentes** do OS que criaste e escolhe o agente **Mari** para mandar mensagens de teste (ex.: “Olá, quero comprar um apartamento”).

**Para validar só a API (sem painel):** com o servidor a correr, abre [http://localhost:8000/docs](http://localhost:8000/docs) → `POST /agents/mari/runs` → **Try it out** → `message` = texto de teste → **Execute**.

---

## 1. Modo local (sem Docker)

1. Copia `.env.example` → `.env` e define pelo menos `MISTRAL_API_KEY`.
2. Arranca o servidor:
   ```powershell
   python run.py
   ```
3. Confirma: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).
4. No painel Agno: **Connect OS** → ambiente **Local** → endpoint **`http://127.0.0.1:8000`** ou **`http://localhost:8000`**.

**Agente na API:** `POST /agents/mari/runs` (campo form `message`).

### Autenticação (`OS_SECURITY_KEY`)

Se definires `OS_SECURITY_KEY` no `.env`:

- Todas as rotas protegidas pedem header **`Authorization: Bearer <valor>`**.
- No Swagger: **Authorize** → HTTP Bearer.
- Ao registares o OS no **os.agno.com**, o painel tem de usar **o mesmo token** (ver documentação atual “Connect Your AgentOS / Security”).

Se estiveres só a testar, **remove** `OS_SECURITY_KEY` do `.env`.

---

## 2. Docker (mesmo projeto — recomendado para URL estável na LAN)

Na pasta do projeto:

```powershell
docker compose up --build
```

- API: `http://localhost:8000/docs`
- O compose monta `./specs` em modo leitura; `data/leads.jsonl` persiste no volume `maria-data`. O ficheiro `agno.db` (sessões) fica dentro do container até `docker compose down -v` — para persistir sessões entre rebuilds, avança depois para PostgreSQL (Supabase/Neon) ou monta um volume só para a BD (ajuste em código).

Variáveis: usa `env_file: .env` (cria `.env` ao lado do `docker-compose.yml`).

Para o painel na cloud ver uma máquina local, normalmente precisas de **túnel HTTPS** (ngrok, Cloudflare Tunnel, etc.) e registas essa URL em vez de `localhost`.

### Deploy na Render (URL Live para o Agent OS)

Guia passo a passo (Blueprint `render.yaml`, env vars, modo **Live** no os.agno.com): [**render-deploy.md**](./render-deploy.md).

**Supabase** (leads na BD + chaves na Render): [**supabase-setup.md**](./supabase-setup.md).

---

## 3. Agno Infra CLI (`ag infra`)

O SDK **Agno Infra** serve para **definir e subir** infra (Docker/AWS/K8s) como código:

```bash
ag infra create    # novo projeto a partir de template
ag infra up        # sobe recursos (ex.: AgentOS + PostgreSQL em Docker local)
```

Isto é **outro fluxo**: um projeto gerado pelo CLI/template. Para **este repositório (Maria)**:

- Ou manténs **este código** e ligas o endpoint como na secção 1 ou 2;
- Ou crias um projeto com `ag infra create`, escolhes o template oficial e **copias** `maria_os.py`, `tools_maria.py`, `specs/`, etc. para dentro da estrutura gerada, ajustando o `Dockerfile` / entrypoint do template para importar a tua `app`.

Documentação: [Agno Infra overview](https://docs.agno.com/deploy/infra/overview.md), template Docker clássico: [agentos-docker-template](https://github.com/agno-agi/agentos-docker-template).

---

## 4. Checklist rápido quando “não liga”

| Sintoma | O que verificar |
|--------|------------------|
| 401 nas chamadas | `OS_SECURITY_KEY` — Bearer correto ou remove a variável. |
| Painel cloud não alcança `localhost` | Usar URL pública (deploy ou túnel). |
| Agente não aparece no chat | ID do agente é **`mari`** — OS tem de expor esse Agent na lista `/config`. |

---

Referência POP e ferramentas: [`implementation-guide.md`](./implementation-guide.md), [`maria.md`](./maria.md).
