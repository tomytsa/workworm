"""Collectors, scoring and persistence for the local job radar."""

from __future__ import annotations

import base64
import copy
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "radar.sqlite3"
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_CONFIG_PATH = ROOT / "config.example.json"
SECRETS = ROOT / ".secrets"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
USER_AGENT = "WorkWorm/1.0 (job search assistant)"
# Scoring and home-country defaults; config.json → scoring / home overrides them (see config.example.json).
SKILLS = {
    "python": 9, "sql": 9, "azure data factory": 8, "adf": 8,
    "sql server": 7, "postgresql": 7, "etl": 8, "power bi": 5,
    "airflow": 5, "n8n": 4, "docker": 3, "metabase": 3,
    "data governance": 3, "data lineage": 3,
}
BONUS = {"databricks": 3, "pyspark": 2, "spark": 2, "lakehouse": 2}
ROLE_TERMS = ("data engineer", "data & analytics engineer", "ingeniero de datos", "ingeniera de datos", "etl", "analytics engineer", "dataops", "data ops", "ingenieria de datos", "databricks developer")
MAIN_TITLES = ("data engineer", "ingeniero de datos", "ingeniera de datos")
SENIOR_TERMS = ("senior", "sr.", "lead", "principal", "staff", "architect", "gerente", "manager", "jefe")
EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})(?![\w.-])")
JOB_URL_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/(?:comm/)?jobs/view/(\d+)", re.I)
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
HOME = {
    "country": "Argentina",
    "code": "ar",
    "timezone": "America/Argentina/Buenos_Aires",
    "places": ["argentina", "buenos aires", "capital federal", "caba", "neuquen", "mendoza", "cordoba", "la plata",
               "rosario", "munro", "lomas de zamora", "santa fe", "tucuman", "mar del plata"],
    "region_terms": ["latam", "latin america", "america latina", "south america"],
    "blocked_locations": ["united states", "usa", "us only", "canada", "europe", "serbia"],
}
BUMERAN_TZ = timezone(timedelta(hours=-3))  # Bumeran publishes Argentine local times

# Offer lifecycle. Order matters: it is the left-to-right order of the pipeline board.
STATUSES = {
    "new": "Nueva",
    "blocked": "No se pudo postular",
    "manual": "Lista para que postules vos",
    "skipped": "Descartada por el agente",
    "discarded": "Descartada por vos",
    "applied": "CV enviado · esperando respuesta",
    "conversation": "En conversación",
    "action_needed": "Requiere tu atención",
    "interview": "Entrevista agendada",
    "offer": "Propuesta recibida",
    "rejected": "Rechazada",
    "failed": "Envío fallido",
}
PIPELINE = ("applied", "conversation", "action_needed", "interview", "offer", "rejected")
LEGACY_STATUS = {"selected": "new", "draft": "new", "sent": "applied", "replied": "conversation", "no_email": "new"}
# Every platform the radar reads from: how offers arrive and how the agent applies.
SOURCES = {
    "linkedin_alert": ("LinkedIn", "Alertas de empleo recibidas en Gmail + descripción pública del aviso",
                       "Manual: el agente deja el link listo, te postulás vos y él sigue la conversación"),
    "indeed": ("Indeed Argentina", "Búsqueda con JobSpy (scraping del sitio público)",
               "Formulario de Indeed o del sitio de la empresa, con el navegador del agente"),
    "getonbrd": ("Get on Board", "API pública de búsqueda", "Formulario de Get on Board (requiere sesión iniciada en el Edge del agente)"),
    "greenhouse": ("Greenhouse", "API pública de cada empresa configurada", "Formulario de Greenhouse, con el navegador del agente"),
    "lever": ("Lever", "API pública de cada empresa configurada", "Formulario de Lever, con el navegador del agente"),
    "link": ("Links que agregás vos", "Pegados en el tablero (p. ej. el link externo de un aviso de LinkedIn)",
             "Formulario de la empresa o del tercerizado, con el navegador del agente"),
    "linkedin_ext": ("LinkedIn → sitio externo", "Botón «Solicitar» de avisos de LinkedIn (máx. 5 cada 4 h, 1 min entre avisos)",
                     "Formulario de la empresa, con el navegador del agente"),
    "computrabajo": ("Computrabajo", "Búsqueda en ar.computrabajo.com + página de cada aviso",
                     "Postulación en Computrabajo (sesión iniciada en el Edge del agente)"),
    "bumeran": ("Bumeran / ZonaJobs", "API de búsqueda del sitio (ZonaJobs publica los mismos avisos)",
                "Postulación en Bumeran (sesión iniciada en el Edge del agente)"),
    "startupsargentina": ("Startups Argentina", "Listado de startupsargentina.com/empleos + página de cada aviso que interesa",
                          "Formulario de la empresa (su ATS), con el navegador del agente"),
    "himalayas": ("Himalayas", "API pública de empleos remotos", "Formulario de la empresa, con el navegador del agente"),
    "remotive": ("Remotive", "API pública de empleos remotos", "Formulario de la empresa, con el navegador del agente"),
    "jobicy": ("Jobicy", "API pública de empleos remotos", "Formulario de la empresa, con el navegador del agente"),
}
TECH = ("python", "sql", "pyspark", "spark", "databricks", "airflow", "dbt", "snowflake", "bigquery",
        "redshift", "aws", "azure", "gcp", "google cloud", "azure data factory", "synapse", "fabric",
        "kafka", "docker", "kubernetes", "terraform", "power bi", "tableau", "looker", "postgresql",
        "sql server", "mysql", "mongodb", "scala", "java", "fivetran", "airbyte", "n8n", "pandas",
        "delta lake", "glue", "lambda", "s3", "etl", "elt", "data lake", "data warehouse", "git", "ci/cd")


_CONFIG_CACHE: dict = {}


def config() -> dict:
    """config.json (or config.example.json until the user saves their own). Cached by mtime; returns a copy."""
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    key = (str(path), path.stat().st_mtime_ns)
    if _CONFIG_CACHE.get("key") != key:
        _CONFIG_CACHE.update(key=key, value=json.loads(path.read_text(encoding="utf-8")))
    return copy.deepcopy(_CONFIG_CACHE["value"])


def scoring() -> dict:
    """Searched roles, skill weights and technologies from config.json → scoring, normalized."""
    s = config().get("scoring", {})

    def terms(key: str, default) -> tuple[str, ...]:
        return tuple(normalize(t) for t in s.get(key, default))
    return {"role_terms": terms("role_terms", ROLE_TERMS), "main_titles": terms("main_titles", MAIN_TITLES),
            "senior_terms": terms("senior_terms", SENIOR_TERMS), "technologies": terms("technologies", TECH),
            "skills": {normalize(k): v for k, v in s.get("skills", SKILLS).items()},
            "bonus": {normalize(k): v for k, v in s.get("bonus", BONUS).items()}}


def home() -> dict:
    """Where the candidate lives (config.json → home): country name, places, region and blocked locations."""
    h = {**HOME, **config().get("home", {})}
    for key in ("places", "region_terms", "blocked_locations"):
        h[key] = [normalize(x) for x in h[key]]
    h["places"] = list(dict.fromkeys([normalize(h["country"]), *h["places"]]))
    return h


def at_home(location: str) -> bool:
    """Whether a normalized location is in the candidate's country."""
    h = home()
    code = re.escape(normalize(h["code"]))
    return any(place in location for place in h["places"]) or bool(code and re.search(rf"\b{code}\b", location))


def _local_tz():
    try:
        return ZoneInfo(config().get("home", {}).get("timezone") or HOME["timezone"])
    except Exception:
        return datetime.now().astimezone().tzinfo


LOCAL_TZ = _local_tz()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in value if not unicodedata.combining(c)).lower().strip()


@contextmanager
def connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL,
            company TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            remote TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            contact_email TEXT NOT NULL DEFAULT '',
            score INTEGER NOT NULL DEFAULT 0,
            reasons TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',
            posted_at TEXT NOT NULL DEFAULT '',
            seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source, external_id)
        );
        CREATE TABLE IF NOT EXISTS applications (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            draft_id TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            added INTEGER NOT NULL,
            updated INTEGER NOT NULL,
            errors TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_rank ON jobs(status, score DESC, seen_at DESC);
        CREATE TABLE IF NOT EXISTS messages (
            gmail_id TEXT PRIMARY KEY,
            job_id INTEGER REFERENCES jobs(id),
            thread_id TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL,
            sender TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            at TEXT NOT NULL,
            job_id INTEGER,
            kind TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT ''
        );
    """)
    if "linkedin_apply" not in {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}:
        # '' = not checked yet, easy = Solicitud sencilla (user), external = sent to the form agent, closed, unknown
        db.execute("ALTER TABLE jobs ADD COLUMN linkedin_apply TEXT NOT NULL DEFAULT ''")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(applications)")}
    for name, ddl in (("thread_id", "TEXT NOT NULL DEFAULT ''"), ("sent_at", "TEXT NOT NULL DEFAULT ''"),
                      ("cv_file", "TEXT NOT NULL DEFAULT ''"), ("language", "TEXT NOT NULL DEFAULT ''"),
                      ("last_inbound_at", "TEXT NOT NULL DEFAULT ''"), ("last_outbound_at", "TEXT NOT NULL DEFAULT ''"),
                      ("followup_at", "TEXT NOT NULL DEFAULT ''"), ("interview_at", "TEXT NOT NULL DEFAULT ''"),
                      ("interview_link", "TEXT NOT NULL DEFAULT ''"), ("note", "TEXT NOT NULL DEFAULT ''"),
                      ("channel", "TEXT NOT NULL DEFAULT 'email'"), ("cv_path", "TEXT NOT NULL DEFAULT ''"),
                      ("added_skills", "TEXT NOT NULL DEFAULT ''"), ("user_input", "TEXT NOT NULL DEFAULT ''"),
                      ("login_url", "TEXT NOT NULL DEFAULT ''"), ("login_google", "INTEGER NOT NULL DEFAULT 0")):
        if name not in columns:
            db.execute(f"ALTER TABLE applications ADD COLUMN {name} {ddl}")
    for old, new in LEGACY_STATUS.items():
        db.execute("UPDATE jobs SET status=? WHERE status=?", (new, old))
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def target_role(job: dict) -> bool:
    """Whether the title is one of the searched roles. This filter is independent of the (display-only) score."""
    return any(term in normalize(job.get("title", "")) for term in scoring()["role_terms"])


def job_score(job: dict) -> tuple[int, str]:
    """Display-only match score: it never decides what is collected, applied to or skipped."""
    title = normalize(job.get("title", ""))
    body = normalize(job.get("description", ""))
    location = normalize(job.get("location", ""))
    sc = scoring()
    if not any(term in title for term in sc["role_terms"]):
        return 0, "Cargo fuera de los objetivos"
    score = 42
    reasons = []
    if any(term in title for term in sc["main_titles"]):
        score += 13
    if any(term in title for term in sc["senior_terms"]):
        score -= 36
        reasons.append("Pide nivel senior o liderazgo")
    matched = [term for term in sc["skills"] if term in body]
    score += min(sum(sc["skills"][s] for s in matched), 32)
    if matched:
        reasons.append("Coincide: " + ", ".join(matched[:4]))
    else:
        reasons.append("Falta descripción para evaluar tecnologías")
    future = [term for term in sc["bonus"] if term in body]
    if future:
        score += min(sum(sc["bonus"][s] for s in future), 7)
        reasons.append("Interés: " + ", ".join(future[:2]))
    if at_home(location):
        score += 5
    elif "remote" in location or "remoto" in location or normalize(job.get("remote", "")) == "remote":
        score += 3
    elif location and not any(x in location for x in (*home()["region_terms"], "worldwide")):
        score -= 12
        reasons.append("Revisar ubicación o elegibilidad")
    if any(x in body for x in ("5+ years", "5 years", "5 anos", "5 anios", "5 años")):
        score -= 17
        reasons.append("Puede pedir más experiencia")
    return max(0, min(100, score)), "; ".join(reasons)


def contact_email(text: str) -> str:
    """Return the first plausible contact address. The agent's LLM later confirms it is for applications."""
    for match in EMAIL_RE.finditer(text or ""):
        address = match.group(1).lower().rstrip(".")
        context = normalize((text or "")[max(0, match.start() - 120):match.start()])
        if any(x in context for x in ("accommodation", "accessibility", "privacy", "data protection",
                                      "equal opportunity", "privacidad", "datos personales")):
            continue
        if any(x in address for x in ("noreply", "no-reply", "donotreply", "example.", "linkedin.com",
                                      "privacy", "accommodation", "sentry", "wixpress")):
            continue
        return address
    return ""


def upsert(db: sqlite3.Connection, job: dict) -> bool:
    key = (job["source"], str(job["external_id"]))
    existing = db.execute("SELECT id, description, contact_email FROM jobs WHERE source=? AND external_id=?", key).fetchone()
    if existing and not job.get("description"):
        # Alerts carry no description; keep the one fetched by enrich().
        job["description"] = existing["description"]
        job["contact_email"] = job.get("contact_email") or existing["contact_email"]
    score, reasons = job_score(job)
    job["score"], job["reasons"] = score, reasons
    job["contact_email"] = job.get("contact_email") or contact_email(job.get("description", ""))
    fields = (job["title"], job.get("company", ""), job.get("location", ""), job.get("remote", ""),
              job["url"], job.get("description", ""), job["contact_email"], score, reasons,
              job.get("posted_at", ""), now())
    if existing:
        db.execute("""UPDATE jobs SET title=?, company=?, location=?, remote=?, url=?, description=?,
                   contact_email=?, score=?, reasons=?, posted_at=?, updated_at=? WHERE id=?""",
                   (*fields, existing["id"]))
        return False
    db.execute("""INSERT INTO jobs(source, external_id, title, company, location, remote, url,
               description, contact_email, score, reasons, posted_at, seen_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (*key, *fields[:-1], now(), fields[-1]))
    return True


def clean_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def eligible_location(job: dict) -> bool:
    if job.get("source") in ("indeed", "computrabajo", "bumeran", "startupsargentina", "linkedin_alert"):
        # Local sites, or LinkedIn alerts whose location the user chose (e.g. USA/Norway/Netherlands remote).
        return True
    location = normalize(job.get("location", ""))
    h = home()
    if job.get("source") in ("himalayas", "remotive", "jobicy"):
        # Remote boards state explicit country restrictions; accept only ones that include the home country.
        return at_home(location) or any(x in location for x in (*h["region_terms"], "americas", "worldwide",
                                                                 "anywhere")) or location == ""
    if at_home(location) or any(x in location for x in (*h["region_terms"], "worldwide", "anywhere")):
        return True
    if any(x in location for x in h["blocked_locations"]):
        return False
    return "remote" in location or "remoto" in location or normalize(job.get("remote", "")) == "remote"


def fetch_board(board: dict, session: requests.Session) -> list[dict]:
    provider, token = board["provider"], board["token"]
    if provider == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        response = session.get(url, timeout=25)
        response.raise_for_status()
        entries = response.json()["jobs"]
        return [{"source": "greenhouse", "external_id": f"{token}:{x['id']}",
                 "title": x["title"], "company": board["name"], "location": x.get("location", {}).get("name", ""),
                 "url": x["absolute_url"], "description": clean_html(x.get("content", "")),
                 "posted_at": x.get("updated_at", "")} for x in entries]
    if provider == "lever":
        url = f"https://api.lever.co/v0/postings/{token}?mode=json"
        response = session.get(url, timeout=25)
        response.raise_for_status()
        entries = response.json()
        return [{"source": "lever", "external_id": f"{token}:{x['id']}",
                 "title": x["text"], "company": board["name"],
                 "location": x.get("categories", {}).get("location", ""),
                 "remote": x.get("workplaceType", ""), "url": x["hostedUrl"],
                 "description": x.get("descriptionPlain") or clean_html(x.get("description", "")),
                 "posted_at": ""} for x in entries]
    raise ValueError(f"Proveedor desconocido: {provider}")


def fetch_getonbrd(cfg: dict, session: requests.Session) -> list[dict]:
    found = []
    for term in cfg["search_terms"]:
        response = session.get("https://www.getonbrd.com/api/v0/search/jobs",
                               params={"query": term, "per_page": 50, "expand": '["company"]'}, timeout=25)
        response.raise_for_status()
        for x in response.json()["data"]:
            at = x["attributes"]
            company = ((at.get("company") or {}).get("data") or {}).get("attributes", {}).get("name", "")
            countries = ", ".join(at.get("countries") or [])
            text = " ".join(clean_html(at.get(k) or "") for k in ("description", "functions", "desirable"))
            found.append({"source": "getonbrd", "external_id": x["id"], "title": at["title"],
                          "company": company, "location": countries,
                          "remote": "remote" if at.get("remote") else "",
                          "url": x["links"]["public_url"], "description": text,
                          "posted_at": datetime.fromtimestamp(at["published_at"], timezone.utc).isoformat()
                          if at.get("published_at") else ""})
    return found


def browser_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "es-AR,es;q=0.9,en;q=0.8"})
    return session


def fetch_computrabajo(cfg: dict) -> list[dict]:
    """Computrabajo Argentina: search pages plus each offer's detail page for the description."""
    import time
    session, found, seen = browser_session(), [], set()
    limit = int(cfg.get("computrabajo", {}).get("details_per_run", 40))
    for term in cfg["search_terms"]:
        slug = re.sub(r"[^a-z0-9]+", "-", normalize(term)).strip("-")
        response = session.get(f"https://ar.computrabajo.com/trabajo-de-{slug}", timeout=25)
        response.raise_for_status()
        for article in BeautifulSoup(response.text, "html.parser").select("article.box_offer"):
            link = article.select_one("h2 a")
            offer_id = article.get("data-id") or ""
            if not link or not offer_id or offer_id in seen:
                continue
            seen.add(offer_id)
            info = [p.get_text(" ", strip=True) for p in article.select("p")]
            company = re.sub(r"^\d+,\d\s+", "", info[0]) if info else ""  # drop the rating prefix ("4,3 Empresa")
            found.append({"source": "computrabajo", "external_id": offer_id, "title": link.get_text(strip=True),
                          "company": company, "location": info[1] if len(info) > 1 else "",
                          "url": "https://ar.computrabajo.com" + link["href"].split("#")[0],
                          "description": "", "posted_at": ""})
        time.sleep(1)
    with connect() as db:
        known = {r["external_id"]: r["description"] for r in db.execute(
            "SELECT external_id, description FROM jobs WHERE source='computrabajo'")}
    fetched = 0
    for job in found:
        if known.get(job["external_id"]):
            job["description"] = known[job["external_id"]]
            continue
        if fetched >= limit or not target_role(job):
            continue
        page = BeautifulSoup(session.get(job["url"], timeout=25).text, "html.parser")
        node = page.select_one('div[div-link="oferta"]') or page.select_one("main")
        job["description"] = node.get_text("\n", strip=True) if node else ""
        if "remoto" in normalize(job["description"][:400]):
            job["remote"] = "remote"
        fetched += 1
        time.sleep(1)
    return found


def fetch_bumeran(cfg: dict) -> list[dict]:
    """Bumeran Argentina (same listings as ZonaJobs) through the site's own search API."""
    session, found = browser_session(), []
    for term in cfg["search_terms"]:
        response = session.post("https://www.bumeran.com.ar/api/avisos/searchV2?pageSize=50&page=0&sort=RECIENTES",
                                json={"filtros": [], "query": term},
                                headers={"x-site-id": "BMAR", "Referer": "https://www.bumeran.com.ar/"}, timeout=25)
        response.raise_for_status()
        for x in response.json().get("content", []):
            posted = ""
            try:
                posted = datetime.strptime(x["fechaHoraPublicacion"], "%d-%m-%Y %H:%M:%S").replace(tzinfo=BUMERAN_TZ).isoformat()
            except (KeyError, TypeError, ValueError):
                pass
            found.append({"source": "bumeran", "external_id": str(x["id"]), "title": x["titulo"],
                          "company": "" if x.get("confidencial") else (x.get("empresa") or ""),
                          "location": x.get("localizacion") or "Argentina",
                          "remote": "remote" if normalize(x.get("modalidadTrabajo") or "") == "remoto" else
                                    (x.get("modalidadTrabajo") or ""),
                          "url": f"https://www.bumeran.com.ar/empleos/{x['id']}.html",
                          "description": clean_html(x.get("detalle") or ""), "posted_at": posted})
    return found


STARTUPS_AR = "https://www.startupsargentina.com"
SPANISH_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
                  "octubre", "noviembre", "diciembre")


def parse_startupsargentina_list(html: str) -> tuple[list[dict], int]:
    """Cards of one /empleos page plus the total page count ("Página 1 de 18")."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for card in soup.select("article"):
        link = card.select_one('h2 a[href^="/empleos/"]')
        if not link:
            continue
        parts = link["href"].strip("/").split("/")  # empleos/<startup>/<uuid>/<slug>
        if len(parts) < 3:
            continue
        company = card.select_one('p a[href^="/startups/"]')
        place = card.find("span", recursive=False)
        where, _, mode = (place.get_text(" ", strip=True) if place else "").partition(" · ")
        found.append({"source": "startupsargentina", "external_id": parts[2], "title": link.get_text(" ", strip=True),
                      "company": company.get_text(" ", strip=True) if company else parts[1],
                      "location": where.strip(), "remote": "remote" if normalize(mode) == "remote" else mode.strip(),
                      "url": STARTUPS_AR + link["href"], "description": "", "posted_at": ""})
    pages = re.search(r"P\w+gina\s+\d+\s+de\s+(\d+)", soup.get_text(" ", strip=True))
    return found, int(pages.group(1)) if pages else 1


def parse_startupsargentina_detail(html: str) -> dict:
    """Description, facts, publication date and the employer's apply link (usually its ATS) of one offer."""
    soup = BeautifulSoup(html, "html.parser")
    facts = {normalize(dt.get_text(" ", strip=True)): dd.get_text(" ", strip=True)
             for dt, dd in zip(soup.select("dl.job-detail-facts dt"), soup.select("dl.job-detail-facts dd"))}
    apply = soup.select_one("a.job-detail-apply__button[href]")
    apply_href = apply["href"] if apply else ""  # read before the header (which holds the button) is dropped
    copy_node = soup.select_one("article.job-detail-copy")
    description = ""
    if copy_node and not copy_node.select_one(".job-detail-empty"):
        for header in copy_node.select(".job-detail-copy__header"):
            header.decompose()
        description = copy_node.get_text("\n", strip=True)
    posted = ""
    date = re.match(r"(\d{1,2}) de (\w+) de (\d{4})", normalize(facts.get("publicada", "")))
    if date and date.group(2) in SPANISH_MONTHS:
        posted = datetime(int(date.group(3)), SPANISH_MONTHS.index(date.group(2)) + 1, int(date.group(1)),
                          tzinfo=BUMERAN_TZ).isoformat()
    apply_url = ""
    if apply_href:
        parts = urlparse(apply_href)
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.startswith("utm_")])
        apply_url = parts._replace(query=query).geturl()
    extra = " · ".join(f"{k.capitalize()}: {v}" for k, v in facts.items() if k in ("tipo", "modalidad"))
    return {"description": (description + ("\n\n" + extra if extra else "")).strip(), "posted_at": posted,
            "apply_url": apply_url}


def fetch_startupsargentina(cfg: dict) -> list[dict]:
    """Startups Argentina (startupsargentina.com/empleos): server-rendered list pages plus each matching offer's page."""
    import time
    conf = cfg.get("startupsargentina", {})
    session, found, seen = browser_session(), [], set()
    page, pages = 1, 1
    while page <= min(pages, int(conf.get("max_pages", 25))):
        response = session.get(f"{STARTUPS_AR}/empleos", params={"page": page} if page > 1 else None, timeout=25)
        response.raise_for_status()
        cards, pages = parse_startupsargentina_list(response.text)
        if not cards:
            break
        for job in cards:
            if job["external_id"] not in seen:
                seen.add(job["external_id"])
                found.append(job)
        page += 1
        time.sleep(1)
    with connect() as db:
        known = {r["external_id"]: dict(r) for r in db.execute(
            "SELECT external_id, url, description, posted_at FROM jobs WHERE source='startupsargentina'")}
        # Offers already stored from another platform are dropped by collect(); don't spend detail requests on them.
        repeated = {job["external_id"] for job in found if job["external_id"] not in known and duplicate(db, job)}
    limit, fetched = int(conf.get("details_per_run", 30)), 0
    for job in found:
        row = known.get(job["external_id"])
        if row and not row["url"].startswith(STARTUPS_AR):
            # Detail already read: keep it. Offers stored with the site's own URL got no detail yet (per-run cap).
            job.update(url=row["url"], description=row["description"], posted_at=row["posted_at"])
            continue
        if fetched >= limit or job["external_id"] in repeated or not target_role(job):
            continue
        response = session.get(job["url"], timeout=25)
        response.raise_for_status()
        detail = parse_startupsargentina_detail(response.text)
        job.update(description=detail["description"], posted_at=detail["posted_at"])
        if detail["apply_url"]:
            job["url"] = detail["apply_url"]  # apply straight on the employer's form
        fetched += 1
        time.sleep(1)
    return found


def fetch_remote_boards(cfg: dict) -> list[dict]:
    """Remote job boards with public APIs: Himalayas, Remotive and Jobicy. Location filtering happens in collect()."""
    session, found, errors = browser_session(), [], []
    for term in cfg["search_terms"]:
        try:
            data = session.get("https://himalayas.app/jobs/api/search", params={"q": term}, timeout=25).json()
            for x in data.get("jobs", []):
                where = ", ".join(x.get("locationRestrictions") or []) or "Worldwide"
                found.append({"source": "himalayas", "external_id": x["guid"], "title": x["title"],
                              "company": x.get("companyName", ""), "location": where, "remote": "remote",
                              "url": x.get("applicationLink") or x["guid"], "description": clean_html(x.get("description", "")),
                              "posted_at": datetime.fromtimestamp(int(x["pubDate"]), timezone.utc).isoformat() if x.get("pubDate") else ""})
        except Exception as exc:
            errors.append(f"Himalayas: {exc}")
        try:
            data = session.get("https://remotive.com/api/remote-jobs", params={"search": term, "limit": 50}, timeout=25).json()
            for x in data.get("jobs", []):
                found.append({"source": "remotive", "external_id": str(x["id"]), "title": x["title"],
                              "company": x.get("company_name", ""), "location": x.get("candidate_required_location") or "Worldwide",
                              "remote": "remote", "url": x["url"], "description": clean_html(x.get("description", "")),
                              "posted_at": x.get("publication_date", "")})
        except Exception as exc:
            errors.append(f"Remotive: {exc}")
    try:
        data = session.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": cfg["search_terms"][0]}, timeout=25).json()
        for x in data.get("jobs", []):
            found.append({"source": "jobicy", "external_id": str(x["id"]), "title": x["jobTitle"],
                          "company": x.get("companyName", ""), "location": x.get("jobGeo") or "Worldwide",
                          "remote": "remote", "url": x["url"], "description": clean_html(x.get("jobDescription", "")),
                          "posted_at": x.get("pubDate", "")})
    except Exception as exc:
        errors.append(f"Jobicy: {exc}")
    if errors and not found:
        raise RuntimeError("; ".join(errors))
    return found


def duplicate(db: sqlite3.Connection, job: dict) -> bool:
    """Same title and company already stored (another platform, or the same one under another ID)."""
    company = normalize(job.get("company", ""))
    if len(company) < 3 or "confidencial" in company:
        return False
    title = normalize(job["title"])
    for row in db.execute("""SELECT title FROM jobs WHERE lower(company)=lower(?)
                             AND NOT (source=? AND external_id=?)""",
                          (job.get("company", ""), job["source"], str(job["external_id"]))):
        if normalize(row["title"]) == title:
            return True
    return False


def linkedin_description(job_id: str, session: requests.Session) -> str:
    """Public guest view of a LinkedIn job; no login or cookies involved."""
    response = session.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}",
                           headers={"User-Agent": BROWSER_UA}, timeout=25)
    if response.status_code == 404:
        return ""
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    node = soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
    criteria = " · ".join(x.get_text(" ", strip=True) for x in soup.select(".description__job-criteria-item"))
    return ((node.get_text("\n", strip=True) if node else "") + ("\n\n" + criteria if criteria else "")).strip()


def enrich(db: sqlite3.Connection, limit: int, errors: list) -> int:
    """Fetch full descriptions for LinkedIn alerts (shown in the board and used to personalise applications)."""
    import time
    session = requests.Session()
    rows = db.execute("""SELECT * FROM jobs WHERE source='linkedin_alert' AND description=''
                         AND status='new' ORDER BY seen_at DESC LIMIT ?""", (limit,)).fetchall()
    done = 0
    for row in rows:
        try:
            text = linkedin_description(row["external_id"], session)
        except Exception as exc:
            errors.append(f"LinkedIn descripción {row['external_id']}: {exc}")
            if "429" in str(exc):
                break
            continue
        job = {**dict(row), "description": text or "(aviso cerrado o sin descripción pública)"}
        upsert(db, job)
        if not text:
            db.execute("UPDATE jobs SET status='skipped' WHERE id=?", (row["id"],))
            log_event(db, row["id"], "status", "Aviso de LinkedIn cerrado")
        done += 1
        time.sleep(1.5)
    return done


def fetch_indeed(cfg: dict) -> list[dict]:
    from jobspy import scrape_jobs

    found = []
    def cell(value) -> str:
        # JobSpy returns pandas NaN for absent fields.
        return "" if value is None or str(value).lower() in {"nan", "none", "nat"} else str(value)
    for term in cfg["search_terms"]:
        frame = scrape_jobs(site_name=["indeed"], search_term=term,
                            location=cfg["indeed"]["location"],
                            country_indeed=cfg["indeed"]["country"],
                            results_wanted=cfg["indeed"]["results_per_query"], verbose=0)
        for x in frame.to_dict("records"):
            url = cell(x.get("job_url"))
            if not url or url == "nan":
                continue
            found.append({"source": "indeed", "external_id": cell(x.get("id")) or url,
                          "title": cell(x.get("title")), "company": cell(x.get("company")),
                          "location": cell(x.get("location")),
                          "remote": "remote" if x.get("is_remote") is True else "",
                          "url": url, "description": cell(x.get("description")),
                          "posted_at": cell(x.get("date_posted"))})
    return found


def parse_linkedin_email(plain: str, sent_at: str = "") -> list[dict]:
    """Parse LinkedIn's plain-text job-alert digest, including /comm/jobs/view URLs."""
    jobs = []
    for segment in re.split(r"-{20,}", plain):
        match = JOB_URL_RE.search(segment)
        if not match:
            continue
        before = segment[:match.start()]
        lines = [s.strip() for s in before.splitlines() if s.strip()]
        while lines and (lines[0].lower().startswith("tu alerta de empleo") or
                         lines[0].lower().startswith("your job alert")):
            lines.pop(0)
        if len(lines) < 3:
            continue
        # Alert cards start with title, company and location. Extra social proof
        # and application hints occur after those lines, before the URL.
        title, company, location = lines[:3]
        if not any(term in normalize(title) for term in scoring()["role_terms"]):
            continue
        jobs.append({"source": "linkedin_alert", "external_id": match.group(1),
                     "title": title, "company": company, "location": location,
                     "url": f"https://www.linkedin.com/jobs/view/{match.group(1)}/",
                     "description": "", "posted_at": sent_at})
    return jobs


GMAIL_TOKENS = {"main": "token_main.json", "alerts": "token.json"}


def gmail_can_modify(role: str = "main") -> bool:
    """Whether the stored token for that inbox already has the gmail.modify permission."""
    path = SECRETS / GMAIL_TOKENS[role]
    return path.exists() and "gmail.modify" in path.read_text(encoding="utf-8")


def gmail_account(role: str) -> str:
    """Expected address for each Gmail role: 'main' sends/receives applications, 'alerts' gets LinkedIn alerts and notices."""
    cfg = config()
    accounts = cfg.get("gmail_accounts", {})
    return accounts.get(role) or cfg["candidate_email"]


class GmailExpired(RuntimeError):
    """Google revoked the stored token (in Testing mode it lasts 7 days); the user must authorize again."""


def gmail_service(interactive: bool = False, role: str = "main"):
    """Return an authorized Gmail API service for a role, or None until OAuth is configured for it.
    Raises GmailExpired when the stored token can no longer be refreshed."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    credentials_path = SECRETS / "credentials.json"
    token_path = SECRETS / GMAIL_TOKENS[role]
    if not credentials_path.exists():
        return None
    # The main inbox also needs gmail.modify (labels/archive for mail_organizer); older tokens keep their own scopes.
    wanted = SCOPES + (["https://www.googleapis.com/auth/gmail.modify"] if role == "main" else [])
    creds = Credentials.from_authorized_user_file(str(token_path)) if token_path.exists() else None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            if not interactive:
                raise GmailExpired(f"Venció la autorización de Gmail de {gmail_account(role)}. "
                                   "Volvé a autorizarla en Configuración → Gmail.") from None
            creds = None
        except Exception:
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            return None
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), wanted)
        expected = gmail_account(role)
        creds = flow.run_local_server(port=0, prompt="consent", login_hint=expected)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        actual = service.users().getProfile(userId="me").execute()["emailAddress"].lower()
        if actual != expected.lower():
            raise ValueError(f"Autorizaste {actual}, pero esta conexión es para {expected}. Repetilo eligiendo esa cuenta.")
    SECRETS.mkdir(exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def fetch_linkedin_alerts(service, cfg: dict) -> list[dict]:
    results = []
    page_token = None
    remaining = int(cfg["gmail"].get("max_messages", 100))
    while remaining > 0:
        args = {"userId": "me", "q": cfg["gmail"]["query"], "maxResults": min(100, remaining)}
        if page_token:
            args["pageToken"] = page_token
        response = service.users().messages().list(**args).execute()
        items = response.get("messages", [])
        for item in items:
            message = service.users().messages().get(userId="me", id=item["id"], format="raw").execute()
            raw = base64.urlsafe_b64decode(message["raw"])
            email = message_from_bytes(raw)
            if "jobalerts-noreply@linkedin.com" not in str(email.get("From", "")):
                continue
            plain = ""
            for part in email.walk():
                if part.get_content_type() == "text/plain":
                    plain += part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            if not plain:
                for part in email.walk():
                    if part.get_content_type() == "text/html":
                        html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                        plain = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
                        break
            sent_at = datetime.fromtimestamp(int(message["internalDate"]) / 1000, timezone.utc).isoformat()
            results.extend(parse_linkedin_email(plain, sent_at))
        remaining -= len(items)
        page_token = response.get("nextPageToken")
        if not items or not page_token:
            break
    return results


def collect() -> dict:
    cfg = config()
    started = now()
    jobs, errors = [], []
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    for board in cfg.get("boards", []):
        try:
            jobs.extend(fetch_board(board, session))
        except Exception as exc:
            errors.append(f"{board['provider']}:{board['token']}: {exc}")
    if cfg.get("indeed", {}).get("enabled"):
        try:
            jobs.extend(fetch_indeed(cfg))
        except Exception as exc:
            errors.append(f"Indeed: {exc}")
    if cfg.get("getonbrd", {}).get("enabled"):
        try:
            jobs.extend(fetch_getonbrd(cfg, session))
        except Exception as exc:
            errors.append(f"Get on Board: {exc}")
    for key, name, fetch in (("computrabajo", "Computrabajo", fetch_computrabajo), ("bumeran", "Bumeran", fetch_bumeran),
                             ("startupsargentina", "Startups Argentina", fetch_startupsargentina),
                             ("remote_boards", "Remotos", fetch_remote_boards)):
        if cfg.get(key, {}).get("enabled"):
            try:
                jobs.extend(fetch(cfg))
            except Exception as exc:
                errors.append(f"{name}: {exc}")
    if cfg.get("gmail", {}).get("enabled"):
        try:
            service = gmail_service(role="alerts")
            if service:
                jobs.extend(fetch_linkedin_alerts(service, cfg))
            else:
                errors.append("LinkedIn: falta conectar Gmail en la pestaña Configuración")
        except Exception as exc:
            errors.append(f"LinkedIn: {exc}")
    added = updated = 0
    with connect() as db:
        for job in jobs:
            try:
                if not target_role(job) or not eligible_location(job):
                    continue
                exists = db.execute("SELECT 1 FROM jobs WHERE source=? AND external_id=?",
                                    (job["source"], str(job["external_id"]))).fetchone()
                if not exists and duplicate(db, job):
                    continue
                if upsert(db, job):
                    added += 1
                else:
                    updated += 1
            except Exception as exc:
                errors.append(f"Oferta {job.get('url', '')}: {exc}")
        enriched = enrich(db, int(cfg.get("enrich_per_run", 60)), errors)
        if enriched:
            log_event(db, None, "enrich", f"{enriched} descripciones de LinkedIn descargadas")
        db.execute("INSERT INTO runs(started_at,finished_at,added,updated,errors) VALUES(?,?,?,?,?)",
                   (started, now(), added, updated, json.dumps(errors, ensure_ascii=False)))
    return {"added": added, "updated": updated, "errors": errors}


def jobs(status: str = "all") -> list[dict]:
    """Jobs joined with their application (if any). `status` may be a status, 'pipeline' or 'all'."""
    query = """SELECT j.*, a.recipient, a.subject AS app_subject, a.sent_at, a.thread_id, a.cv_file,
                      a.last_inbound_at, a.last_outbound_at, a.followup_at, a.interview_at,
                      a.interview_link, a.note, a.channel, a.cv_path, a.added_skills, a.status AS app_status, a.user_input, a.login_url, a.login_google
               FROM jobs j LEFT JOIN applications a ON a.job_id = j.id WHERE 1=1"""
    args: list = []
    if status == "pipeline":
        query += f" AND j.status IN ({','.join('?' * len(PIPELINE))})"
        args.extend(PIPELINE)
    elif status != "all":
        query += " AND j.status = ?"
        args.append(status)
    query += " ORDER BY COALESCE(NULLIF(j.posted_at, ''), j.seen_at) DESC"
    with connect() as db:
        return [dict(row) for row in db.execute(query, args)]


def job(job_id: int) -> dict:
    found = [x for x in jobs("all") if x["id"] == job_id]
    if not found:
        raise ValueError(f"Oferta {job_id} inexistente")
    return found[0]


def log_event(db: sqlite3.Connection, job_id: int | None, kind: str, detail: str = "") -> None:
    db.execute("INSERT INTO events(at, job_id, kind, detail) VALUES(?,?,?,?)", (now(), job_id, kind, detail))


def events(limit: int = 200) -> list[dict]:
    with connect() as db:
        return [dict(r) for r in db.execute(
            """SELECT e.*, j.title, j.company FROM events e LEFT JOIN jobs j ON j.id=e.job_id
               ORDER BY e.id DESC LIMIT ?""", (limit,))]


# Why an offer left the queue, grouped from the free-text reason of its last status change.
# First match wins, so hard blockers (closed, permit, location, language) beat softer ones (seniority, cloud, stack).
EXIT_REASONS = (
    ("Link externo", r"se postula por el link externo"),
    ("Aviso cerrado", r"ya no acepta|ya no esta disponible|no longer open|no existe"),
    ("Duplicada", r"duplicad"),
    ("Permiso de trabajo", r"permiso|elegibilidad|visa|sponsor|autorizado a trabajar"),
    ("Ubicación / residencia", r"resid|wohnsitz|no coincide con la ubicacion|ubicado en|presencial en|solo para"),
    ("Idioma", r"polaco|noruego|sueco|aleman|frances|portugues|idioma|fluid"),
    ("Seniority / años", r"palabra en el cargo|excluida por experiencia|\d+\s*\+?\s*anos|anos de experiencia|senior|seniority|nivel l\d"),
    ("Nube no Microsoft", r"excluida por nube|\baws\b|\bgcp\b|google cloud|bigquery|redshift|nube"),
    ("Stack no coincide", r"no figura|no estan en el cv|no tiene|java"),
)


def exit_reason(status: str, detail: str) -> str:
    """Category of why a skipped/discarded/rejected offer is out: seniority, cloud, permit, location, etc."""
    if status == "rejected":
        return "Rechazada por la empresa"
    if status == "discarded":
        return "Descartada por vos"
    text = normalize(detail)
    for label, pattern in EXIT_REASONS:
        if re.search(pattern, text):
            return label
    return "Otro motivo"


def exit_reasons() -> dict[int, tuple[str, str]]:
    """{job_id: (category, detail)} for every skipped, discarded or rejected offer.
    Offers handed over to their external link are left out: they were not discarded."""
    with connect() as db:
        rows = db.execute(
            """SELECT j.id, j.status, COALESCE(e.detail, '') AS detail FROM jobs j
               LEFT JOIN events e ON e.id = (SELECT MAX(id) FROM events WHERE job_id = j.id AND kind = 'status')
               WHERE j.status IN ('skipped', 'discarded', 'rejected')""").fetchall()
    out = {}
    for row in rows:
        detail = re.sub(r"^Movida a «[^»]+»:?\s*", "", row["detail"])
        category = exit_reason(row["status"], detail)
        if category != "Link externo":
            out[row["id"]] = (category, detail)
    return out


def thread(job_id: int) -> list[dict]:
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM messages WHERE job_id=? ORDER BY at", (job_id,))]


def parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def ago(value: str) -> str:
    """Human 'hace X' in Spanish, or '' when the timestamp is missing."""
    parsed = parse_time(value)
    if not parsed:
        return ""
    seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
    if seconds < 3600:
        return f"hace {max(1, int(seconds // 60))} min"
    if seconds < 86400:
        return f"hace {int(seconds // 3600)} h"
    days = int(seconds // 86400)
    return "hace 1 día" if days == 1 else f"hace {days} días"


def technologies(text: str) -> list[str]:
    body = " " + normalize(text) + " "
    return [t for t in scoring()["technologies"] if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", body)]


def modality(job: dict) -> str:
    text = normalize(" ".join(str(job.get(k) or "") for k in ("title", "location", "remote")))
    if "hybrid" in text or "hibrido" in text:
        return "Híbrido"
    if "remote" in text or "remoto" in text:
        return "Remoto"
    if any(term in text for term in ("on-site", "onsite", "in-office", "presencial")):
        return "Presencial"
    return "Sin especificar"


def job_timestamp(job: dict) -> float | None:
    value = job.get("posted_at") or job.get("seen_at")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError):
        return None


def sort_jobs(offers: list[dict], order: str) -> list[dict]:
    if order == "score":
        return sorted(offers, key=lambda job: (job["score"], job_timestamp(job) or 0), reverse=True)
    if order in {"date_new", "date_old"}:
        dated = [job for job in offers if job_timestamp(job) is not None]
        undated = [job for job in offers if job_timestamp(job) is None]
        return sorted(dated, key=job_timestamp, reverse=order == "date_new") + undated
    if order in {"remote_first", "onsite_first"}:
        ranking = (["Remoto", "Híbrido", "Presencial", "Sin especificar"] if order == "remote_first"
                   else ["Presencial", "Híbrido", "Remoto", "Sin especificar"])
        return sorted(offers, key=lambda job: ranking.index(modality(job)))
    if order in {"location_asc", "location_desc"}:
        known = [job for job in offers if job.get("location")]
        unknown = [job for job in offers if not job.get("location")]
        return sorted(known, key=lambda job: normalize(job["location"]),
                      reverse=order == "location_desc") + unknown
    if order in {"company_asc", "title_asc"}:
        field = "company" if order == "company_asc" else "title"
        known = [job for job in offers if job.get(field)]
        unknown = [job for job in offers if not job.get(field)]
        return sorted(known, key=lambda job: normalize(job[field])) + unknown
    raise ValueError(f"Orden desconocido: {order}")


def stats() -> dict:
    with connect() as db:
        counts = {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) n FROM jobs GROUP BY status")}
        run = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {"counts": counts, "run": dict(run) if run else None}


def set_status(job_id: int, status: str, note: str = "") -> None:
    if status not in STATUSES:
        raise ValueError("Estado inválido")
    with connect() as db:
        db.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (status, now(), job_id))
        if note:
            db.execute("UPDATE applications SET note=?, updated_at=? WHERE job_id=?", (note, now(), job_id))
        log_event(db, job_id, "status", f"Movida a «{STATUSES[status]}»" + (f": {note}" if note else ""))


def page_summary(url: str) -> dict:
    """Best-effort title and text of an application page (many are JS-rendered; the browser agent reads them anyway)."""
    try:
        response = requests.get(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "es-AR,es;q=0.9"}, timeout=25)
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception:
        return {"title": "", "company": "", "description": ""}
    meta = lambda key: (soup.find("meta", property=key) or soup.find("meta", attrs={"name": key}) or {}).get("content", "")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return {"title": (meta("og:title") or (soup.title.string if soup.title else "") or "").strip(),
            "company": meta("og:site_name").strip(),
            "description": soup.get_text("\n", strip=True)[:12000]}


def add_link(url: str, title: str = "", company: str = "", linked_job_id: int | None = None,
             source: str = "link") -> int:
    """Register an external application link (pasted by the user, or found behind a LinkedIn «Solicitar» button
    when source='linkedin_ext'). Returns the new job id."""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Pegá un link completo (https://...)")
    if "linkedin.com" in parsed.netloc.lower():
        raise ValueError("Ese link es de LinkedIn; acá va el link externo al que te manda el aviso.")
    with connect() as db:
        existing = db.execute("SELECT id FROM jobs WHERE source IN ('link','linkedin_ext') AND url=?", (url,)).fetchone()
        if existing:
            raise ValueError(f"Ese link ya está cargado (oferta #{existing['id']}).")
        base = dict(db.execute("SELECT * FROM jobs WHERE id=?", (linked_job_id,)).fetchone()) if linked_job_id else {}
    page = page_summary(url)
    description = base.get("description") or ""
    if page["description"]:
        description = (description + "\n\n--- Página de postulación ---\n" + page["description"]).strip()
    job = {"source": source, "external_id": url, "url": url,
           "title": title.strip() or base.get("title") or page["title"] or "Postulación por link",
           "company": company.strip() or base.get("company") or page["company"] or parsed.netloc.replace("www.", ""),
           "location": base.get("location", ""), "remote": base.get("remote", ""),
           "description": description or "(sin descripción: el agente la lee en la página)",
           "posted_at": base.get("posted_at", "")}
    with connect() as db:
        upsert(db, job)
        job_id = db.execute("SELECT id FROM jobs WHERE source=? AND external_id=?", (source, url)).fetchone()["id"]
        log_event(db, job_id, "link_added", ("Link externo de LinkedIn: " if source == "linkedin_ext" else "Link agregado: ") + url)
        if linked_job_id:
            db.execute("UPDATE jobs SET status='skipped', updated_at=? WHERE id=?", (now(), linked_job_id))
            log_event(db, linked_job_id, "status", f"Se postula por el link externo (oferta #{job_id})")
    return job_id


COUNTRY_NAMES = {
    "united states": "Estados Unidos", "usa": "Estados Unidos", "us": "Estados Unidos", "estados unidos": "Estados Unidos",
    "canada": "Canadá", "mexico": "México", "brazil": "Brasil", "brasil": "Brasil", "chile": "Chile",
    "colombia": "Colombia", "peru": "Perú", "uruguay": "Uruguay", "spain": "España", "espana": "España",
    "netherlands": "Países Bajos", "paises bajos": "Países Bajos", "norway": "Noruega", "noruega": "Noruega",
    "germany": "Alemania", "alemania": "Alemania", "united kingdom": "Reino Unido", "reino unido": "Reino Unido",
    "uk": "Reino Unido", "portugal": "Portugal", "ireland": "Irlanda", "irlanda": "Irlanda", "poland": "Polonia",
    "polonia": "Polonia", "finlandia": "Finlandia", "finland": "Finlandia", "suecia": "Suecia", "sweden": "Suecia",
    "belgica": "Bélgica", "belgium": "Bélgica", "france": "Francia", "francia": "Francia", "italy": "Italia",
    "italia": "Italia", "india": "India", "union europea": "Unión Europea", "european union": "Unión Europea",
}


def country(job: dict) -> str:
    """Country (in Spanish) from the free-text location; 'Remoto global', 'LATAM' or 'Sin dato' otherwise."""
    location = normalize(job.get("location", ""))
    if not location:
        return "Sin dato"
    if location.count(",") >= 4:
        return "LATAM (varios países)"
    h = home()
    if any(place in location for place in h["places"]) or re.search(rf",\s*{re.escape(normalize(h['code']))}$", location):
        return h["country"]
    if location in ("remote", "remoto", "worldwide", "anywhere") or "worldwide" in location:
        return "Remoto global"
    if any(x in location for x in ("latam", "latin america", "america latina", "south america")):
        return "LATAM (varios países)"
    last = location.split(",")[-1].strip()
    for key in (last, location):
        if key in COUNTRY_NAMES:
            return COUNTRY_NAMES[key]
    for key, name in COUNTRY_NAMES.items():
        if len(key) > 3 and key in location:
            return name
    return "Sin dato"


def valid_recipient(address: str) -> bool:
    return bool(EMAIL_RE.fullmatch(address.strip())) and "\n" not in address and "\r" not in address
