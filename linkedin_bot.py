"""LinkedIn offers: find the external application link behind the «Solicitar» button, slowly.

Never applies on LinkedIn. Per offer: open the job (agent's Edge profile), wait for it to load, read the apply
button. «Solicitud sencilla / Easy Apply» stays for the user; «Solicitar» to a company site is clicked once, the
destination URL is stored as a queued offer for the form agent, and the browser is closed. Visits are rate-limited.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import radar
from radar import connect, log_event, now

EASY = ("solicitud sencilla", "easy apply", "candidatura simplificada")
# The apply control is an <a>/<button> whose aria-label/text says «Solicitar…» / «Solicitud sencilla» / «Apply».
APPLY_SELECTOR = ", ".join(f"{tag}[aria-label*='{word}']:visible" for tag in ("a", "button")
                           for word in ("olicit", "Apply", "apply")) + \
    ", button.jobs-apply-button:visible, a.jobs-apply-button:visible"


def external_from_href(href: str) -> str:
    """LinkedIn wraps external links as /safety/go/?url=<encoded>; return the real destination."""
    if not href:
        return ""
    parsed = urlparse(href)
    if "linkedin.com" in parsed.netloc and parsed.path.startswith("/safety/go"):
        return parse_qs(parsed.query).get("url", [""])[0]
    return href if href.startswith("http") and "linkedin.com" not in parsed.netloc else ""
CLOSED = ("ya no se aceptan solicitudes", "no longer accepting applications")
DEFAULTS = {"enabled": True, "max_visits": 5, "window_hours": 4, "load_wait_seconds": 15, "pause_seconds": 60,
            "headless": False}


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg.get("linkedin_external", {})}


def visits_in_window(cfg: dict) -> int:
    s = settings(cfg)
    since = (datetime.now(timezone.utc) - timedelta(hours=float(s["window_hours"]))).isoformat(timespec="seconds")
    with connect() as db:
        return db.execute("SELECT COUNT(*) n FROM events WHERE kind='linkedin_visit' AND at >= ?", (since,)).fetchone()["n"]


def next_slot(cfg: dict) -> datetime | None:
    """When a new LinkedIn visit becomes allowed again (None if allowed now)."""
    s = settings(cfg)
    with connect() as db:
        rows = [r["at"] for r in db.execute("SELECT at FROM events WHERE kind='linkedin_visit' ORDER BY id DESC LIMIT ?",
                                            (int(s["max_visits"]),))]
    if len(rows) < int(s["max_visits"]):
        return None
    oldest = radar.parse_time(rows[-1]) + timedelta(hours=float(s["window_hours"]))
    return oldest if oldest > datetime.now(timezone.utc) else None


def visit(job: dict, cfg: dict) -> dict:
    """Open one LinkedIn job and classify its apply button. Returns {kind, url, detail}.
    kind: easy | external | closed | login | unknown."""
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    import agent
    s = settings(cfg)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(agent.EDGE_PROFILE), channel="msedge",
                                                       headless=bool(s["headless"]), viewport={"width": 1280, "height": 900})
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(int(s["load_wait_seconds"]) * 1000)
            body = radar.normalize(page.inner_text("body")[:20000])
            if "/login" in page.url or "authwall" in page.url or "unete a linkedin" in body[:3000] \
                    or "join linkedin" in body[:3000]:
                return {"kind": "login", "url": "", "detail": "LinkedIn pide iniciar sesión en el Edge del agente"}
            if any(x in body for x in CLOSED):
                return {"kind": "closed", "url": "", "detail": "El aviso ya no acepta solicitudes"}
            button = page.locator(APPLY_SELECTOR).first
            if button.count() == 0:
                return {"kind": "unknown", "url": "", "detail": "No se encontró el botón de solicitud"}
            label = radar.normalize((button.inner_text() or "") + " " + (button.get_attribute("aria-label") or ""))
            if any(x in label for x in EASY):
                return {"kind": "easy", "url": "", "detail": "Solicitud sencilla: queda para vos"}
            href = external_from_href(button.get_attribute("href") or "")
            try:
                with context.expect_page(timeout=20000) as new_page:
                    button.click()
                    page.wait_for_timeout(2500)
                    # LinkedIn sometimes asks to confirm leaving / sharing the profile.
                    confirm = page.locator("button:has-text('Continuar'), button:has-text('Continue')").first
                    if confirm.count():
                        confirm.click()
                target = new_page.value
                target.wait_for_load_state("domcontentloaded", timeout=30000)
                for _ in range(15):  # follow LinkedIn's redirect out to the company site
                    if "linkedin.com" not in target.url:
                        break
                    target.wait_for_timeout(1000)
                url = target.url
            except PWTimeout:
                url = page.url if "linkedin.com" not in page.url else href
            if not url or "linkedin.com" in url:
                url = href  # the decoded destination of LinkedIn's /safety/go link
            if not url or "linkedin.com" in url:
                return {"kind": "unknown", "url": "", "detail": "El botón no llevó a un sitio externo"}
            return {"kind": "external", "url": url, "detail": "Solicitud externa"}
        finally:
            context.close()  # close everything after each offer


def resolve(cfg: dict, report: dict, progress=lambda *a, **k: None) -> None:
    """Process LinkedIn offers within the rate limit; external ones become queued offers for the form agent."""
    s = settings(cfg)
    if not s["enabled"]:
        return
    with connect() as db:
        rows = [dict(r) for r in db.execute(
            """SELECT * FROM jobs WHERE status='manual' AND linkedin_apply=''
               ORDER BY COALESCE(NULLIF(posted_at, ''), seen_at) DESC""")]
    for index, row in enumerate(rows):
        if visits_in_window(cfg) >= int(s["max_visits"]):
            report["linkedin_next_slot"] = (next_slot(cfg) or datetime.now(timezone.utc)).isoformat(timespec="seconds")
            break
        if index:
            progress(f"LinkedIn: pausa de {s['pause_seconds']} s antes del próximo aviso")
            time.sleep(int(s["pause_seconds"]))
        progress(f"LinkedIn: revisando el botón de «{row['title']} · {row['company']}»")
        try:
            result = visit(row, cfg)
        except Exception as exc:
            # The browser could not start (usually the agent's Edge was left open by hand): this offer is
            # not checked and the visit does not count against the LinkedIn limit.
            report["errors"].append("LinkedIn: no se pudo abrir el Edge del agente (¿quedó abierto a mano? cerralo). "
                                    + str(exc).splitlines()[0][:150])
            break
        with connect() as db:
            log_event(db, row["id"], "linkedin_visit", f"{result['kind']}: {result['detail']}")
        report.setdefault("linkedin", []).append(result["kind"])
        if result["kind"] == "login":
            report["errors"].append("LinkedIn pide iniciar sesión en el Edge del agente (Configuración → Abrir Edge del agente)")
            break
        with connect() as db:
            db.execute("UPDATE jobs SET linkedin_apply=?, updated_at=? WHERE id=?", (result["kind"], now(), row["id"]))
        if result["kind"] == "closed":
            radar.set_status(row["id"], "skipped", result["detail"])
        elif result["kind"] == "external":
            try:
                radar.add_link(result["url"], row["title"], row["company"], row["id"], source="linkedin_ext")
            except ValueError as exc:
                with connect() as db:
                    log_event(db, row["id"], "status", f"Link externo no agregado: {exc}")
