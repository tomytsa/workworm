"""Put scheduled interviews on the user's LifePrime calendar through its MCP server (via `claude -p`)."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import llm
import radar

MCP_CONFIG = radar.ROOT / "data" / "lifeprime_mcp.json"
SCHEMA = {
    "type": "object",
    "properties": {
        "created": {"type": "boolean"},
        "already_existed": {"type": "boolean"},
        "event_id": {"type": "string"},
        "error": {"type": "string", "description": "Vacío si salió bien; si no, el motivo en español"},
    },
    "required": ["created", "already_existed", "event_id", "error"],
}


def settings(cfg: dict) -> dict:
    return {"enabled": False, "duration_minutes": 60, "color": "#ffb000",
            "url": "", **cfg.get("lifeprime", {})}


def add_interview(cfg: dict, job: dict, interview_at: str, link: str, summary: str) -> dict:
    """Create (once) a LifePrime event for the interview. Returns SCHEMA-shaped dict; never raises."""
    s = settings(cfg)
    if not s["enabled"] or not s["url"]:
        return {"created": False, "already_existed": False, "event_id": "",
                "error": "LifePrime desactivado o sin URL en config.json → lifeprime"}
    start = radar.parse_time(interview_at)
    if not start:
        return {"created": False, "already_existed": False, "event_id": "", "error": f"Fecha inválida: {interview_at}"}
    start = start.astimezone(radar.LOCAL_TZ)
    end = start + timedelta(minutes=int(s["duration_minutes"]))
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"radar-laboral/{job['id']}/{start.isoformat()}"))
    MCP_CONFIG.parent.mkdir(exist_ok=True)
    MCP_CONFIG.write_text(json.dumps({"mcpServers": {"lifeprime": {"type": "http", "url": s["url"]}}}), encoding="utf-8")
    title = f"Entrevista – {job['company']} ({job['title']})"[:200]
    description = (f"{job['title']} en {job['company']}\nLink de la reunión: {link or '(todavía no enviado)'}\n"
                   f"Oferta: {job['url']}\n\n{summary}\n\nAgendada por WorkWorm.")[:3900]
    prompt = (
        "Cargá esta entrevista en el calendario de LifePrime.\n"
        f"1. Primero listá los eventos entre {(start - timedelta(hours=1)).isoformat()} y {(end + timedelta(hours=1)).isoformat()}. "
        f"Si ya existe uno con el título «{title}», no crees otro: already_existed=true y devolvé su id.\n"
        "2. Si no existe, creá el evento con EXACTAMENTE estos valores:\n"
        f"   title: {title}\n   start_at: {start.isoformat()}\n   end_at: {end.isoformat()}\n"
        f"   color: {s['color']}\n   idempotency_key: {key}\n   confirmation: I confirm this change\n"
        f"   description: {description}\n"
        "No modifiques ni borres ningún otro evento. Si el servidor pide iniciar sesión o falla, devolvé el error.")
    try:
        return llm.ask("Sos un asistente que carga eventos en un calendario. Seguí las instrucciones al pie de la letra.",
                       prompt, SCHEMA, model=cfg["autopilot"].get("model", "sonnet"), timeout=240,
                       extra=["--mcp-config", str(MCP_CONFIG), "--strict-mcp-config", "--allowedTools",
                              "mcp__lifeprime__list_calendar_events", "mcp__lifeprime__create_calendar_event"])
    except llm.LLMError as exc:
        return {"created": False, "already_existed": False, "event_id": "", "error": str(exc)}
