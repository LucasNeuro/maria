"""Lista memórias Agno para um user_id (mesma BD que run.py). Uso:
    python scripts/list_user_memories.py
    python scripts/list_user_memories.py lucas.marcondes@clicvendy.com.br
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))


def main() -> None:
    uid = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("LIST_MEMORY_USER_ID", "")).strip()
    if not uid:
        print("Passa o user_id como argumento ou define LIST_MEMORY_USER_ID no .env.")
        print('Ex.: python scripts/list_user_memories.py "lucas.marcondes@clicvendy.com.br"')
        sys.exit(1)

    from maria_os import maria

    maria.initialize_agent()
    try:
        mems = maria.get_user_memories(user_id=uid)
    except Exception as e:
        print("Erro ao ler memórias:", e)
        sys.exit(2)

    print(f"user_id={uid!r} — {len(mems) if mems else 0} memória(s)")
    if mems:
        for m in mems:
            print("-", getattr(m, "memory", m))


if __name__ == "__main__":
    main()
