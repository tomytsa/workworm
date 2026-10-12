"""Autopilot: apply by email, follow conversations in Gmail, reply and schedule interviews."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path

import llm
import radar
from radar import LOCAL_TZ, connect, log_event, normalize, now

LOCK = radar.ROOT / "data" / "agent.lock"
PROGRESS = radar.ROOT / "data" / "progress.json"
CV_CACHE = radar.ROOT / "data" / "cv_cache.json"
FREE_DOMAINS = {"gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.ar", "live.com",
                "icloud.com", "protonmail.com", "hotmail.com.ar", "outlook.es"}
ACTIVE = ("applied", "conversation", "action_needed", "interview", "offer")

APPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "should_apply": {"type": "boolean"},
        "reason": {"type": "string", "description": "Una oración en español explicando la decisión"},
        "recipient": {"type": "string", "description": "Correo para enviar el CV tal como aparece en el aviso; vacío si se postula por formulario"},
        "language": {"type": "string", "enum": ["es", "en"]},
        "cv_id": {"type": "string"},
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["should_apply", "reason", "recipient", "language", "cv_id", "subject", "body"],
}
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["auto_ack", "rejection", "interview_proposed", "availability_request",
                                             "interview_confirmed", "question", "offer", "not_related", "other"]},
        "summary": {"type": "string", "description": "Resumen en español de una oración del mensaje recibido"},
        "needs_human": {"type": "boolean"},
        "needs_human_reason": {"type": "string"},
        "should_reply": {"type": "boolean"},
        "reply_body": {"type": "string", "description": "Texto completo de la respuesta, con firma; vacío si no se responde"},
        "interview_at": {"type": "string", "description": "Fecha y hora de la entrevista en ISO 8601 con zona (-03:00 si no indican otra); vacío si no hay"},
        "interview_link": {"type": "string"},
    },
    "required": ["kind", "summary", "needs_human", "needs_human_reason", "should_reply", "reply_body",
                 "interview_at", "interview_link"],
}


# ---------- context ----------

def profile(cfg: dict) -> str:
    path = radar.ROOT / cfg.get("profile_path", "profile.md")
    return path.read_text(encoding="utf-8") if path.exists() else ""


def cv_texts(cfg: dict) -> dict[str, str]:
    """Text of each configured CV, cached by path and modification time."""
    import pymupdf

    cache = json.loads(CV_CACHE.read_text(encoding="utf-8")) if CV_CACHE.exists() else {}
    texts, changed = {}, False
    for cv in cfg["cvs"]:
        path = Path(cv["path"])
        if not path.is_file():
            texts[cv["id"]] = ""  # missing file: reported by missing_cvs(), never crashes a cycle
            continue
        key = f"{path}|{path.stat().st_mtime}"
        if key not in cache:
            with pymupdf.open(path) as doc:
                cache[key] = "".join(page.get_text() for page in doc)
            changed = True
        texts[cv["id"]] = cache[key]
    if changed:
        CV_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return texts


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def within(hours: list[int]) -> bool:
    start, end = hours
    return start <= local_now().hour < end


def system_prompt(cfg: dict) -> str:
    return (f"Sos el agente de búsqueda laboral de {cfg['candidate_name']}. Escribís correos en su nombre, en primera persona, "
            "como si fueras esa persona. Hoy es " + local_now().strftime("%A %d/%m/%Y %H:%M") + f" (hora local, {LOCAL_TZ}). "
            "Usá solo datos del perfil y de los CV; nunca inventes experiencia, cifras ni disponibilidad. "
            "Si un dato del perfil dice COMPLETAR y hace falta para responder, no lo inventes: marcá needs_human. "
            "Firmá los correos con nombre, y si están en el perfil, teléfono y LinkedIn. Sin markdown: texto plano de correo.\n\n"
            "PERFIL Y PREFERENCIAS:\n" + profile(cfg))


# ---------- gmail helpers ----------

def build_raw(cfg: dict, to: str, subject: str, body: str, attachment: Path | None = None,
              reply_to_id: str = "", references: str = "") -> str:
    email = EmailMessage()
    email["To"] = to
    email["From"] = formataddr((cfg["candidate_name"], cfg["candidate_email"]))
    email["Subject"] = subject
    if reply_to_id:
        email["In-Reply-To"] = reply_to_id
        email["References"] = (references + " " + reply_to_id).strip()
    email.set_content(body.strip())
    if attachment:
        email.add_attachment(attachment.read_bytes(), maintype="application", subtype="pdf", filename=attachment.name)
    return base64.urlsafe_b64encode(email.as_bytes()).decode("ascii")


def send(service, raw: str, thread_id: str = "") -> dict:
    body = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    return service.users().messages().send(userId="me", body=body).execute()


def header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def plain_text(payload: dict) -> str:
    """Best-effort plain text of a Gmail API message payload."""
    def walk(part):
        yield part
        for child in part.get("parts", []) or []:
            yield from walk(child)
    plain, html = "", ""
    for part in walk(payload):
        data = part.get("body", {}).get("data")
        if not data:
            continue
        text = base64.urlsafe_b64decode(data).decode("utf-8", "replace")
        if part.get("mimeType") == "text/plain":
            plain += text
        elif part.get("mimeType") == "text/html":
            html += text
    if not plain and html:
        plain = radar.BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    # Drop quoted history so the model sees only the new content.
    plain = re.split(r"\n(?:El .{5,80} escribió:|On .{5,80} wrote:)\s*\n", plain)[0]
    return "\n".join(line for line in plain.splitlines() if not line.startswith(">")).strip()


def notify(service, cfg: dict, subject: str, body: str) -> None:
    """Radar notices go to the notifications inbox, sent from that same account when it is connected."""
    to = cfg["autopilot"].get("notify_email")
    try:
        alerts = radar.gmail_service(role="alerts")
    except Exception:
        alerts = None
    if alerts and to:
        sender = radar.gmail_account("alerts")
        alerts.users().messages().send(userId="me", body={"raw": build_raw({**cfg, "candidate_email": sender}, to,
                                       f"[WorkWorm] {subject}", body)}).execute()
        return
    if to:
        send(service, build_raw(cfg, to, f"[WorkWorm] {subject}", body))


def mail_link(thread_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}" if thread_id else ""


# ---------- applying ----------

def applications_today(db) -> int:
    midnight = local_now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return db.execute("SELECT COUNT(*) n FROM applications WHERE sent_at >= ?",
                      (midnight.isoformat(timespec="seconds"),)).fetchone()["n"]


ES_WORDS = (" de ", " la ", " que ", " el ", " los ", " para ", " con ", " experiencia ", " conocimientos ", " buscamos ")
EN_WORDS = (" the ", " and ", " to ", " of ", " with ", " you ", " experience ", " our ", " will ", " we ")


def detect_language(text: str) -> str:
    """'es' or 'en' from the posting text; '' when it is too short or mixed to tell."""
    body = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    es = sum(body.count(w) for w in ES_WORDS)
    en = sum(body.count(w) for w in EN_WORDS)
    if es + en < 8:
        return ""
    return "es" if es > en * 1.5 else "en" if en > es * 1.5 else ""


def application_language(job: dict, cfg: dict, llm_guess: str) -> str:
    """Language for CV, cover letter and email, per config: 'auto' follows the posting."""
    mode = cfg.get("language", {}).get("mode", "auto")
    if mode in ("es", "en"):
        return mode
    found = detect_language(f"{job.get('title', '')} {job.get('description', '')}")
    return found or (llm_guess if llm_guess in ("es", "en") else cfg.get("language", {}).get("fallback", "es"))


def missing_cvs(cfg: dict) -> list[str]:
    """CV files (PDF or Word) from config that are not on disk, e.g. after renaming them."""
    return [p for cv in cfg["cvs"] for p in (cv["path"], cv.get("docx", "")) if p and not Path(p).is_file()]


def cv_for(cfg: dict, language: str) -> dict:
    """CV for the language; falls back to any CV whose PDF exists if that one is missing."""
    usable = [c for c in cfg["cvs"] if Path(c["path"]).is_file()]
    if not usable:
        raise FileNotFoundError("No se encontró ningún CV configurado: revisá las rutas en config.json → cvs")
    return next((c for c in usable if c["language"] == language), usable[0])


def decide(job: dict, cfg: dict, hint: str) -> dict:
    """Ask Claude whether the offer fits; returns recipient (if any), CV choice and the text to send."""
    cvs = cv_texts(cfg)
    cv_menu = "\n".join(f"- id={cv['id']} idioma={cv['language']} foco={cv['focus']}" for cv in cfg["cvs"])
    lang = application_language(job, cfg, "") if cfg.get("language", {}).get("mode", "auto") != "auto" \
        or detect_language(job.get("description", "")) else ""
    language_rule = (f"Escribí todo en {'inglés' if lang == 'en' else 'español'} (language={lang}) y usá el CV de ese idioma. "
                     if lang else "Escribí en el idioma del aviso (language: es o en) y usá el CV de ese idioma. ")
    prompt = (
        "Prepará la postulación a esta oferta y verificá si cae en alguna exclusión.\n"
        "should_apply=true SIEMPRE que el rol sea de datos (Data/Analytics Engineer, ETL, BI técnico) y no cumpla "
        "ninguna de estas exclusiones definidas por el candidato:\n" + exclusions(cfg)["rules"] + "\n"
        "IMPORTANTE: tu filtro son SOLO esas exclusiones. No evalúes si el candidato encaja, no compares sus años de "
        "experiencia con los pedidos (salvo la regla de años de arriba) ni descartes por tecnologías que no tenga. "
        "Si dudás, should_apply=true.\n"
        "recipient: un correo que el aviso indique para enviar CV o postulaciones (o el de un reclutador del aviso). "
        "Si no hay, dejalo vacío: se postulará por el formulario de la página.\n"
        + language_rule +
        "subject: '<Cargo> – <Nombre>' o el formato que pida el aviso. body: 4 a 7 oraciones, 2 o 3 coincidencias "
        "reales entre el aviso y el CV. Si hay recipient, es el correo (con firma) y aclara que el CV va adjunto; si no, "
        "es una carta de presentación breve para pegar en el formulario.\n\n"
        f"CV disponibles:\n{cv_menu}\n\n"
        + "\n\n".join(f"=== CV {k} ===\n{v}" for k, v in cvs.items())
        + f"\n\n=== OFERTA ===\nCargo: {job['title']}\nEmpresa: {job['company']}\nUbicación: {job['location']}\n"
          f"Fuente: {job['source']} ({job['url']})\nCorreo detectado: {hint or '(ninguno)'}\n\n"
          f"{(job.get('description') or '')[:8000]}")
    return llm.ask(system_prompt(cfg), prompt, APPLY_SCHEMA, model=cfg["autopilot"].get("model", "sonnet"))


def prepare_cv(job: dict, cv: dict, cfg: dict, decision: dict, as_email: bool) -> dict:
    """Tailor the CV and cover letter to the offer (see tailor.py). Falls back to the original PDF on failure."""
    import tailor
    if not tailor.settings(cfg)["enabled"] or not cv.get("docx"):
        return {**cv, "send_path": cv["path"], "added": []}
    try:
        result = tailor.build(job, cv, cfg, decision["language"], as_email)
    except Exception as exc:
        with connect() as db:
            log_event(db, job["id"], "tailor_error", f"No se pudo personalizar el CV ({exc}); se usa el original")
        return {**cv, "send_path": cv["path"], "added": []}
    if result["cover_letter"]:
        decision["body"] = result["cover_letter"]
    with connect() as db:
        log_event(db, job["id"], "tailored", "CV personalizado" + (f"; agregadas a habilidades: {', '.join(result['added'])}"
                                                                   if result["added"] else "")
                  + (f"; revertido: {' | '.join(result['reverted'])}" if result["reverted"] else ""))
    return {**cv, "send_path": str(result["pdf"]), "added": result["added"]}


def reserve(job: dict, recipient: str, decision: dict, cv: dict, channel: str) -> None:
    """Persist intent before sending so a crash never leads to a duplicate application."""
    with connect() as db:
        send_path = cv.get("send_path", cv["path"])
        db.execute("""INSERT INTO applications(job_id,recipient,subject,body,status,cv_file,cv_path,added_skills,
                      language,channel,updated_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET recipient=excluded.recipient,
                      subject=excluded.subject, body=excluded.body, status=excluded.status, cv_file=excluded.cv_file,
                      cv_path=excluded.cv_path, added_skills=excluded.added_skills,
                      language=excluded.language, channel=excluded.channel, updated_at=excluded.updated_at""",
                   (job["id"], recipient, decision["subject"], decision["body"], "sending",
                    Path(cv["path"]).name, send_path, ", ".join(cv.get("added", [])),
                    decision["language"], channel, now()))


def record_sent(job: dict, channel: str, decision: dict, gmail_id: str, thread_id: str, summary: str) -> None:
    with connect() as db:
        db.execute("""UPDATE applications SET status='sent', channel=?, message_id=?, thread_id=?, sent_at=?,
                      last_outbound_at=?, updated_at=? WHERE job_id=?""",
                   (channel, gmail_id, thread_id, now(), now(), now(), job["id"]))
        db.execute("""INSERT OR IGNORE INTO messages(gmail_id,job_id,thread_id,direction,sender,subject,body,kind,summary,at)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (gmail_id, job["id"], thread_id, "out", radar.config()["candidate_email"], decision["subject"],
                    decision["body"], "application", summary, now()))
        db.execute("UPDATE jobs SET status='applied', updated_at=? WHERE id=?", (now(), job["id"]))
        log_event(db, job["id"], "applied", summary)


def apply(service, job: dict, cfg: dict, recipient_hint: str = "", force: bool = False) -> str:
    """Decide with the LLM and apply by email or, when there is no email, through the web form.
    Returns 'applied', 'skipped', 'blocked', 'action_needed', 'failed', 'new' or 'login_required:<site>'."""
    if is_manual(job):
        return "manual"  # LinkedIn: never applied automatically.
    reason = excluded(job, cfg) if job["source"] != "link" else ""
    if reason:
        radar.set_status(job["id"], "skipped", reason)
        return "skipped"
    hint = recipient_hint or job.get("contact_email") or ""
    decision = decide(job, cfg, hint)
    force = force or job["source"] == "link"  # Links the user added are always applied to.
    if not decision["should_apply"] and not recipient_hint and not force:
        radar.set_status(job["id"], "skipped", decision["reason"])
        return "skipped"
    # Language is a config rule, not the model's choice: it fixes the CV, the letter and the email.
    decision["language"] = application_language(job, cfg, decision.get("language", ""))
    cv = cv_for(cfg, decision["language"])
    recipient = (decision["recipient"] or "").strip().lower()
    if recipient and not (radar.valid_recipient(recipient) and recipient in normalize(job.get("description", "") + " " + hint)):
        recipient = ""  # Never write to an address the posting does not show.
    if not recipient:
        return apply_form(service, job, cfg, decision, prepare_cv(job, cv, cfg, decision, as_email=False))
    with connect() as db:
        recent = db.execute("SELECT COUNT(*) n FROM applications WHERE recipient=? AND sent_at >= ?",
                            (recipient, (datetime.now(timezone.utc) - timedelta(days=14)).isoformat())).fetchone()["n"]
    if recent and not recipient_hint:
        return "new"  # Avoid two CVs to the same inbox within two weeks; retry later.
    cv = prepare_cv(job, cv, cfg, decision, as_email=True)
    reserve(job, recipient, decision, cv, "email")
    try:
        result = send(service, build_raw(cfg, recipient, decision["subject"], decision["body"], Path(cv["send_path"])))
    except Exception as exc:
        with connect() as db:
            db.execute("UPDATE applications SET status='unknown', updated_at=? WHERE job_id=?", (now(), job["id"]))
        radar.set_status(job["id"], "failed", f"Error al enviar ({exc}). Revisá Enviados antes de reintentar.")
        return "failed"
    record_sent(job, "email", decision, result["id"], result["threadId"],
                f"CV ({cv['id']}) enviado por correo a {recipient}")
    return "applied"


# ---------- applying through web forms (separate Edge driven by Claude) ----------

EDGE_PROFILE = radar.ROOT / "data" / "edge-profile"
PLAYWRIGHT_MCP = "@playwright/mcp@0.0.83"
FORM_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["applied", "already_applied", "needs_human", "login_required",
                                               "blocked", "closed", "not_fit"]},
        "reason": {"type": "string", "description": "Una o dos oraciones en español"},
        "site": {"type": "string", "description": "Dominio donde está el formulario (p. ej. linkedin.com, job-boards.greenhouse.io)"},
        "final_url": {"type": "string"},
        "login_url": {"type": "string", "description": "Si pide cuenta/sesión: URL exacta de la página de inicio de sesión "
                                                       "o registro que muestra el botón de Google (o el formulario de login)"},
        "login_google": {"type": "boolean", "description": "La página de login ofrece «Continuar/Iniciar sesión con Google»"},
        "answers": {"type": "array", "items": {"type": "object", "properties": {
            "question": {"type": "string"}, "answer": {"type": "string"}}, "required": ["question", "answer"]}},
    },
    "required": ["status", "reason", "site", "final_url", "login_url", "login_google", "answers"],
}


def upload_copy(cv: dict) -> Path:
    """ASCII-named copy of the CV for uploads (some forms choke on accents)."""
    import shutil
    import unicodedata
    src = Path(cv["path"])
    name = "".join(c for c in unicodedata.normalize("NFKD", src.name) if not unicodedata.combining(c))
    target = radar.ROOT / "data" / "cv" / cv["id"] / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.stat().st_mtime < src.stat().st_mtime:
        shutil.copy2(src, target)
    return target


def mcp_config(cfg: dict) -> Path:
    args = ["-y", PLAYWRIGHT_MCP, "--browser", "msedge", "--user-data-dir", str(EDGE_PROFILE),
            "--allow-unrestricted-file-access", "--output-dir", str(radar.ROOT / "data" / "pw-output"),
            "--viewport-size", "1280,900"]
    if cfg["autopilot"].get("browser_headless"):
        args.append("--headless")
    path = radar.ROOT / "data" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": args}}}), encoding="utf-8")
    return path


def close_agent_edge() -> None:
    """Kill any Edge left running on the agent's profile; the user's own Edge is untouched."""
    import subprocess
    script = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
              f"Where-Object {{ $_.CommandLine -like '*{radar.ROOT.name}*edge-profile*' }} | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


LOGIN_PAGES = ("https://secure.indeed.com/auth", "https://www.getonbrd.com/login",
               "https://candidato.ar.computrabajo.com/acceso/", "https://www.bumeran.com.ar/login")


def open_agent_edge(urls: tuple[str, ...] = LOGIN_PAGES) -> None:
    """Open the agent's Edge profile so the user can sign in once (Indeed, Get on Board...)."""
    import subprocess
    EDGE_PROFILE.mkdir(parents=True, exist_ok=True)
    edge = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
    subprocess.Popen([str(edge), f"--user-data-dir={EDGE_PROFILE}", "--no-first-run", *urls])


ASSIST_PORT = 9333
ASSIST_SCHEMA = {
    "type": "object",
    "properties": {"ready": {"type": "boolean"}, "detail": {"type": "string"}},
    "required": ["ready", "detail"],
}


def assist_form(job_id: int, cfg: dict) -> dict:
    """Fill the application form in a visible Edge and stop before submitting, leaving the window open so the
    user can solve the captcha and press submit. Returns {ready, detail}."""
    import subprocess
    import urllib.request
    job = radar.job(job_id)
    with connect() as db:
        app_row = db.execute("SELECT body, cv_path FROM applications WHERE job_id=?", (job_id,)).fetchone()
    letter = (app_row["body"] if app_row else "").split("\n\nRespuestas del formulario:")[0]
    cv_path = app_row["cv_path"] if app_row and app_row["cv_path"] and Path(app_row["cv_path"]).exists() else ""
    if not cv_path:
        cv = cv_for(cfg, application_language(job, cfg, ""))
        cv_path = str(upload_copy(cv))
    close_agent_edge()
    edge = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
    subprocess.Popen([str(edge), f"--user-data-dir={EDGE_PROFILE}", f"--remote-debugging-port={ASSIST_PORT}",
                      "--no-first-run", "about:blank"])
    for _ in range(30):  # wait for the debugging endpoint
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{ASSIST_PORT}/json/version", timeout=1)
            break
        except OSError:
            time.sleep(1)
    config = radar.ROOT / "data" / "mcp_assist.json"
    config.write_text(json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": [
        "-y", PLAYWRIGHT_MCP, "--cdp-endpoint", f"http://127.0.0.1:{ASSIST_PORT}", "--allow-unrestricted-file-access",
        "--output-dir", str(radar.ROOT / "data" / "pw-output")]}}}), encoding="utf-8")
    prompt = (
        f"Completá la postulación a esta oferta en nombre de {cfg['candidate_name']} usando el navegador, pero NO la envíes.\n"
        f"Oferta: {job['title']} — {job['company']}\nURL: {job['url']}\nCV para subir (ruta absoluta): {cv_path}\n"
        f"Carta de presentación:\n{letter}\n\n"
        f"Texto del CV:\n{cv_texts(cfg).get(cv_for(cfg, application_language(job, cfg, ''))['id'], '')}\n\n"
        "Abrí la URL en la pestaña actual, pulsá «Apply/Postularme», completá todos los campos y subí el CV. "
        "Dejá todo listo en el último paso con el botón de envío visible. NO pulses enviar ni resuelvas captchas: "
        "eso lo hace el candidato. No cierres la pestaña ni el navegador. ready=true si quedó listo para enviar.")
    try:
        return llm.ask(system_prompt(cfg), prompt, ASSIST_SCHEMA, model=cfg["autopilot"].get("model", "sonnet"),
                       timeout=int(cfg["autopilot"].get("form_timeout_minutes", 15)) * 60,
                       extra=["--mcp-config", str(config), "--strict-mcp-config", "--allowedTools", "mcp__playwright"])
    except llm.LLMError as exc:
        return {"ready": False, "detail": str(exc)}


SIGNUP_SCHEMA = {
    "type": "object",
    "properties": {"pages": {"type": "array", "items": {"type": "object", "properties": {
        "company": {"type": "string"}, "url": {"type": "string"}, "google": {"type": "boolean"},
        "detail": {"type": "string"}}, "required": ["company", "url", "google", "detail"]}}},
    "required": ["pages"],
}


def prepare_signup_pages(job_ids: list[int], cfg: dict) -> list[dict]:
    """Open each offer in its own tab of a visible agent Edge and walk it to the «create account» screen, without
    filling anything. The window stays open for the user; the reached URLs are stored as each job's login page."""
    import subprocess
    import urllib.request
    jobs = [radar.job(i) for i in job_ids]
    close_agent_edge()
    edge = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
    subprocess.Popen([str(edge), f"--user-data-dir={EDGE_PROFILE}", f"--remote-debugging-port={ASSIST_PORT}",
                      "--no-first-run", "about:blank"])
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{ASSIST_PORT}/json/version", timeout=1)
            break
        except OSError:
            time.sleep(1)
    config = radar.ROOT / "data" / "mcp_assist.json"
    config.write_text(json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": [
        "-y", PLAYWRIGHT_MCP, "--cdp-endpoint", f"http://127.0.0.1:{ASSIST_PORT}",
        "--output-dir", str(radar.ROOT / "data" / "pw-output")]}}}), encoding="utf-8")
    listing = "\n".join(f"- {j['company']}: {j.get('login_url') or j['url']}" for j in jobs)
    prompt = ("Para cada oferta, abrí una PESTAÑA NUEVA y llevala hasta la pantalla de CREAR CUENTA / REGISTRO del sitio "
              "de postulación (tocá «Postularme/Apply» y luego «Crear cuenta/Sign up/Registrarse» si hace falta). "
              "No completes ningún campo, no escribas nada y no envíes nada. Dejá cada pestaña abierta en esa pantalla. "
              "No cierres pestañas ni el navegador. Devolvé por cada una la URL final y si ofrece registrarse con Google.\n\n"
              + listing)
    try:
        result = llm.ask("Sos un asistente que navega páginas. Seguí las instrucciones al pie de la letra.", prompt,
                         SIGNUP_SCHEMA, model=cfg["autopilot"].get("model", "sonnet"), timeout=15 * 60,
                         extra=["--mcp-config", str(config), "--strict-mcp-config", "--allowedTools", "mcp__playwright"])
    except llm.LLMError as exc:
        return [{"company": "", "url": "", "google": False, "detail": str(exc)}]
    with connect() as db:
        for page in result["pages"]:
            job = next((j for j in jobs if normalize(j["company"]) == normalize(page["company"])), None)
            if job and page["url"].startswith("http"):
                db.execute("UPDATE applications SET login_url=?, login_google=? WHERE job_id=?",
                           (page["url"], int(page["google"]), job["id"]))
    return result["pages"]


def apply_form(service, job: dict, cfg: dict, decision: dict, cv: dict, dry_run: bool = False):
    """Apply through the posting's web form. With dry_run it fills everything, never submits, and returns the raw result."""
    upload = Path(cv["send_path"]) if cv.get("send_path") else upload_copy(cv)
    prompt = (
        f"Postulate a esta oferta en nombre de {cfg['candidate_name']} usando el navegador (herramientas de Playwright).\n\n"
        f"Oferta: {job['title']} — {job['company']}\nURL: {job['url']}\n"
        f"CV para subir (ruta absoluta): {upload}\n"
        + (f"\nDATOS QUE AGREGÓ EL CANDIDATO PARA ESTA OFERTA (usalos para completar el formulario):\n{job['user_input']}\n\n"
           if job.get("user_input") else "")
        + f"Texto del CV (para años de experiencia, empresas, fechas):\n{cv_texts(cfg).get(cv['id'], '')}\n\n"
        f"Carta de presentación (usala si el formulario la pide; adaptá el largo si hace falta):\n{decision['body']}\n\n"
        "Pasos:\n"
        "1. Abrí la URL y pulsá «Solicitar / Apply»; puede llevarte al sitio de la empresa (quizás en otra pestaña). "
        "Nunca te postules a través de LinkedIn: si el único camino es LinkedIn, devolvé blocked.\n"
        "2. Completá todos los campos con los datos del perfil y del CV. Subí el CV con la herramienta de carga de archivos. "
        "Preguntas de selección o de años de experiencia: respondé con la verdad según el CV. Pretensión salarial, "
        "disponibilidad y modalidad: según el perfil.\n"
        "3. Revisá el formulario y envialo UNA sola vez. Confirmá que aparezca un mensaje de envío exitoso.\n\n"
        "Detenete SIN enviar y devolvé el estado correspondiente si:\n"
        "- login_required: el sitio pide iniciar sesión (Indeed, Get on Board, Computrabajo, Bumeran, etc.) y no hay sesión abierta. "
        "Nunca escribas contraseñas ni crees cuentas. Antes de devolver login_required (o blocked por cuenta), navegá hasta "
        "la página de INICIO DE SESIÓN o REGISTRO (la que muestra «Continuar con Google» / «Sign in with Google» o el "
        "formulario de login; si hay que tocar «Iniciar sesión» o «Crear cuenta» para llegar, tocalo) y devolvé esa URL "
        "exacta en login_url, y login_google=true si ofrece Google.\n"
        "- blocked: hay que crear una cuenta nueva, hay captcha, una prueba técnica o un paso que no podés completar.\n"
        "- needs_human: el formulario exige un dato que el perfil tiene como COMPLETAR o que no sabés; decí cuál en reason.\n"
        "- closed: el aviso ya no acepta postulaciones. already_applied: ya figura como postulado.\n"
        "- not_fit: al leer el aviso completo resulta claramente incompatible.\n"
        "En answers listá cada pregunta del formulario y lo que respondiste. No cierres el navegador vos.")
    if dry_run:
        prompt += ("\n\nMODO ENSAYO: completá todo pero NO pulses el botón final de envío. Al llegar ahí devolvé "
                   "status=needs_human con reason='ensayo: listo para enviar' (o el estado real si te frenaste antes).")
        try:
            return llm.ask(system_prompt(cfg), prompt, FORM_SCHEMA, model=cfg["autopilot"].get("model", "sonnet"),
                           timeout=int(cfg["autopilot"].get("form_timeout_minutes", 15)) * 60,
                           extra=["--mcp-config", str(mcp_config(cfg)), "--strict-mcp-config",
                                  "--allowedTools", "mcp__playwright"])
        finally:
            close_agent_edge()
    reserve(job, "", decision, cv, "form")
    try:
        result = llm.ask(system_prompt(cfg), prompt, FORM_SCHEMA, model=cfg["autopilot"].get("model", "sonnet"),
                         timeout=int(cfg["autopilot"].get("form_timeout_minutes", 15)) * 60,
                         extra=["--mcp-config", str(mcp_config(cfg)), "--strict-mcp-config",
                                "--allowedTools", "mcp__playwright"])
    except llm.LLMError as exc:
        with connect() as db:
            db.execute("UPDATE applications SET status='unknown', updated_at=? WHERE job_id=?", (now(), job["id"]))
        radar.set_status(job["id"], "failed", f"La sesión del navegador terminó sin resultado ({exc}). "
                                               "Puede haber quedado enviada: revisá tu correo antes de reintentar.")
        return "failed"
    finally:
        close_agent_edge()
    answers = "\n".join(f"- {a['question']}: {a['answer']}" for a in result["answers"])
    status = result["status"]
    if status in ("applied", "already_applied"):
        body = decision["body"] + ("\n\nRespuestas del formulario:\n" + answers if answers else "")
        with connect() as db:
            db.execute("UPDATE applications SET body=?, note=? WHERE job_id=?", (body, result["final_url"], job["id"]))
        record_sent(job, "form", {**decision, "body": body}, f"form-{job['id']}", "",
                    f"Formulario enviado en {result['site']} con CV {cv['id']}"
                    + (" (ya estaba postulado)" if status == "already_applied" else ""))
        return "applied"
    with connect() as db:
        db.execute("UPDATE applications SET status='not_sent', note=?, updated_at=? WHERE job_id=?",
                   (result["reason"], now(), job["id"]))
        if status == "login_required":
            log_event(db, job["id"], "login_required", f"{result['site']}: {result['reason']}")
    if status == "login_required":
        with connect() as db:
            tries = db.execute("SELECT COUNT(*) n FROM events WHERE job_id=? AND kind='login_required'",
                               (job["id"],)).fetchone()["n"]
        if tries >= 2:  # don't retry forever: the user has to sign in / create the account once
            return needs_account(service, job, cfg, result["site"], result["reason"], result)
        return "login_required:" + (result["site"] or "")
    if status == "needs_human":
        radar.set_status(job["id"], "action_needed", "Formulario sin enviar: " + result["reason"])
        notify(service, cfg, f"Falta un dato para postular a {job['company']}",
               f"{job['title']} en {job['company']}\n{job['url']}\n\n{result['reason']}\n\n"
               "Completalo en tu perfil (Configuración) y mové la oferta a «Nueva», o postulate a mano.")
        return "action_needed"
    if status in ("closed", "not_fit"):
        radar.set_status(job["id"], "skipped", result["reason"])
        return "skipped"
    # Anything else the agent can't do (new account, captcha, test...) is the user's call: never a silent dead end.
    if ACCOUNT_HINTS.search(result["reason"]):
        return needs_account(service, job, cfg, result["site"], result["reason"], result)
    radar.set_status(job["id"], "action_needed", "No se pudo postular: " + result["reason"])
    notify(service, cfg, f"No pude postular a {job['company']}",
           f"{job['title']} en {job['company']}\n{job['url']}\n\n{result['reason']}")
    return "action_needed"


ACCOUNT_HINTS = re.compile(r"cuenta|registr|sign ?up|sign ?in|login|iniciar sesi|crear .*usuario|account", re.I)


def needs_account(service, job: dict, cfg: dict, site: str, reason: str, result: dict | None = None) -> str:
    """The site needs an account or a login: ask the user to do it once in the agent's Edge, then retry.
    Stores the exact login page the agent reached, so the user only has to press «Continue with Google»."""
    result = result or {}
    login_url = (result.get("login_url") or "").strip()
    with connect() as db:
        db.execute("UPDATE applications SET login_url=?, login_google=?, updated_at=? WHERE job_id=?",
                   (login_url if login_url.startswith("http") else "", int(bool(result.get("login_google"))),
                    now(), job["id"]))
    radar.set_status(job["id"], "action_needed",
                     f"CUENTA: {site or radar.urlparse(job['url']).netloc} pide crear cuenta o iniciar sesión. {reason}")
    notify(service, cfg, f"Creá la cuenta para postular a {job['company']}",
           f"{job['title']} en {job['company']}\n{job['url']}\n\n{reason}\n\n"
           f"En WorkWorm → Pipeline → Tu atención: «Abrir en el Edge del agente», creá la cuenta con {cfg['candidate_email']} "
           "(o entrá con Google), cerrá la ventana y tocá «Listo, reintentar».")
    return "action_needed"


EXCLUSION_DEFAULTS = {
    "title_keywords": ["Senior", "Sr", "Staff", "Principal", "Lead", "Head of"],
    "title_exceptions": ["Semi Senior", "Semi-Senior", "Semisenior", "SSr"],
    "max_years": 3,
    "companies": [],
    "rules": "- No postular si exige 6 o más años de experiencia como requisito excluyente.\n"
             "- No postular si no acepta candidatos que vivan en mi país.",
}


CLOUD_DEFAULTS = {
    "enabled": False,
    "allow_if_microsoft_too": True,
    "microsoft_terms": ["Azure", "Microsoft Fabric", "Fabric", "Synapse", "Data Factory", "ADF", "Power BI",
                        "SQL Server", "SSIS", "T-SQL", "Purview", "OneLake", "Cosmos DB", "Dynamics 365", "Microsoft"],
    "blocked_terms": ["AWS", "Amazon Web Services", "Redshift", "Glue", "EMR", "Athena", "Kinesis", "Lambda",
                      "SageMaker", "DynamoDB", "S3", "GCP", "Google Cloud", "BigQuery", "Big Query", "Cloud Composer",
                      "Dataproc", "Google Dataflow", "Pub/Sub", "Vertex AI"],
}


def exclusions(cfg: dict) -> dict:
    return {**EXCLUSION_DEFAULTS, **cfg.get("exclusions", {})}


def cloud_filter(cfg: dict) -> dict:
    return {**CLOUD_DEFAULTS, **cfg.get("cloud_filter", {})}


def _terms_in(text: str, terms: list[str]) -> list[str]:
    body = " " + re.sub(r"[^a-z0-9/]+", " ", normalize(text)) + " "
    return [t for t in terms if " " + re.sub(r"[^a-z0-9/]+", " ", normalize(t)).strip() + " " in body]


def cloud_block(job: dict, cfg: dict) -> str:
    """Microsoft/Azure-only rule: reason to skip when the offer asks for AWS/GCP (and, by default, no Microsoft stack)."""
    rule = cloud_filter(cfg)
    if not rule["enabled"]:
        return ""
    text = f"{job.get('title', '')}\n{job.get('description', '')}"
    blocked = _terms_in(text, rule["blocked_terms"])
    if not blocked:
        return ""
    if rule["allow_if_microsoft_too"] and _terms_in(text, rule["microsoft_terms"]):
        return ""
    return "Excluida por nube (no es ecosistema Microsoft): " + ", ".join(blocked[:4])


YEARS_RE = re.compile(r"(?:(?:al menos|minimo|minimum of|at least|mas de|more than|over)\s+)?(\d{1,2})\s*\+?\s*"
                      r"(?:(?:a|y|to|and|-|–)\s*\d{1,2}\s*)?(?:anos|años|years?|yrs)")
OPTIONAL_HINTS = ("deseable", "preferred", "preferably", "nice to have", "plus", "valorable", "ideally")


def required_years(text: str) -> int:
    """Largest years-of-experience requirement stated in the posting (0 if none); 'nice to have' mentions ignored."""
    body = normalize(text)
    found = 0
    for match in YEARS_RE.finditer(body):
        window = body[max(0, match.start() - 80):match.end() + 80]
        if not any(w in window for w in ("experien", "trayectoria")):
            continue
        if any(h in window for h in OPTIONAL_HINTS):
            continue
        years = int(match.group(1))
        if years <= 15:
            found = max(found, years)
    return found


def excluded(job: dict, cfg: dict) -> str:
    """Reason the offer matches a hard exclusion from config (title keyword, company, years, cloud), or ''."""
    rules = exclusions(cfg)
    title = " " + re.sub(r"[^a-z0-9]+", " ", normalize(job.get("title", ""))) + " "
    for phrase in rules.get("title_exceptions", []):
        title = title.replace(" " + re.sub(r"[^a-z0-9]+", " ", normalize(phrase)).strip() + " ", " ")
    for word in rules["title_keywords"]:
        token = " " + re.sub(r"[^a-z0-9]+", " ", normalize(word)).strip() + " "
        if token.strip() and token in title:
            return f"Excluida por palabra en el cargo: «{word}»"
    company = normalize(job.get("company", ""))
    for name in rules["companies"]:
        if normalize(name) and normalize(name) in company:
            return f"Excluida por empresa: «{name}»"
    limit = int(rules.get("max_years") or 0)
    if limit and job.get("description"):
        years = required_years(job["description"])
        if years > limit:
            return f"Excluida por experiencia: pide {years}+ años (máximo configurado {limit})"
    return cloud_block(job, cfg) if job.get("description") else ""


def apply_exclusions(cfg: dict, statuses: tuple[str, ...] = ("new", "manual")) -> int:
    """Move offers matching a hard exclusion to 'skipped'. Links the user added are never excluded."""
    moved = 0
    with connect() as db:
        rows = [dict(r) for r in db.execute(
            f"SELECT * FROM jobs WHERE source != 'link' AND status IN ({','.join('?' * len(statuses))})", statuses)]
        for row in rows:
            reason = excluded(row, cfg)
            if reason:
                db.execute("UPDATE jobs SET status='skipped', updated_at=? WHERE id=?", (now(), row["id"]))
                log_event(db, row["id"], "status", reason)
                moved += 1
    return moved


def is_manual(job: dict) -> bool:
    """LinkedIn offers are never auto-applied: the user applies by hand."""
    return job["source"] == "linkedin_alert" or "linkedin.com" in (job.get("url") or "").lower()


def triage_manual(cfg: dict) -> int:
    """Move new LinkedIn offers to 'manual'. No LLM, no browser."""
    moved = 0
    with connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE status='new' AND description != ''")]
        for row in filter(is_manual, rows):
            db.execute("UPDATE jobs SET status='manual', updated_at=? WHERE id=?", (now(), row["id"]))
            moved += 1
    return moved


PROFILE_SECTION = "## Datos extra (respondidos en el tablero)"


def remember_in_profile(cfg: dict, text: str, context: str) -> None:
    """Append user-provided facts to profile.md so future applications and replies can use them."""
    path = radar.ROOT / cfg.get("profile_path", "profile.md")
    body = path.read_text(encoding="utf-8") if path.exists() else ""
    if PROFILE_SECTION not in body:
        body = body.rstrip() + f"\n\n{PROFILE_SECTION}\n"
    lines = "\n".join(f"- {line.strip().lstrip('-').strip()}" for line in text.strip().splitlines() if line.strip())
    body = body.rstrip() + f"\n<!-- {local_now():%d/%m/%Y} · {context} -->\n{lines}\n"
    path.write_text(body, encoding="utf-8")


MANUAL_FINISH_HINTS = re.compile(r"captcha|recaptcha|hcaptcha|no soy un robot|i.?m not a robot|verificaci[oó]n humana|"
                                 r"prueba t[eé]cnica|assessment|test t[eé]cnico", re.I)


def attention_kind(job: dict) -> str:
    """'account' when a site needs a login/new account, 'form' when a web form was left unsent,
    'reply' when a recruiter message needs the user."""
    note = job.get("note") or ""
    if note.startswith("CUENTA:"):
        return "account"
    if MANUAL_FINISH_HINTS.search(note):
        return "finish"  # captcha / human check: the user submits by hand with the prepared CV and letter
    return "form" if job.get("app_status") in ("not_sent", "") and job.get("channel") == "form" else "reply"


def open_login_pages(job_ids: list[int]) -> int:
    """Open the stored login page (or the offer) of each job in the agent's Edge, one tab per site."""
    urls, seen = [], set()
    for job_id in job_ids:
        job = radar.job(job_id)
        url = job.get("login_url") or job["url"]
        host = radar.urlparse(url).netloc
        if host not in seen:
            seen.add(host)
            urls.append(url)
    if urls:
        open_agent_edge(tuple(urls))
    return len(urls)


def retry_after_account(job_id: int) -> None:
    """User created the account / signed in inside the agent's Edge: queue the offer again."""
    with connect() as db:
        db.execute("UPDATE jobs SET status='new', seen_at=?, updated_at=? WHERE id=?", (now(), now(), job_id))
        db.execute("UPDATE applications SET note='', updated_at=? WHERE job_id=?", (now(), job_id))
        db.execute("UPDATE events SET kind='login_required_done' WHERE job_id=? AND kind='login_required'", (job_id,))
        log_event(db, job_id, "status", "Cuenta lista: vuelve a la cola para postular")


def resolve_form(job_id: int, answers: str, cfg: dict, remember: bool) -> None:
    """User filled the missing data: store it and put the offer back in the queue (it gets priority)."""
    job = radar.job(job_id)
    with connect() as db:
        db.execute("UPDATE applications SET user_input=?, note='', updated_at=? WHERE job_id=?",
                   ((job.get("user_input", "") + "\n" + answers).strip(), now(), job_id))
        db.execute("UPDATE jobs SET status='new', seen_at=?, updated_at=? WHERE id=?", (now(), now(), job_id))
        log_event(db, job_id, "user_input", "Completaste datos; vuelve a la cola para postular")
    if remember:
        remember_in_profile(cfg, answers, f"{job['title']} · {job['company']}")


REPLY_WITH_INPUT = {
    "type": "object",
    "properties": {"reply_body": {"type": "string"}},
    "required": ["reply_body"],
}


def resolve_reply(service, job_id: int, answer: str, cfg: dict, remember: bool, verbatim: bool = False) -> str:
    """Answer the recruiter's last message using what the user wrote. Returns the text sent."""
    job = radar.job(job_id)
    inbound = [m for m in radar.thread(job_id) if m["direction"] == "in"]
    if not inbound:
        raise ValueError("No hay un mensaje del reclutador para responder en esta postulación.")
    last = inbound[-1]
    # Claim the reply atomically before the slow LLM call: a second click (or a Streamlit rerun) while the first
    # one is still drafting must not send a second answer to the same message.
    with connect() as db:
        previous = db.execute("SELECT last_outbound_at FROM applications WHERE job_id=?", (job_id,)).fetchone()
        claimed = db.execute("UPDATE applications SET last_outbound_at=? WHERE job_id=? AND last_outbound_at < ?",
                             (now(), job_id, last["at"])).rowcount
    if not claimed:
        raise ValueError("Ya hay una respuesta enviada (o enviándose) para el último mensaje del reclutador. "
                         "Si querés agregar algo, escribile desde Gmail.")
    try:
        if verbatim:
            body = answer.strip()
        else:
            history = "\n\n".join(f"[{m['at'][:16]} {'YO' if m['direction'] == 'out' else m['sender']}]\n{m['body'][:1500]}"
                                   for m in radar.thread(job_id))
            prompt = ("Redactá la respuesta a este último mensaje del reclutador usando la información que te da el candidato. "
                      "Respondé en el idioma del mensaje, breve y profesional, con firma.\n\n"
                      f"=== LO QUE DICE EL CANDIDATO ===\n{answer}\n\n=== HISTORIAL ===\n{history}\n\n"
                      f"=== MENSAJE A RESPONDER ===\nDe: {last['sender']}\nAsunto: {last['subject']}\n\n{last['body'][:4000]}")
            body = llm.ask(system_prompt(cfg), prompt, REPLY_WITH_INPUT, model=cfg["autopilot"].get("model", "sonnet"))["reply_body"]
        original = service.users().messages().get(userId="me", id=last["gmail_id"], format="metadata",
                                                   metadataHeaders=["Message-ID", "References", "Reply-To", "From"]).execute()
        to = header(original, "Reply-To") or header(original, "From") or last["sender"]
        subject = last["subject"] if last["subject"].lower().startswith(("re:", "rv:")) else f"Re: {last['subject']}"
        sent = send(service, build_raw(cfg, to, subject, body, reply_to_id=header(original, "Message-ID"),
                                       references=header(original, "References")), last["thread_id"])
    except Exception:
        # Nothing went out: release the claim so the user can retry.
        with connect() as db:
            db.execute("UPDATE applications SET last_outbound_at=? WHERE job_id=?",
                       (previous["last_outbound_at"] if previous else "", job_id))
        raise
    with connect() as db:
        db.execute("""INSERT OR IGNORE INTO messages(gmail_id,job_id,thread_id,direction,sender,subject,body,kind,summary,at)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (sent["id"], job_id, sent["threadId"], "out", cfg["candidate_email"], subject, body, "reply",
                    "Respuesta con tus datos", now()))
        db.execute("UPDATE applications SET last_outbound_at=?, note='', updated_at=? WHERE job_id=?", (now(), now(), job_id))
        log_event(db, job_id, "replied", f"Respondí a {parseaddr(to)[1]} con los datos que completaste")
    radar.set_status(job_id, "conversation")
    if remember:
        remember_in_profile(cfg, answer, f"{job['title']} · {job['company']}")
    return body


def mark_applied_manually(job_id: int) -> None:
    """The user applied on their own (e.g. LinkedIn); start following the conversation."""
    with connect() as db:
        db.execute("""INSERT INTO applications(job_id,recipient,subject,body,status,channel,sent_at,last_outbound_at,updated_at)
                      VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET status='sent', channel='manual',
                      sent_at=excluded.sent_at, last_outbound_at=excluded.last_outbound_at, updated_at=excluded.updated_at""",
                   (job_id, "", "", "", "sent", "manual", now(), now(), now()))
        db.execute("UPDATE jobs SET status='applied', updated_at=? WHERE id=?", (now(), job_id))
        log_event(db, job_id, "applied", "Te postulaste vos")


def check_every_seconds(cfg: dict) -> int:
    """How often the agent looks at the inbox (also the scheduled-task interval)."""
    return int(float(cfg["autopilot"].get("check_every_hours", 4)) * 3600)


def queue_size() -> int:
    """Offers waiting for the agent to apply (LinkedIn ones are the user's and are not counted)."""
    with connect() as db:
        return db.execute("SELECT COUNT(*) n FROM jobs WHERE status='new' AND description != ''").fetchone()["n"]


def auto_apply(service, cfg: dict, report: dict) -> None:
    """Apply to every queued offer until the queue is empty or the daily cap is reached."""
    pilot = cfg["autopilot"]
    if not within(pilot["apply_hours"]):
        return
    with connect() as db:
        budget = pilot["max_applications_per_day"] - applications_today(db)
        rows = [dict(r) for r in db.execute(
            """SELECT * FROM jobs WHERE status='new' AND description != ''
               ORDER BY source='link' DESC, COALESCE(NULLIF(posted_at, ''), seen_at) DESC""")]
    if budget <= 0:
        report["daily_cap_reached"] = True
        return
    no_login: set[str] = set()
    last_mail_check, llm_failures = time.time(), 0
    for done, row in enumerate(rows):
        if budget <= 0:
            report["daily_cap_reached"] = True
            break
        if time.time() - last_mail_check > check_every_seconds(cfg):
            progress("Revisando tus correos (pausa entre postulaciones)", queue_done=done, queue_total=len(rows))
            try:
                track(service, cfg, report)
            except Exception as exc:
                report["errors"].append(f"seguimiento de correos: {exc}")
            last_mail_check = time.time()
        domain = radar.urlparse(row["url"]).netloc.replace("www.", "")
        if not row["contact_email"] and domain in no_login:
            continue
        progress(f"Postulando: {row['title']} · {row['company'] or 'empresa sin dato'}",
                 queue_done=done, queue_total=len(rows))
        try:
            status = apply(service, radar.job(row["id"]), cfg)
            llm_failures = 0
        except llm.LLMError as exc:
            report["errors"].append(f"LLM al postular a {row['company']}: {exc}")
            llm_failures += 1
            if llm_failures >= 3:
                report["errors"].append("Claude falló 3 veces seguidas: se retoma en la próxima revisión")
                break
            continue
        if status.startswith("login_required"):
            no_login.add(domain)
            report.setdefault("login_required", []).append(status.split(":", 1)[1] or domain)
            continue
        report["applied" if status == "applied" else "skipped"] += 1
        if status == "applied":
            budget -= 1
    if report.get("login_required"):
        sites = sorted(set(report["login_required"]))
        with connect() as db:
            already = db.execute("SELECT COUNT(*) n FROM events WHERE kind='login_notice' AND at >= ?",
                                 ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),)).fetchone()["n"]
            log_event(db, None, "login_notice", ", ".join(sites))
        if not already:
            notify(service, cfg, "Iniciá sesión en el Edge del agente",
                   "No pude postular porque estos sitios piden sesión: " + ", ".join(sites) +
                   "\n\nEn el tablero: Configuración → «Abrir Edge del agente», iniciá sesión y cerrá la ventana.")


# ---------- tracking ----------

def known_threads(db, job_id: int, app_thread: str) -> set[str]:
    threads = {r["thread_id"] for r in db.execute("SELECT DISTINCT thread_id FROM messages WHERE job_id=?", (job_id,))}
    return {t for t in threads | {app_thread} if t}


def match_job(sender: str, text: str, apps: list[dict]) -> dict | None:
    address = parseaddr(sender)[1].lower()
    domain = address.split("@")[-1]
    if any(x in address for x in ("jobalert", "alert", "newsletter", "digest", "jobs-listings", "marketing")):
        return None
    for app in apps:
        if address == app["recipient"]:
            return app
    for app in apps:
        app_domain = app["recipient"].split("@")[-1]
        if domain == app_domain and domain not in FREE_DOMAINS:
            return app
    body = normalize(text)
    for app in apps:
        name = normalize(app["company"])
        if len(name) >= 4 and re.search(rf"\b{re.escape(name)}\b", body):
            return app
    return None


def handle_inbound(service, cfg: dict, app: dict, message: dict, report: dict) -> None:
    sender, subject = header(message, "From"), header(message, "Subject")
    text = plain_text(message["payload"])[:6000]
    history = "\n\n".join(f"[{m['at'][:16]} {'YO' if m['direction'] == 'out' else m['sender']}] {m['subject']}\n{m['body'][:1500]}"
                          for m in radar.thread(app["id"]))
    is_linkedin = "linkedin.com" in sender.lower()
    prompt = (
        "Llegó este correo relacionado con una postulación. Clasificalo y, si corresponde, redactá la respuesta.\n"
        "Política: respondé vos todo lo que puedas con el perfil y el CV (pretensión, disponibilidad, inglés, experiencia). "
        "Respondé siempre en el idioma en que está escrito el correo recibido. "
        "Entrevistas: aceptá el día y horario que proponga el reclutador; si solo piden disponibilidad, decí que te adaptás "
        "y pedí que propongan día y hora. Si confirman una entrevista, agradecé brevemente y confirmá. "
        "auto_ack (acuse automático) y rejection: should_reply=false. not_related si el correo no tiene que ver con esta postulación. "
        "needs_human=true solo si: falta un dato marcado COMPLETAR que hace falta, piden documentos personales, "
        "hay una propuesta económica formal, piden completar una prueba técnica o formulario, o el mensaje es ambiguo. "
        "Si needs_human es true, should_reply=false.\n"
        + ("Este correo es una notificación de LinkedIn: no se puede responder por mail; si requiere respuesta, needs_human=true.\n" if is_linkedin else "")
        + f"\n=== POSTULACIÓN ===\nCargo: {app['title']}\nEmpresa: {app['company']}\nCV enviado a: {app['recipient']} "
          f"el {app['sent_at'][:10]}\nEstado actual: {radar.STATUSES[app['status']]}\n\nDescripción del aviso:\n{(app['description'] or '')[:3000]}"
          f"\n\n=== HISTORIAL ===\n{history or '(sin mensajes)'}\n\n=== CORREO NUEVO ===\nDe: {sender}\nAsunto: {subject}\n\n{text}"
          + "\n\n=== CV ENVIADO ===\n" + cv_texts(cfg).get(next((c["id"] for c in cfg["cvs"] if Path(c["path"]).name == app["cv_file"]), cfg["cvs"][0]["id"]), ""))
    result = llm.ask(system_prompt(cfg), prompt, REPLY_SCHEMA, model=cfg["autopilot"].get("model", "sonnet"))
    at = datetime.fromtimestamp(int(message["internalDate"]) / 1000, timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        db.execute("""INSERT OR IGNORE INTO messages(gmail_id,job_id,thread_id,direction,sender,subject,body,kind,summary,at)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (message["id"], app["id"] if result["kind"] != "not_related" else None, message["threadId"], "in",
                    sender, subject, text, result["kind"], result["summary"], at))
        if result["kind"] == "not_related":
            return
        db.execute("UPDATE applications SET last_inbound_at=?, updated_at=? WHERE job_id=?", (at, now(), app["id"]))
        log_event(db, app["id"], "inbound", f"{result['kind']}: {result['summary']}")
    report["inbound"] += 1
    link = mail_link(message["threadId"])
    if result["needs_human"]:
        radar.set_status(app["id"], "action_needed", result["needs_human_reason"])
        notify(service, cfg, f"Necesito tu respuesta: {app['company']}",
               f"{app['title']} en {app['company']}\n\n{result['summary']}\n\nMotivo: {result['needs_human_reason']}\n\nHilo: {link}")
        report["action_needed"] += 1
        return
    if result["should_reply"] and result["reply_body"].strip():
        to = header(message, "Reply-To") or sender
        reply_subject = subject if subject.lower().startswith(("re:", "rv:")) else f"Re: {subject}"
        raw = build_raw(cfg, to, reply_subject, result["reply_body"], reply_to_id=header(message, "Message-ID"),
                        references=header(message, "References"))
        sent = send(service, raw, message["threadId"])
        with connect() as db:
            db.execute("""INSERT OR IGNORE INTO messages(gmail_id,job_id,thread_id,direction,sender,subject,body,kind,summary,at)
                          VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (sent["id"], app["id"], sent["threadId"], "out", cfg["candidate_email"], reply_subject,
                        result["reply_body"], "reply", "Respuesta automática", now()))
            db.execute("UPDATE applications SET last_outbound_at=?, updated_at=? WHERE job_id=?", (now(), now(), app["id"]))
            log_event(db, app["id"], "replied", f"Respondí a {parseaddr(to)[1]}")
        report["replied"] += 1
    kind = result["kind"]
    if kind == "rejection":
        radar.set_status(app["id"], "rejected")
    elif kind == "offer":
        radar.set_status(app["id"], "offer", result["summary"])
        notify(service, cfg, f"Propuesta de {app['company']}", f"{result['summary']}\n\nHilo: {link}")
    elif kind in ("interview_proposed", "interview_confirmed") and result["interview_at"]:
        with connect() as db:
            db.execute("UPDATE applications SET interview_at=?, interview_link=?, updated_at=? WHERE job_id=?",
                       (result["interview_at"], result["interview_link"], now(), app["id"]))
        radar.set_status(app["id"], "interview")
        when = radar.parse_time(result["interview_at"])
        import lifeprime
        calendar_text = ""
        if lifeprime.settings(cfg)["enabled"] and lifeprime.settings(cfg)["url"]:
            calendar = lifeprime.add_interview(cfg, app, result["interview_at"], result["interview_link"], result["summary"])
            calendar_text = ("Cargada en tu calendario de LifePrime." if calendar["created"] or calendar["already_existed"]
                             else f"NO se pudo cargar en LifePrime: {calendar['error']}. Cargala a mano.")
            with connect() as db:
                log_event(db, app["id"], "calendar", calendar_text)
        notify(service, cfg, f"Entrevista con {app['company']} — {when.astimezone(LOCAL_TZ):%a %d/%m %H:%M}" if when else f"Entrevista con {app['company']}",
               f"{app['title']} en {app['company']}\nCuándo: {result['interview_at']}\nLink: {result['interview_link'] or '(sin link todavía)'}\n"
               + (f"{calendar_text}\n\n" if calendar_text else "\n") + f"{result['summary']}\n\nHilo: {link}")
        report["interviews"] += 1
    elif kind != "auto_ack" and app["status"] in ("applied", "action_needed"):
        radar.set_status(app["id"], "conversation")


def track(service, cfg: dict, report: dict) -> None:
    me = cfg["candidate_email"].lower()
    with connect() as db:
        apps = [dict(r) for r in db.execute(
            f"""SELECT j.*, a.recipient, a.thread_id, a.sent_at, a.cv_file FROM jobs j JOIN applications a ON a.job_id=j.id
                WHERE j.status IN ({','.join('?' * len(ACTIVE))}) AND a.status='sent'""", ACTIVE)]
        seen = {r["gmail_id"] for r in db.execute("SELECT gmail_id FROM messages")}
    if not apps:
        return
    pending: list[tuple[dict, dict]] = []
    thread_owner = {}
    for app in apps:
        with connect() as db:
            for t in known_threads(db, app["id"], app["thread_id"]):
                thread_owner[t] = app
    for thread_id, app in thread_owner.items():
        data = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        for message in data.get("messages", []):
            if message["id"] not in seen:
                pending.append((app, message))
                seen.add(message["id"])
    # Recruiters often answer from a new thread or another address.
    oldest = min(radar.parse_time(a["sent_at"]) for a in apps)
    # All mail, not just the inbox: mail_organizer archives job mail out of the inbox.
    query = f"-from:me -in:sent -in:spam -in:trash -in:drafts after:{int(oldest.timestamp())}"
    listing = service.users().messages().list(userId="me", q=query, maxResults=200).execute()
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS seen_mail (gmail_id TEXT PRIMARY KEY)")
        ignored = {r["gmail_id"] for r in db.execute("SELECT gmail_id FROM seen_mail")}
    for item in listing.get("messages", []):
        if item["id"] in seen or item["id"] in ignored or item["threadId"] in thread_owner:
            continue
        message = service.users().messages().get(userId="me", id=item["id"], format="full").execute()
        app = match_job(header(message, "From"), header(message, "Subject") + "\n" + plain_text(message["payload"]), apps)
        if app:
            pending.append((app, message))
        else:
            with connect() as db:
                db.execute("INSERT OR IGNORE INTO seen_mail(gmail_id) VALUES(?)", (item["id"],))
    pending.sort(key=lambda pair: int(pair[1]["internalDate"]))
    for app, message in pending:
        sender = parseaddr(header(message, "From"))[1].lower()
        if sender == me:
            # Something I (or this agent) sent from Gmail directly: record it.
            at = datetime.fromtimestamp(int(message["internalDate"]) / 1000, timezone.utc).isoformat(timespec="seconds")
            with connect() as db:
                db.execute("""INSERT OR IGNORE INTO messages(gmail_id,job_id,thread_id,direction,sender,subject,body,kind,summary,at)
                              VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           (message["id"], app["id"], message["threadId"], "out", me, header(message, "Subject"),
                            plain_text(message["payload"])[:4000], "manual", "Enviado desde Gmail", at))
                db.execute("UPDATE applications SET last_outbound_at=? WHERE job_id=?", (at, app["id"]))
                status = db.execute("SELECT status FROM jobs WHERE id=?", (app["id"],)).fetchone()["status"]
            if status == "action_needed":
                radar.set_status(app["id"], "conversation", "Respondiste vos desde Gmail")
            continue
        if not within(cfg["autopilot"]["reply_hours"]):
            continue  # Process it in reply hours; it stays unseen until then.
        current = radar.job(app["id"])
        try:
            handle_inbound(service, cfg, {**app, **current, "status": current["status"]}, message, report)
        except llm.LLMError as exc:
            report["errors"].append(f"LLM con correo de {app['company']}: {exc}")
            break


def followups(service, cfg: dict, report: dict) -> None:
    pilot = cfg["autopilot"]
    if not within(pilot["apply_hours"]):
        return
    limit = (datetime.now(timezone.utc) - timedelta(days=pilot["followup_after_days"])).isoformat()
    with connect() as db:
        rows = [dict(r) for r in db.execute(
            """SELECT j.id, j.title, j.company, a.* FROM jobs j JOIN applications a ON a.job_id=j.id
               WHERE j.status='applied' AND a.status='sent' AND a.channel='email' AND a.followup_at='' AND a.last_inbound_at=''
               AND a.sent_at < ?""", (limit,))]
    for row in rows:
        if row["language"] == "en":
            body = (f"Hi,\n\nI'm following up on my application for the {row['title']} position, sent on "
                    f"{row['sent_at'][:10]}. I'm still very interested and happy to share anything else you need.\n\n"
                    f"Best regards,\n{cfg['candidate_name']}")
        else:
            body = (f"Hola,\n\nTe escribo para hacer seguimiento de mi postulación a {row['title']}, que envié el "
                    f"{datetime.fromisoformat(row['sent_at']).astimezone(LOCAL_TZ):%d/%m}. Sigo muy interesado; si necesitan "
                    f"algo más de mi parte, quedo atento.\n\nSaludos,\n{cfg['candidate_name']}")
        subject = row["subject"] if row["subject"].lower().startswith("re:") else f"Re: {row['subject']}"
        with connect() as db:
            db.execute("UPDATE applications SET followup_at=? WHERE job_id=?", (now(), row["job_id"]))
        sent = send(service, build_raw(cfg, row["recipient"], subject, body), row["thread_id"])
        with connect() as db:
            db.execute("""INSERT OR IGNORE INTO messages(gmail_id,job_id,thread_id,direction,sender,subject,body,kind,summary,at)
                          VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (sent["id"], row["job_id"], sent["threadId"], "out", cfg["candidate_email"], subject, body,
                        "followup", "Seguimiento automático", now()))
            db.execute("UPDATE applications SET last_outbound_at=? WHERE job_id=?", (now(), row["job_id"]))
            log_event(db, row["job_id"], "followup", f"Seguimiento enviado a {row['recipient']}")
        report["followups"] += 1


# ---------- orchestration ----------

def collect_due(cfg: dict) -> bool:
    with connect() as db:
        last = db.execute("SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    parsed = radar.parse_time(last["finished_at"]) if last else None
    return not parsed or datetime.now(timezone.utc) - parsed > timedelta(hours=cfg.get("collect_every_hours", 6))


def progress(step: str, **counters) -> None:
    """Record what the running cycle is doing (for the dashboard) and keep the lock fresh."""
    try:
        data = json.loads(PROGRESS.read_text(encoding="utf-8")) if PROGRESS.exists() else {"started": now()}
        PROGRESS.write_text(json.dumps({**data, **counters, "step": step, "since": now()}, ensure_ascii=False),
                            encoding="utf-8")
        if LOCK.exists():
            os.utime(LOCK)
    except (OSError, json.JSONDecodeError):
        pass


def next_run() -> datetime | None:
    """Next time Windows Task Scheduler will start the agent (None if the task is missing)."""
    import subprocess
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                              "(Get-ScheduledTaskInfo -TaskName 'WorkWorm').NextRunTime.ToString('o')"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        parsed = datetime.fromisoformat(out)
        return parsed if parsed.tzinfo else parsed.astimezone()
    except Exception:
        return None


def current_progress() -> dict:
    try:
        return json.loads(PROGRESS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def running() -> int | None:
    """PID of the cycle currently running, if any."""
    if not LOCK.exists() or time.time() - LOCK.stat().st_mtime > 45 * 60:
        return None
    try:
        return int(LOCK.read_text().strip())
    except ValueError:
        return None


def stop_cycle() -> bool:
    """Kill the running cycle with its Claude/Playwright children and the agent's Edge. Returns whether one was running."""
    import subprocess
    pid = running()
    root = radar.ROOT.name
    script = (f"$targets = Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{root}*update.py*' "
              f"-or $_.CommandLine -like '*{root}*edge-profile*' -or $_.CommandLine -like '*{root}*mcp.json*' "
              f"-or $_.CommandLine -like '*{root}*lifeprime_mcp.json*' }}; "
              + (f"$targets += Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; " if pid else "")
              + "foreach ($p in $targets) { Get-CimInstance Win32_Process -Filter \"ParentProcessId=$($p.ProcessId)\" | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
              "Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    LOCK.unlink(missing_ok=True)
    PROGRESS.unlink(missing_ok=True)
    with connect() as db:
        for row in db.execute("SELECT job_id FROM applications WHERE status='sending'").fetchall():
            db.execute("UPDATE applications SET status='unknown', updated_at=? WHERE job_id=?", (now(), row["job_id"]))
            db.execute("UPDATE jobs SET status='failed', updated_at=? WHERE id=?", (now(), row["job_id"]))
            log_event(db, row["job_id"], "status", "Ciclo detenido a mitad de la postulación: revisá si quedó enviada")
        log_event(db, None, "stopped", "Ciclo detenido desde el tablero")
    return pid is not None


def start_cycle(force_collect: bool = True) -> None:
    """Launch one cycle in the background so the dashboard stays responsive."""
    import subprocess
    import sys
    args = [sys.executable, str(radar.ROOT / "update.py")] + (["--collect"] if force_collect else [])
    subprocess.Popen(args, cwd=str(radar.ROOT), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "DETACHED_PROCESS", 0), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run(force_collect: bool = False) -> dict:
    """One autopilot cycle. Safe to call often: a lock prevents overlapping runs."""
    LOCK.parent.mkdir(exist_ok=True)
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 45 * 60:
        return {"skipped": "Ya hay un ciclo en curso"}
    LOCK.write_text(str(os.getpid()))
    PROGRESS.write_text(json.dumps({"started": now(), "step": "Iniciando", "since": now()}), encoding="utf-8")
    report = {"started": now(), "collected": None, "applied": 0, "skipped": 0, "inbound": 0, "replied": 0,
              "interviews": 0, "action_needed": 0, "followups": 0, "errors": []}
    try:
        cfg = radar.config()
        if force_collect or collect_due(cfg):
            progress("Buscando ofertas en los portales (1 a 3 min)")
            report["collected"] = radar.collect()
        progress("Aplicando exclusiones y armando tu lista de LinkedIn")
        for path in missing_cvs(cfg):
            report["errors"].append(f"Falta el archivo del CV: {path} (¿lo renombraste? actualizá config.json → cvs)")
        report["excluded"] = apply_exclusions(cfg)
        report["manual"] = triage_manual(cfg)
        service = radar.gmail_service()
        if not service:
            report["errors"].append("Gmail no está conectado")
            return report
        # With the autopilot off nothing is sent: no applications, replies or follow-ups.
        import linkedin_bot
        steps = ([("seguimiento de correos", track),
                  ("linkedin", lambda service, cfg, report: linkedin_bot.resolve(cfg, report, progress)),
                  ("postulaciones", auto_apply), ("recordatorios", followups)]
                 if cfg["autopilot"].get("enabled") else [])
        labels = {"seguimiento de correos": "Revisando tus correos y respondiendo a reclutadores",
                  "linkedin": "LinkedIn: buscando los botones «Solicitar» externos",
                  "postulaciones": "Postulando", "recordatorios": "Enviando seguimientos"}
        for name, step in steps:
            progress(labels[name])
            try:
                step(service, cfg, report)
            except Exception as exc:
                report["errors"].append(f"{name}: {exc}")
        # Last: tidy the main inbox (after tracking read everything).
        if radar.gmail_can_modify("main"):
            import mail_organizer
            progress("Ordenando tu Gmail: ofertas a su etiqueta")
            try:
                mail_organizer.organize(service, cfg, report)
            except Exception as exc:
                report["errors"].append(f"ordenar Gmail: {exc}")
        return report
    finally:
        report["finished"] = now()
        try:
            report["queue_left"] = queue_size()
        except Exception:
            pass
        LOCK.unlink(missing_ok=True)
        PROGRESS.unlink(missing_ok=True)
        with connect() as db:
            if any(report[k] for k in ("applied", "replied", "inbound", "followups")) or report["errors"]:
                log_event(db, None, "cycle", json.dumps({k: v for k, v in report.items() if k != "collected"}, ensure_ascii=False))
