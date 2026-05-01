"""Um turno rápido contra a Mari (útil para validar modelo + memória). Uso: python scripts/smoke_chat.py"""

from __future__ import annotations

import asyncio
import os
import sys

# project root no path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))


async def main() -> None:
    from maria_os import maria

    uid = os.getenv("SMOKE_USER_ID", "smoke-test-local")
    sid = os.getenv("SMOKE_SESSION_ID", "smoke-session-1")
    msg = os.getenv(
        "SMOKE_MESSAGE",
        "Olá, sou a Ana e quero alugar um apartamento na Zona Sul do Rio. Prefiro falar por WhatsApp.",
    )

    maria.initialize_agent()
    out = await maria.arun(input=msg, session_id=sid, user_id=uid, stream=False)
    text = (getattr(out, "content", None) or str(out)).strip()
    print("--- Resposta ---")
    print(text)
    try:
        mems = maria.get_user_memories(user_id=uid)
        print("\n--- Memórias deste user_id ---")
        print(mems if mems else "(nenhuma ainda — pode levar um run extra ou mais conteúdo memorizável)")
    except Exception as e:
        print("\n(get_user_memories falhou:", e, ")")


if __name__ == "__main__":
    asyncio.run(main())
