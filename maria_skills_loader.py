"""Carrega skills Mari (SKILL.md em ``skills/<id>/``) — índice compacto + corpo completo por pedido."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent
SKILLS_DIR = ROOT / "skills"

_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _skill_dirs() -> list[Path]:
    if not SKILLS_DIR.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(SKILLS_DIR.iterdir()):
        if p.is_dir() and (p / "SKILL.md").is_file():
            out.append(p)
    return out


def _parse_skill_md(raw: str) -> tuple[dict[str, Any], str]:
    raw = (raw or "").lstrip("\ufeff")
    if not raw.startswith("---"):
        return {}, raw.strip()
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw.strip()
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, parts[2].strip()


def skill_index_entries() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for d in _skill_dirs():
        sid = d.name
        raw = (d / "SKILL.md").read_text(encoding="utf-8")
        meta, _ = _parse_skill_md(raw)
        desc = str(meta.get("description") or meta.get("summary") or "").strip()
        title = str(meta.get("name") or sid).strip()
        entries.append({"id": sid, "title": title, "description": desc})
    return entries


def skill_index_markdown() -> str:
    lines = [
        "## Skills Mari (instruções sob demanda)",
        "Para não inflar o prompt, os detalhes de cada domínio estão em ficheiros skill. "
        "Usa a ferramenta **`carregar_skill_maria`** com o `skill_id` correspondente quando precisares de procedimentos longos.",
        "",
    ]
    for e in skill_index_entries():
        blurb = e["description"] or "(sem descrição)"
        lines.append(f"- **`{e['id']}`** — {blurb}")
    if len(lines) <= 4:
        lines.append("- *(nenhuma pasta `skills/` encontrada)*")
    return "\n".join(lines)


def load_skill_body(skill_id: str) -> tuple[dict[str, Any] | None, str | None]:
    if not _SKILL_ID_RE.match(skill_id):
        return None, None
    path = SKILLS_DIR / skill_id / "SKILL.md"
    if not path.is_file():
        return None, None
    raw = path.read_text(encoding="utf-8")
    meta, body = _parse_skill_md(raw)
    return meta, body


def carregar_skill_maria(skill_id: str = "") -> str:
    """
    Lista todas as skills disponíveis (``skill_id`` vazio) ou devolve o **conteúdo Markdown completo**
    da skill pedida (ex.: ``midias-leads``, ``cep-endereco``). Usa antes de fluxos longos (anexos, CEP).
    """
    sid = (skill_id or "").strip().lower()
    if not sid:
        idx = skill_index_entries()
        return json.dumps({"skills": idx}, ensure_ascii=False, indent=2)
    meta, body = load_skill_body(sid)
    if body is None:
        return json.dumps(
            {"erro": "skill não encontrada", "skill_id": sid, "disponiveis": [e["id"] for e in skill_index_entries()]},
            ensure_ascii=False,
        )
    title = (meta or {}).get("name") or sid
    out = f"# Skill: {title}\n\n{body}"
    return out[:120_000] + ("\n\n…(truncado)" if len(out) > 120_000 else "")
