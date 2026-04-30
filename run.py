"""Inicia o AgentOS da Mari. Uso: python run.py"""

import uvicorn

if __name__ == "__main__":
    print(
        "\n  Mari AgentOS\n"
        "  - Swagger UI:  http://127.0.0.1:8000/docs\n"
        "  - Atalho:      http://127.0.0.1:8000/swagger\n"
        "  - Chat API:    POST /agents/mari/runs (multipart: message=...)\n"
        "  Se usar OS_SECURITY_KEY no .env, no Swagger clique em Authorize e Bearer <chave>.\n"
    )
    uvicorn.run(
        "maria_os:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
