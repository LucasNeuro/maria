"""Inicia o AgentOS da Mari. Uso: python run.py"""

import uvicorn

from maria_logging import UVICORN_RICH_LOG_CONFIG, quiet_noisy_loggers

if __name__ == "__main__":
    quiet_noisy_loggers()
    print(
        "\n  Mari AgentOS\n"
        "  - Swagger UI:  http://127.0.0.1:8000/docs\n"
        "  - Atalho:      http://127.0.0.1:8000/swagger\n"
        "  - Chat API:    POST /agents/mari/runs (multipart: message=...)\n"
        "  - WhatsApp:    POST /webhooks/uazapi (inalterado enquanto este processo corre)\n"
        "  Agno Studio (os.agno.com): liga OS em Local → http://127.0.0.1:8000 — depois REFRESH no Studio.\n"
        "  Guia: specs/agno-agentos-connection.md\n"
        "  Se usar OS_SECURITY_KEY no .env, no Swagger clique em Authorize e Bearer <chave>.\n"
    )
    uvicorn.run(
        "maria_os:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_config=UVICORN_RICH_LOG_CONFIG,
        access_log=True,
    )
