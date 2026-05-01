"""API HTTP para o painel React: leads (Supabase) e chat JSON com a Mari."""

from __future__ import annotations

import os
from typing import Annotated

from agno.agent import Agent
from fastapi import APIRouter, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


def maybe_add_cors(app: FastAPI) -> None:
    """Se MARIA_CORS_ORIGINS estiver definido (URLs separadas por vírgula), habilita CORS para o React."""
    raw = (os.getenv("MARIA_CORS_ORIGINS") or "").strip()
    if not raw:
        return
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if not origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )


def _admin_key_configured() -> str | None:
    k = (os.getenv("MARIA_ADMIN_API_KEY") or "").strip()
    return k or None


def _extract_admin_token(
    authorization: str | None,
    x_maria_admin_key: str | None,
) -> str:
    if x_maria_admin_key and x_maria_admin_key.strip():
        return x_maria_admin_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _require_admin(
    authorization: str | None,
    x_maria_admin_key: str | None,
) -> None:
    expected = _admin_key_configured()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="MARIA_ADMIN_API_KEY não configurado no servidor.",
        )
    got = _extract_admin_token(authorization, x_maria_admin_key)
    if got != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _supabase_leads_client():
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise HTTPException(
            status_code=503,
            detail="SUPABASE_URL ou SUPABASE_SERVICE_ROLE_KEY ausentes.",
        )
    from supabase import create_client

    return create_client(url, key)


class ChatRequest(BaseModel):
    """Corpo JSON para o React falar com a Mari (sem multipart do AgentOS)."""

    message: str = Field(..., min_length=1, max_length=16000)
    session_id: str | None = Field(
        default=None,
        description="Opcional. Mesmo valor = mesma conversa (memória Agno). Ex.: painel:user@id",
    )
    user_id: str | None = Field(default=None, description="Opcional. Identificador do utilizador no painel.")


def register_maria_admin_routes(app: FastAPI, agent: Agent) -> None:
    r = APIRouter(prefix="/api/maria", tags=["Maria — admin"])

    @r.get("/leads")
    async def list_leads(
        authorization: Annotated[str | None, Header()] = None,
        x_maria_admin_key: Annotated[str | None, Header(alias="X-Maria-Admin-Key")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict:
        """
        Lista os últimos leads em `public.leads` (ordem `created_at` desc).

        Auth: `X-Maria-Admin-Key` ou `Authorization: Bearer <MARIA_ADMIN_API_KEY>`.
        """
        _require_admin(authorization, x_maria_admin_key)
        client = _supabase_leads_client()
        resp = (
            client.table("leads")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = resp.data or []
        return {"items": rows, "count": len(rows)}

    @r.post("/chat")
    async def chat_with_mari(
        body: ChatRequest,
        authorization: Annotated[str | None, Header()] = None,
        x_maria_admin_key: Annotated[str | None, Header(alias="X-Maria-Admin-Key")] = None,
    ) -> dict:
        """
        Executa um turno da Mari com JSON simples (ideal para React noutro repositório).

        Auth: igual a `/api/maria/leads`. Não expõe a service role do Supabase no browser.
        """
        _require_admin(authorization, x_maria_admin_key)
        session_id = (body.session_id or "").strip() or "admin:default"
        user_id = (body.user_id or "").strip() or "admin-ui"
        run_out = await agent.arun(
            input=body.message.strip(),
            session_id=session_id,
            user_id=user_id,
            stream=False,
        )
        reply = (getattr(run_out, "content", None) or "").strip()
        return {
            "reply": reply,
            "session_id": session_id,
            "user_id": user_id,
        }

    app.include_router(r)
