"""Local Streamlit dashboard for WorkWorm."""

from __future__ import annotations

import html
import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from pathlib import Path

import pandas as pd
import streamlit as st

import importlib

import agent
import llm
import radar


@st.cache_resource
def _module_mtimes() -> dict:
    return {}


def reload_changed_modules() -> None:
    """Streamlit re-runs app.py but keeps imported modules cached; reload the ones edited on disk."""
    import lifeprime
    import tailor
    seen = _module_mtimes()
    for module in (radar, llm, tailor, lifeprime, agent):  # dependency order
        mtime = Path(module.__file__).stat().st_mtime
        if seen.get(module.__name__, mtime) != mtime:
            importlib.reload(module)
        seen[module.__name__] = mtime


reload_changed_modules()
from radar import LOCAL_TZ, STATUSES  # noqa: E402


st.set_page_config(page_title="WorkWorm", page_icon="🪱", layout="wide")
st.markdown("""
<style>
  /* ---- WorkWorm: amber CRT, GRUB boot-menu style ---- */
  :root { --amber: #ffa526; --amber-hi: #ffe2b8; --amber-dim: #7a4a0e; --band: rgba(255, 140, 20, .32);
          --glow: 0 0 2px rgba(255,165,38,.7), 0 0 10px rgba(255,120,0,.3); }
  .stApp { background:
      radial-gradient(ellipse 60% 45% at 50% 30%, rgba(120, 60, 0, .55) 0%, rgba(60, 28, 0, .35) 45%, transparent 75%),
      radial-gradient(ellipse at center, #1e0e00 0%, #120800 55%, #060300 100%); }
  .block-container { max-width: 1280px; padding-top: 1.4rem; }
  .stApp, .stApp p, .stApp span, .stApp label, .stApp li, .stApp div[data-testid="stMarkdownContainer"] { text-shadow: var(--glow); }
  h1, h2, h3, h4 { text-transform: uppercase; letter-spacing: .08em; font-weight: 400 !important;
    text-shadow: 0 0 3px rgba(255,165,38,.9), 0 0 14px rgba(255,110,0,.45); }
  /* tube: scanlines + vignette + glare + faint flicker; never blocks clicks */
  .stApp::before { content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 999990;
    background: repeating-linear-gradient(to bottom, transparent 0 2px, rgba(0,0,0,.25) 3px, transparent 4px); }
  .stApp::after { content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 999991;
    background: radial-gradient(circle at 84% 14%, rgba(255,240,215,.10) 0, rgba(255,220,170,.04) 6%, transparent 14%),
                radial-gradient(ellipse at center, transparent 58%, rgba(0,0,0,.65) 100%);
    animation: crt-flicker 7s infinite steps(1); }
  @keyframes crt-flicker { 0%,100% {opacity: 1} 47% {opacity: .94} 48% {opacity: 1} }
  /* hero */
  .hero { text-align: center; padding: .4rem 0 .8rem 0; }
  .hero-brand { display: inline-flex; align-items: center; gap: 1rem; }
  .hero-title { font-family: "VT323", monospace; font-size: 3.6rem; letter-spacing: .14em; color: var(--amber);
    text-shadow: 0 0 4px rgba(255,165,38,.95), 0 0 18px rgba(255,110,0,.5); }
  .hero-sub { font-size: .85rem; letter-spacing: .3em; margin-top: .1rem; opacity: .85; }
  .hero-pred { font-size: .95rem; letter-spacing: .06em; text-transform: uppercase; }
  .hero-pred b { font-family: "VT323", monospace; font-size: 1.5rem; color: var(--amber-hi); }
  .worm { filter: drop-shadow(0 0 3px rgba(255,165,38,.9)) drop-shadow(0 0 10px rgba(255,110,0,.45));
    animation: wiggle 1.6s steps(2) infinite; }
  @keyframes wiggle { 50% { transform: translateY(-3px) skewX(-4deg); } }
  /* status bar */
  .st-key-statusbar { border-top: 1px solid var(--amber-dim); border-bottom: 1px solid var(--amber-dim);
    padding: .35rem .6rem; margin-bottom: .6rem; background: rgba(255,140,20,.05); }
  .status-line { font-size: .9rem; letter-spacing: .05em; text-transform: uppercase;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  /* tabs = GRUB menu entries */
  [role="tablist"] { justify-content: center; gap: .2rem !important; border-bottom: 1px dashed var(--amber-dim); }
  [data-testid="stTab"] { text-transform: uppercase; letter-spacing: .08em; border: none !important;
    padding: .25rem .9rem !important; background: transparent !important; }
  [data-testid="stTab"] p { font-size: .85rem !important; }
  [data-testid="stTab"]:hover { background: rgba(255,140,20,.12) !important; }
  [data-testid="stTab"][aria-selected="true"] { background: var(--band) !important; }
  [data-testid="stTab"][aria-selected="true"] p { color: var(--amber-hi) !important; font-weight: 600; }
  [data-baseweb="tab-highlight"], [data-baseweb="tab-border"], [data-testid="stTabsHighlight"] { display: none !important; }
  /* buttons: flat, selected-band look */
  .stButton button, .stDownloadButton button, .stLinkButton a, .stFormSubmitButton button, [data-testid="stPopover"] button {
    text-transform: uppercase; letter-spacing: .06em; border: 1px solid var(--amber-dim) !important; background: transparent !important; }
  .stButton button:hover, .stDownloadButton button:hover, .stLinkButton a:hover, .stFormSubmitButton button:hover,
  [data-testid="stPopover"] button:hover { background: var(--band) !important; border-color: var(--amber) !important; }
  button[kind="primary"], button[kind="primaryFormSubmit"] { background: var(--band) !important; border-color: var(--amber) !important; }
  button[kind="primary"] p, button[kind="primaryFormSubmit"] p { color: var(--amber-hi) !important; font-weight: 600; }
  button:disabled { opacity: .3 !important; }
  /* segmented control (pipeline stages) */
  [data-testid="stButtonGroup"] { justify-content: center; }
  [data-testid="stButtonGroup"] button { border-radius: 0 !important; border: none !important; background: transparent !important; }
  [data-testid="stButtonGroup"] button[aria-checked="true"], [data-testid="stButtonGroup"] button[kind*="Active"] { background: var(--band) !important; }
  /* pipeline rows: one line per application, band on hover */
  [class*="st-key-row-"] { border-bottom: 1px dotted rgba(122,74,14,.6); padding: .15rem .4rem; }
  [class*="st-key-row-"]:hover { background: rgba(255,140,20,.14); }
  .row-main { font-size: .95rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .row-time { font-size: .8rem; opacity: .8; text-align: right; }
  /* panels */
  [data-testid="stMetric"] { border: 1px solid var(--amber-dim); padding: 10px 14px; background: rgba(255,140,20,.04); }
  [data-testid="stMetricValue"] { font-family: "VT323", monospace; text-shadow: 0 0 4px rgba(255,165,38,.9), 0 0 16px rgba(255,110,0,.5); }
  [data-testid="stMetricLabel"] p { text-transform: uppercase; letter-spacing: .06em; font-size: .75rem; }
  [data-testid="stMetricLabel"] [data-testid="stMarkdownContainer"], [data-testid="stMetricLabel"] p {
    white-space: normal !important; overflow: visible !important; text-overflow: clip !important; line-height: 1.25; }
  [data-testid="stVerticalBlockBorderWrapper"] { border-color: var(--amber-dim) !important; background: rgba(255,140,20,.03); }
  [data-testid="stExpander"] details { border-color: var(--amber-dim) !important; }
  input, textarea { caret-color: var(--amber); }
  ::selection { background: var(--amber); color: #120800; }
  ::-webkit-scrollbar { width: 10px; height: 10px; } ::-webkit-scrollbar-track { background: #0c0600; }
  ::-webkit-scrollbar-thumb { background: var(--amber-dim); }
  .lane-title { font-family: "VT323", monospace; font-size: 1.35rem; text-transform: uppercase; letter-spacing: .04em; }
  .card-meta { font-size: .8rem; opacity: .85; line-height: 1.5; }
  .late { color: #ff6a2a !important; border-color: #ff6a2a !important; font-weight: 600; }
  .tag { display: inline-block; border: 1px solid var(--amber-dim); padding: 0 .35em; font-size: .72rem; margin-right: .3em;
    letter-spacing: .05em; }
  /* analytics readouts: full labels (wrap, never cut), LED-segment bars, value at the end */
  .panel-title { font-family: "VT323", monospace; font-size: 1.3rem; text-transform: uppercase; letter-spacing: .05em;
    border-bottom: 1px dashed var(--amber-dim); margin: .6rem 0 .5rem 0; padding-bottom: .1rem; }
  .panel-title::before { content: "> "; opacity: .6; }
  .panel-note { font-size: .75rem; opacity: .65; margin: -.25rem 0 .55rem 0; text-transform: uppercase; letter-spacing: .04em; }
  .meter { display: grid; grid-template-columns: fit-content(50%) 1fr 2.6em; gap: .3rem .6rem; align-items: center;
    font-size: .78rem; margin-bottom: 1.3rem; }
  .m-label { text-align: right; text-transform: uppercase; letter-spacing: .03em; line-height: 1.2; overflow-wrap: anywhere; }
  .m-track { height: .75rem; background: repeating-linear-gradient(90deg, rgba(255,165,38,.09) 0 6px, transparent 6px 8px); }
  .m-bar { height: 100%; box-shadow: 0 0 6px rgba(255,140,0,.45);
    background: repeating-linear-gradient(90deg, var(--amber) 0 6px, transparent 6px 8px); }
  .m-val { font-family: "VT323", monospace; font-size: 1.2rem; color: var(--amber-hi); text-align: right; }
</style>
""", unsafe_allow_html=True)

ORDERS = {
    "score": "Puntaje (solo informativo)",
    "date_new": "Fecha: recientes primero",
    "date_old": "Fecha: antiguas primero",
    "remote_first": "Modalidad: remoto primero",
    "onsite_first": "Modalidad: presencial primero",
    "location_asc": "Ubicación: A-Z",
    "location_desc": "Ubicación: Z-A",
    "company_asc": "Empresa: A-Z",
    "title_asc": "Cargo: A-Z",
}
LANE_LABEL = {"applied": "CV enviado", "conversation": "Conversación", "action_needed": "Tu atención",
              "interview": "Entrevista", "offer": "Propuesta", "rejected": "Rechazada"}
LANE_ICON = {"applied": "[TX]", "conversation": "[IO]", "action_needed": "[!!]", "interview": "[>>]", "offer": "[$$]",
             "rejected": "[XX]"}


def gmail():
    try:
        return radar.gmail_service()
    except Exception as exc:
        st.error(f"Gmail necesita reconexión: {exc}")
        return None


def local(value: str, fmt: str = "%d/%m %H:%M") -> str:
    parsed = radar.parse_time(value)
    return parsed.astimezone(LOCAL_TZ).strftime(fmt) if parsed else ""


def meter(box, title: str, items, note: str = "") -> None:
    """Terminal-style horizontal bars: one row per (label, count), labels wrap instead of being cut."""
    items = [(str(k), int(v)) for k, v in items]
    top = max((v for _, v in items), default=0) or 1
    rows = "".join(f"<div class='m-label'>{html.escape(k)}</div>"
                   f"<div class='m-track'><div class='m-bar' style='width:{f"max(6px, {v / top * 100:.1f}%)" if v else "0"}'></div></div>"
                   f"<div class='m-val'>{v}</div>" for k, v in items)
    box.markdown(f"<div class='panel-title'>{html.escape(title)}</div>"
                 + (f"<div class='panel-note'>{html.escape(note)}</div>" if note else "")
                 + (f"<div class='meter'>{rows}</div>" if items else "<div class='panel-note'>-- sin datos --</div>"),
                 unsafe_allow_html=True)


def days_since(value: str) -> float | None:
    parsed = radar.parse_time(value)
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 86400 if parsed else None


def save_config(cfg: dict) -> None:
    radar.CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


cfg = radar.config()
pilot = cfg["autopilot"]
all_jobs = radar.jobs("all")
applied = [j for j in all_jobs if j.get("sent_at")]
messages = []
with radar.connect() as db:
    messages = [dict(r) for r in db.execute("SELECT * FROM messages")]
inbound_by_job: dict[int, list] = {}
for m in messages:
    if m["direction"] == "in" and m["job_id"]:
        inbound_by_job.setdefault(m["job_id"], []).append(m)
responded = [j for j in applied if any(m["kind"] != "auto_ack" for m in inbound_by_job.get(j["id"], []))]
week_ago = datetime.now(timezone.utc) - timedelta(days=7)

prediction = cfg.get("branding", {}).get("prediction", "")
label, _, value = prediction.rpartition(":")
st.markdown("<div class='hero'><div class='hero-brand'><svg class='worm' width='96' height='36' viewBox='0 0 96 36' xmlns='http://www.w3.org/2000/svg' shape-rendering='crispEdges'><rect x='60' y='0' width='6' height='6' fill='#ffb000'/><rect x='66' y='0' width='6' height='6' fill='#ffb000'/><rect x='72' y='0' width='6' height='6' fill='#ffb000'/><rect x='54' y='6' width='6' height='6' fill='#ffb000'/><rect x='60' y='6' width='6' height='6' fill='#ffb000'/><rect x='66' y='6' width='6' height='6' fill='#ffb000'/><rect x='72' y='6' width='6' height='6' fill='#ffb000'/><rect x='78' y='6' width='6' height='6' fill='#ffb000'/><rect x='12' y='12' width='6' height='6' fill='#ffb000'/><rect x='18' y='12' width='6' height='6' fill='#ffb000'/><rect x='48' y='12' width='6' height='6' fill='#ffb000'/><rect x='54' y='12' width='6' height='6' fill='#ffb000'/><rect x='60' y='12' width='6' height='6' fill='#ffb000'/><rect x='66' y='12' width='6' height='6' fill='#0b0700'/><rect x='72' y='12' width='6' height='6' fill='#ffb000'/><rect x='78' y='12' width='6' height='6' fill='#ffb000'/><rect x='84' y='12' width='6' height='6' fill='#ffb000'/><rect x='6' y='18' width='6' height='6' fill='#ffb000'/><rect x='12' y='18' width='6' height='6' fill='#ffb000'/><rect x='18' y='18' width='6' height='6' fill='#ffb000'/><rect x='24' y='18' width='6' height='6' fill='#ffb000'/><rect x='42' y='18' width='6' height='6' fill='#ffb000'/><rect x='48' y='18' width='6' height='6' fill='#ffb000'/><rect x='54' y='18' width='6' height='6' fill='#ffb000'/><rect x='60' y='18' width='6' height='6' fill='#ffb000'/><rect x='66' y='18' width='6' height='6' fill='#ffb000'/><rect x='72' y='18' width='6' height='6' fill='#ffb000'/><rect x='78' y='18' width='6' height='6' fill='#ffb000'/><rect x='0' y='24' width='6' height='6' fill='#ffb000'/><rect x='6' y='24' width='6' height='6' fill='#ffb000'/><rect x='24' y='24' width='6' height='6' fill='#ffb000'/><rect x='30' y='24' width='6' height='6' fill='#ffb000'/><rect x='36' y='24' width='6' height='6' fill='#ffb000'/><rect x='42' y='24' width='6' height='6' fill='#ffb000'/><rect x='48' y='24' width='6' height='6' fill='#ffb000'/><rect x='54' y='24' width='6' height='6' fill='#ffb000'/><rect x='0' y='30' width='6' height='6' fill='#ffb000'/><rect x='30' y='30' width='6' height='6' fill='#ffb000'/><rect x='36' y='30' width='6' height='6' fill='#ffb000'/><rect x='42' y='30' width='6' height='6' fill='#ffb000'/><rect x='48' y='30' width='6' height='6' fill='#ffb000'/></svg><span class='hero-title'>WORKWORM</span></div>"
            + (f"<div class='hero-sub'>-OBJETIVO-</div><div class='hero-pred'>{label.strip()}: <b>{value.strip()}</b></div>"
               if prediction else "") + "</div>", unsafe_allow_html=True)


@st.cache_data(ttl=60, show_spinner=False)
def next_run_text() -> tuple[str, str]:
    """('13:00', 'en 2 h 10 min') for the next scheduled agent run."""
    when = agent.next_run()
    if not when:
        return "", ""
    minutes = max(0, int((when - datetime.now(when.tzinfo)).total_seconds() // 60))
    left = f"en {minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"en {minutes} min"
    return when.astimezone(LOCAL_TZ).strftime("%H:%M"), left


def last_cycle() -> dict:
    log_path = radar.ROOT / "data" / "scheduler.log"
    if not log_path.exists():
        return {}
    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    try:
        return json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        return {}


@st.fragment(run_every="3s")
def control_panel():
    """One-line status bar: what the agent is doing, next run, run/stop and the autopilot switch."""
    current = radar.config()
    pilot_now = current["autopilot"]
    pid = agent.running()
    starting = not pid and time.time() - st.session_state.get("start_clicked", 0) < 12
    with radar.connect() as db:
        sent_today = agent.applications_today(db)
    cap = pilot_now["max_applications_per_day"]
    bar = st.container(key="statusbar")
    left, mid, right = bar.columns([5, 1.6, 1.8], vertical_alignment="center")
    if pid or starting:
        info = agent.current_progress()
        elapsed = radar.ago(info.get("started", "")).replace("hace ", "") if info.get("started") else "0 min"
        counter = f" · {info['queue_done']}/{info['queue_total']}" if info.get("queue_total") else ""
        step = info.get("step", "Iniciando")
        step = step if len(step) <= 70 else step[:69] + "…"
        left.markdown(f"<div class='status-line' title='{info.get('step', '')}'><b>● WORKING</b> {elapsed}{counter}"
                      f" &nbsp;&gt; {step}</div>", unsafe_allow_html=True)
        if mid.button("■ DETENER", type="primary", width="stretch", key="stop-cycle"):
            agent.stop_cycle()
            st.session_state["start_clicked"] = 0
            st.rerun()
    else:
        at, _ = next_run_text()
        state = ("○ PAUSED" if not pilot_now["enabled"] else
                 "○ SLEEPING · tope del día" if sent_today >= cap and agent.queue_size() else "○ SLEEPING")
        left.markdown(f"<div class='status-line'><b>{state}</b> &nbsp;·&nbsp; próxima revisión {at or '—'}</div>",
                      unsafe_allow_html=True)
        if mid.button("▶ REVISAR AHORA", type="primary", width="stretch", key="start-cycle",
                      help="No hace falta: el agente corre solo. Sirve para no esperar a la próxima revisión."):
            agent.start_cycle()
            st.session_state["start_clicked"] = time.time()
            st.rerun()
    toggled = right.toggle("PILOTO ACTIVO" if pilot_now["enabled"] else "PILOTO EN PAUSA", value=pilot_now["enabled"],
                           key=f"pilot-{pilot_now['enabled']}",
                           help="Activo: postula y responde solo. En pausa: solo busca ofertas, no envía nada.")
    if toggled != pilot_now["enabled"]:
        current["autopilot"]["enabled"] = toggled
        save_config(current)
        st.rerun(scope="app")


control_panel()

attention = [j for j in all_jobs if j["status"] == "action_needed"]
manual_jobs = [j for j in all_jobs if j["status"] == "manual"]
if attention:
    st.warning(f"[!!] {len(attention)} te necesitan: " + ", ".join(j['company'] or j['title'] for j in attention)
               + " → Pipeline")

board, manual_tab, offers_tab, analytics, sources_tab, activity, settings = st.tabs(
    ["Pipeline", "Postular en LinkedIn", "Ofertas", "Analíticas", "Fuentes", "Actividad", "Configuración"])
with settings:
    settings_general, tailoring_tab = st.tabs(["General", "CV, carta, idioma y exclusiones"])


# ---------------- Pipeline ----------------
with board:
    attention_jobs = [j for j in radar.jobs("action_needed")]
    if attention_jobs:
        st.markdown(f"### [!!] Requieren tu atención · {len(attention_jobs)}")
        st.caption("El agente se frenó porque le falta un dato o una decisión tuya. Completalo acá: "
                   "si era un formulario, la oferta vuelve a la cola y se postula en la próxima revisión; "
                   "si era un mensaje de un reclutador, el agente le responde con lo que escribas.")
        account_jobs = [j for j in attention_jobs if agent.attention_kind(j) == "account"]
        if len(account_jobs) > 1:
            b1, b2 = st.columns(2)
            if b1.button(f"Abrir todos los logins ({len({radar.urlparse(j.get('login_url') or j['url']).netloc for j in account_jobs})} sitios)",
                         type="primary", width="stretch", disabled=bool(agent.running())):
                agent.open_login_pages([j["id"] for j in account_jobs])
                st.toast("Abiertos en el Edge del agente: entrá con Google en cada pestaña y cerrá la ventana")
            if b2.button("Listo, reintentar todas", width="stretch"):
                for j in account_jobs:
                    agent.retry_after_account(j["id"])
                st.rerun()
        for j in attention_jobs:
            kind = agent.attention_kind(j)
            box = st.container(border=True)
            box.markdown(f"**{j['title']}** · {j['company'] or 'Empresa sin dato'} <span class='tag'>"
                         + {"account": "NECESITA CUENTA", "form": "FORMULARIO SIN ENVIAR",
                            "finish": "TERMINALO VOS"}.get(kind, "MENSAJE DE RECLUTADOR")
                         + "</span>", unsafe_allow_html=True)
            if kind == "finish":
                box.markdown(f"**Por qué:** {j.get('note') or ''}")
                box.caption("El agente ya preparó todo: abrí la oferta, subí este CV, pegá la carta, resolvé la verificación "
                            "y enviala. Después tocá «Ya me postulé» y el agente sigue la conversación por correo.")
                with radar.connect() as db:
                    app_row = db.execute("SELECT body, cv_path FROM applications WHERE job_id=?", (j["id"],)).fetchone()
                if box.button("Completar el formulario en el Edge del agente (vos resolvés el captcha y enviás)",
                              key=f"fin-assist-{j['id']}", type="primary", width="stretch", disabled=bool(agent.running())):
                    with st.spinner("El agente está completando el formulario (3 a 10 min)…"):
                        result = agent.assist_form(j["id"], cfg)
                    (box.success if result["ready"] else box.warning)(result["detail"])
                f1, f2, f3, f4 = box.columns(4)
                f1.link_button("Abrir oferta", j["url"], width="stretch")
                cv_file = Path(app_row["cv_path"]) if app_row and app_row["cv_path"] else None
                if cv_file and cv_file.exists():
                    f2.download_button("Descargar CV", cv_file.read_bytes(), file_name=cv_file.name,
                                       mime="application/pdf", key=f"fin-cv-{j['id']}", width="stretch")
                if f3.button("Ya me postulé", key=f"fin-done-{j['id']}", width="stretch"):
                    agent.mark_applied_manually(j["id"])
                    st.rerun()
                if f4.button("Descartar oferta", key=f"fin-drop-{j['id']}", width="stretch"):
                    radar.set_status(j["id"], "discarded")
                    st.rerun()
                if app_row and app_row["body"]:
                    with box.expander("Carta de presentación (para copiar)"):
                        st.code(app_row["body"].split("\n\nRespuestas del formulario:")[0], language=None, wrap_lines=True)
                continue
            if kind == "account":
                box.markdown(f"**Qué necesita:** {(j.get('note') or '').removeprefix('CUENTA:').strip()}")
                if j.get("login_url") and j.get("login_google"):
                    box.caption(f"Se abre directo en la página de login: tocá «Continuar con Google» → {cfg['candidate_email']}, "
                                "cerrá la ventana y «Listo, reintentar».")
                elif j.get("login_url"):
                    box.caption("Se abre directo en la página de login/registro. Este sitio NO ofrece Google: creá la cuenta "
                                f"con {cfg['candidate_email']}, cerrá la ventana y «Listo, reintentar».")
                else:
                    box.caption("Abrí la oferta, tocá «Postularme» → «Continuar con Google» (o creá la cuenta), cerrá la "
                                "ventana y «Listo, reintentar».")
                a1, a2, a3 = box.columns(3)
                if a1.button("Abrir login en el Edge del agente" if j.get("login_url") else "Abrir en el Edge del agente",
                             key=f"acc-open-{j['id']}", type="primary", width="stretch"):
                    agent.open_login_pages([j["id"]])
                if a2.button("Listo, reintentar", key=f"acc-retry-{j['id']}", width="stretch"):
                    agent.retry_after_account(j["id"])
                    st.toast("Vuelve a la cola: se postula en la próxima revisión")
                    st.rerun()
                if a3.button("Descartar oferta", key=f"acc-drop-{j['id']}", width="stretch"):
                    radar.set_status(j["id"], "discarded")
                    st.rerun()
                continue
            box.markdown(f"**Qué necesita:** {j.get('note') or 'ver detalle'}")
            if kind == "reply":
                inbound = [m for m in radar.thread(j["id"]) if m["direction"] == "in"]
                if inbound:
                    with box.expander(f"Mensaje de {inbound[-1]['sender']} · {radar.ago(inbound[-1]['at'])}"):
                        st.text(inbound[-1]["body"][:3000])
            with box.form(f"attn-{j['id']}"):
                text = st.text_area("Tus datos / respuesta" if kind == "form" else "Qué le respondo (datos o el texto tal cual)",
                                    placeholder=("Ej.: 1 año en roles similares. No trabajé en fintech. Pretensión: ..."
                                                 if kind == "form" else "Ej.: Sí, mi pretensión es X bruto. Puedo empezar el 1/11."),
                                    key=f"attn-text-{j['id']}", height=110)
                remember = st.checkbox("Guardar en mi perfil para próximas postulaciones", value=True, key=f"attn-rem-{j['id']}")
                verbatim = (st.checkbox("Enviar mi texto tal cual (sin que Claude lo redacte)", key=f"attn-verb-{j['id']}")
                            if kind == "reply" else False)
                c1, c2, c3 = st.columns(3)
                go = c1.form_submit_button("Reintentar postulación" if kind == "form" else "Responder", type="primary")
                done = c2.form_submit_button("Ya lo resolví yo")
                drop = c3.form_submit_button("Descartar oferta")
            if go:
                if not text.strip():
                    box.error("Escribí los datos primero.")
                elif kind == "form":
                    agent.resolve_form(j["id"], text, cfg, remember)
                    st.toast("Listo: vuelve a la cola con tus datos")
                    st.rerun()
                else:
                    service = gmail()
                    if service:
                        with st.spinner("Respondiendo..."):
                            try:
                                agent.resolve_reply(service, j["id"], text, cfg, remember, verbatim)
                                st.toast("Respuesta enviada")
                                st.rerun()
                            except Exception as exc:
                                box.error(str(exc))
            if done:
                if kind == "form":
                    agent.mark_applied_manually(j["id"])
                else:
                    radar.set_status(j["id"], "conversation", "Lo resolviste vos")
                st.rerun()
            if drop:
                radar.set_status(j["id"], "discarded")
                st.rerun()
        st.divider()
    pipeline_jobs = radar.jobs("pipeline")
    if not pipeline_jobs:
        st.info("> SIN POSTULACIONES TODAVÍA. Con el piloto activo, el agente postula por correo o formulario; "
                "las de LinkedIn aparecen acá cuando marcás «Ya me postulé».")
    counts = {status: sum(1 for j in pipeline_jobs if j["status"] == status) for status in radar.PIPELINE}
    priority = ("action_needed", "interview", "offer", "conversation", "applied", "rejected")
    default = next((st_ for st_ in priority if counts[st_]), "applied")
    stage = st.segmented_control("Etapa", list(radar.PIPELINE), default=default, key="stage",
                                 format_func=lambda s_: f"{LANE_ICON[s_]} {LANE_LABEL[s_].upper()} · {counts[s_]}",
                                 label_visibility="collapsed") or default
    items = [j for j in pipeline_jobs if j["status"] == stage]
    if stage == "interview":
        items.sort(key=lambda j: j.get("interview_at") or "")
    else:
        items.sort(key=lambda j: j.get("last_inbound_at") or j.get("sent_at") or "", reverse=True)
    if not items:
        st.caption("> nada en esta etapa")
    for j in items:
        row = st.container(key=f"row-{j['id']}")
        c1, c2, c3, c4 = row.columns([5, 3, 1.6, 0.9], vertical_alignment="center")
        c1.markdown(f"<div class='row-main'>▸ <b>{j['company'] or 'Empresa sin dato'}</b> — {j['title']}</div>",
                    unsafe_allow_html=True)
        tags = [{"email": "MAIL", "form": "FORM", "manual": "MANUAL"}.get(j.get("channel"), "")]
        if j.get("interview_at"):
            tags.append(f"ENTREVISTA {local(j['interview_at'], '%d/%m %H:%M')}")
        kinds = {m["kind"] for m in inbound_by_job.get(j["id"], [])}
        if kinds - {"auto_ack"}:
            tags.append("RESPONDIERON")
        elif kinds:
            tags.append("ACUSE")
        if j.get("followup_at"):
            tags.append("SEGUIMIENTO")
        late = (stage == "applied" and not j.get("last_inbound_at")
                and (days_since(j.get("sent_at") or "") or 0) > pilot["ghosted_after_days"])
        if late:
            tags.append("SIN RESPUESTA")
        c2.markdown(" ".join(f"<span class='tag{' late' if t == 'SIN RESPUESTA' else ''}'>{t}</span>" for t in tags if t),
                    unsafe_allow_html=True)
        c3.markdown(f"<div class='row-time'>{radar.ago(j.get('last_inbound_at') or j.get('sent_at') or j['updated_at'])}</div>",
                    unsafe_allow_html=True)
        with c4.popover("···", width="stretch"):
            st.markdown(f"**{j['title']}** · {j['company']}  \nCV: {j.get('cv_file') or '—'} · Para: {j.get('recipient') or '—'}")
            if j.get("note"):
                st.caption(j["note"])
            b1, b2 = st.columns(2)
            b1.link_button("Oferta", j["url"], width="stretch")
            if j.get("thread_id"):
                b2.link_button("Hilo en Gmail", agent.mail_link(j["thread_id"]), width="stretch")
            if j.get("interview_link"):
                st.link_button("Link de la entrevista", j["interview_link"])
            if j.get("cv_path") and Path(j["cv_path"]).exists():
                st.download_button("CV enviado (PDF)", Path(j["cv_path"]).read_bytes(),
                                   file_name=Path(j["cv_path"]).name, mime="application/pdf", key=f"cv-{j['id']}")
            if j.get("added_skills"):
                st.caption("Agregadas a habilidades: " + j["added_skills"])
            st.divider()
            for msg in radar.thread(j["id"]):
                who = "> YO" if msg["direction"] == "out" else f"< {msg['sender']}"
                st.markdown(f"**{who}** · {local(msg['at'])} · _{msg['kind']}_  \n{msg['summary']}")
                with st.expander("Texto"):
                    st.text(msg["body"][:3000])
            st.divider()
            target = st.selectbox("Mover a", list(radar.PIPELINE), index=radar.PIPELINE.index(stage),
                                  format_func=STATUSES.get, key=f"move-{j['id']}")
            if st.button("Mover", key=f"movebtn-{j['id']}") and target != stage:
                radar.set_status(j["id"], target, "Movida a mano")
                st.rerun()


# ---------------- Postular en LinkedIn ----------------
with manual_tab:
    pending = radar.sort_jobs(radar.jobs("manual"), "date_new")
    with st.expander("[+] Agregar link de postulación (empresa, consultora, ATS)", expanded=False):
        st.caption("Si el aviso de LinkedIn te manda a otra página para postularte, pegá ese link: el agente se postula ahí "
                   "como en los demás portales, con CV y carta personalizados. Los links que agregás tienen prioridad y no "
                   "pasan por el filtro de puntaje.")
        with st.form("add-link", clear_on_submit=True):
            link_url = st.text_input("Link de postulación", placeholder="https://empresa.com/careers/...")
            origin = st.selectbox("¿De qué oferta de LinkedIn sale? (opcional, para usar su descripción)",
                                  [None] + pending, format_func=lambda j: "— Ninguna —" if j is None
                                  else f"{j['title']} · {j['company']}")
            l1, l2 = st.columns(2)
            link_title = l1.text_input("Cargo (opcional)")
            link_company = l2.text_input("Empresa (opcional)")
            if st.form_submit_button("Agregar a la cola de postulación", type="primary"):
                try:
                    new_id = radar.add_link(link_url, link_title, link_company, origin["id"] if origin else None)
                    st.success(f"Agregado (oferta #{new_id}). Se postula en el próximo ciclo del agente.")
                except ValueError as exc:
                    st.error(str(exc))
        links = radar.sort_jobs([j for j in all_jobs if j["source"] == "link"], "date_new")
        if links:
            st.markdown("**Links cargados**")
            st.dataframe(pd.DataFrame([{"Cargo": j["title"], "Empresa": j["company"], "Estado": STATUSES.get(j["status"], j["status"]),
                                        "Nota": j.get("note") or "", "Link": j["url"]} for j in links]),
                         hide_index=True, width="stretch", column_config={"Link": st.column_config.LinkColumn()})
    import linkedin_bot
    li = linkedin_bot.settings(cfg)
    easy = [j for j in pending if j.get("linkedin_apply") in ("easy", "unknown")]
    unchecked = [j for j in pending if not j.get("linkedin_apply")]
    sent_out = sum(1 for j in all_jobs if j["source"] == "linkedin_ext")
    slot = linkedin_bot.next_slot(cfg)
    st.markdown(f"**{len(easy)} con «Solicitud sencilla» esperando que te postules** · "
                f"{len(unchecked)} todavía sin revisar por el agente · {sent_out} derivadas a sitios externos")
    st.caption(f"El agente abre cada aviso de LinkedIn, espera {li['load_wait_seconds']} s, y si el botón es «Solicitar» "
               f"(a un sitio externo) lo sigue y se postula ahí; las de «Solicitud sencilla» quedan acá para vos. "
               f"Ritmo: máximo {li['max_visits']} avisos cada {li['window_hours']:g} h, con {li['pause_seconds']} s entre uno y otro. "
               f"Usadas en la ventana actual: {linkedin_bot.visits_in_window(cfg)}/{li['max_visits']}"
               + (f" · se libera un lugar a las {slot.astimezone(LOCAL_TZ):%H:%M}." if slot else "."))
    view = st.radio("Ver", ["Solicitud sencilla (para vos)", "Sin revisar todavía", "Todas"], horizontal=True, key="li-view")
    shown = easy if view.startswith("Solicitud") else unchecked if view.startswith("Sin") else pending
    m_order = st.selectbox("Ordenar por", list(ORDERS), index=list(ORDERS).index("date_new"),
                           format_func=ORDERS.get, key="manual-order")
    for job in radar.sort_jobs(shown, m_order):
        stamp = radar.job_timestamp(job)
        row = st.container(border=True)
        c1, c2, c3, c4 = row.columns([6, 1.3, 1.3, 1])
        tag = {"easy": "SOLICITUD SENCILLA", "unknown": "BOTÓN NO DETECTADO", "": "SIN REVISAR"}.get(job.get("linkedin_apply", ""), "")
        c1.markdown(f"**{job['title']}** · {job['company'] or 'Empresa sin dato'} <span class='tag'>{tag}</span>  \n"
                    f"<span class='card-meta'>{job['score']}/100 · {job['location'] or 'Sin ubicación'} · {radar.modality(job)}"
                    f" · publicada {radar.ago(datetime.fromtimestamp(stamp, timezone.utc).isoformat()) if stamp else 'sin fecha'}"
                    f" · {', '.join(radar.technologies(job['description'])[:6])}</span>", unsafe_allow_html=True)
        c2.link_button("Abrir en LinkedIn", job["url"], width="stretch")
        if c3.button("Ya me postulé", key=f"manual-done-{job['id']}", type="primary", width="stretch"):
            agent.mark_applied_manually(job["id"])
            st.rerun()
        if c4.button("Descartar", key=f"manual-skip-{job['id']}", width="stretch"):
            radar.set_status(job["id"], "discarded")
            st.rerun()
        d1, d2 = row.columns([3, 2])
        with d1.expander("Ver descripción"):
            st.write(job["description"][:4000])
        with d2.popover("¿Te manda a otra página? Pegá el link", width="stretch"):
            ext = st.text_input("Link externo de postulación", key=f"ext-{job['id']}")
            if st.button("Que se postule el agente", key=f"extbtn-{job['id']}", type="primary"):
                try:
                    radar.add_link(ext, job["title"], job["company"], job["id"])
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


# ---------------- Ofertas ----------------
with offers_tab:
    f1, f2 = st.columns([1, 2])
    status = f1.selectbox("Estado", ["new", "manual", "blocked", "skipped", "discarded", "failed", "all"],
                          format_func=lambda s: "Todas" if s == "all" else STATUSES[s])
    order = f2.selectbox("Ordenar por", list(ORDERS), index=list(ORDERS).index("date_new"), format_func=ORDERS.get)
    offers = radar.sort_jobs(radar.jobs(status), order)
    st.caption(f"{len(offers)} ofertas. «No se pudo postular» = el formulario pedía crear cuenta, captcha o una prueba; ver la nota.")
    for job in offers:
        header = f"{job['score']}/100 · {job['title']} · {job['company'] or 'Empresa sin dato'}"
        with st.expander(header):
            stamp = radar.job_timestamp(job)
            date_text = datetime.fromtimestamp(stamp, timezone.utc).strftime("%d/%m/%Y") if stamp else "Sin dato"
            st.markdown(f"**Fecha:** {date_text} · **Modalidad:** {radar.modality(job)} · **Ubicación:** {job['location'] or 'Sin dato'} "
                        f"· **Fuente:** {job['source']} · **Estado:** {STATUSES.get(job['status'], job['status'])}"
                        + (f" · **Correo:** {job['contact_email']}" if job["contact_email"] else ""))
            st.write(job["reasons"] or "Sin explicación disponible")
            if job.get("note"):
                st.info(job["note"])
            tech = radar.technologies(job["description"])
            if tech:
                st.caption("Tecnologías: " + ", ".join(tech))
            if job["description"]:
                st.write(job["description"][:1800] + ("…" if len(job["description"]) > 1800 else ""))
            c1, c2 = st.columns([1, 3])
            c1.link_button("Abrir oferta original", job["url"])
            if job["status"] != "discarded" and c1.button("Descartar", key=f"discard-{job['id']}"):
                radar.set_status(job["id"], "discarded")
                st.rerun()
            if agent.is_manual(job):
                if job["status"] != "manual" and c1.button("Mover a «Postular en LinkedIn»", key=f"tomanual-{job['id']}"):
                    radar.set_status(job["id"], "manual")
                    st.rerun()
            elif job["status"] in {"new", "blocked", "skipped", "discarded"}:
                with c2.form(f"apply-{job['id']}"):
                    recipient = st.text_input("Correo para postularte (vacío = el agente completa el formulario del aviso)",
                                              value=job["contact_email"], key=f"rcpt-{job['id']}")
                    go = st.form_submit_button("Postular ahora")
                if go:
                    service = gmail()
                    if not service:
                        st.error("Conectá Gmail en Configuración.")
                    elif recipient.strip() and not radar.valid_recipient(recipient):
                        st.error("Ingresá un correo válido.")
                    else:
                        with st.spinner("Claude está postulando (si es por formulario se abre un Edge aparte y puede tardar unos minutos)..."):
                            try:
                                result = agent.apply(service, job, cfg, recipient_hint=recipient.strip().lower())
                                st.success("Postulación enviada." if result == "applied"
                                           else f"No se envió: {STATUSES.get(result, result)}. Mirá la nota de la oferta.")
                            except Exception as exc:
                                st.error(str(exc))


# ---------------- Analíticas ----------------
with analytics:
    with radar.connect() as db:
        today = agent.applications_today(db)
    m = st.columns(6)
    m[0].metric("Enviadas hoy", today, help=f"Máximo {pilot['max_applications_per_day']} por día (Configuración).")
    m[1].metric("En cola", agent.queue_size(), help="Ofertas que el agente todavía tiene que procesar (sin contar LinkedIn).")
    m[2].metric("CV enviados", len(applied), f"+{sum(1 for j in applied if radar.parse_time(j['sent_at']) >= week_ago)} esta semana")
    m[3].metric("Entrevistas", sum(1 for j in all_jobs if j["status"] == "interview" or j.get("interview_at")))
    m[4].metric("Tu atención", len(attention))
    m[5].metric("Solicitud sencilla", sum(1 for j in manual_jobs if j.get("linkedin_apply") in ("easy", "unknown")),
                help=f"Avisos de LinkedIn para que te postules vos. Además hay "
                     f"{sum(1 for j in manual_jobs if not j.get('linkedin_apply'))} sin revisar por el agente.")
    st.divider()
    if not applied:
        st.info("Las analíticas de postulaciones aparecen cuando se envía el primer CV. Abajo, el panorama de ofertas encontradas.")
    a1, a2 = st.columns(2)
    if applied:
        weekly = pd.DataFrame({"semana": [radar.parse_time(j["sent_at"]).astimezone(LOCAL_TZ).date() - timedelta(
            days=radar.parse_time(j["sent_at"]).astimezone(LOCAL_TZ).weekday()) for j in applied]})
        a1.markdown("<div class='panel-title'>CV enviados por semana</div>", unsafe_allow_html=True)
        a1.bar_chart(weekly.value_counts().rename("CV enviados").sort_index().reset_index().set_index("semana"))
        meter(a2, "Embudo", [("CV enviados", len(applied)), ("Con respuesta", len(responded)),
                             ("Entrevistas", sum(1 for j in applied if j.get("interview_at") or j["status"] == "interview")),
                             ("Propuestas", sum(1 for j in applied if j["status"] == "offer"))])
        firsts = []
        for j in applied:
            replies = [m for m in inbound_by_job.get(j["id"], []) if m["kind"] != "auto_ack"]
            if replies:
                firsts.append((radar.parse_time(min(m["at"] for m in replies)) - radar.parse_time(j["sent_at"])).total_seconds() / 86400)
        k = st.columns(4)
        k[0].metric("Días promedio hasta la 1.ª respuesta", f"{sum(firsts) / len(firsts):.1f}" if firsts else "—")
        k[1].metric("Rechazos", sum(1 for j in applied if j["status"] == "rejected"))
        k[2].metric("Sin respuesta (> %d días)" % pilot["ghosted_after_days"],
                    sum(1 for j in applied if j["status"] == "applied" and (days_since(j["sent_at"]) or 0) > pilot["ghosted_after_days"]))
        k[3].metric("Seguimientos enviados", sum(1 for j in applied if j.get("followup_at")))
        b1, b2 = st.columns(2)
        meter(b1, "Tecnologías en ofertas a las que te postulaste",
              Counter(t for j in applied for t in radar.technologies(j["description"])).most_common(15))
        meter(b2, "CV usado", Counter(j.get("cv_file") or "—" for j in applied).most_common())
    st.divider()
    st.subheader("Países")
    p1, p2 = st.columns(2)
    meter(p1, "A qué países te postulaste", Counter(radar.country(j) for j in applied).most_common())
    meter(p2, "Países de las ofertas pendientes",
          Counter(radar.country(j) for j in all_jobs if j["status"] in ("new", "manual")).most_common(),
          note="Cola del agente + tu lista de LinkedIn")
    st.divider()
    st.subheader("Mercado: todas las ofertas encontradas")
    c1, c2, c3 = st.columns(3)
    described = [j for j in all_jobs if len(j["description"]) > 200]
    meter(c1, "Tecnologías más pedidas", Counter(t for j in described for t in radar.technologies(j["description"])).most_common(15),
          note="Solo ofertas con descripción")
    meter(c2, "Ofertas por estado", Counter(STATUSES.get(j["status"], j["status"]) for j in all_jobs).most_common())
    exits = radar.exit_reasons()
    if exits:
        meter(c2, "Por qué quedaron afuera", Counter(cat for cat, _ in exits.values()).most_common(),
              note="Descartadas o rechazadas")
        with c2.expander("Ver el motivo de cada una"):
            by_id = {j["id"]: j for j in all_jobs}
            st.dataframe(pd.DataFrame(sorted(
                ({"Motivo": cat, "Cargo": by_id[i]["title"], "Empresa": by_id[i]["company"], "Detalle": detail}
                 for i, (cat, detail) in exits.items() if i in by_id), key=lambda r: r["Motivo"])),
                hide_index=True, width="stretch")
    meter(c3, "Ofertas por fuente", Counter(radar.SOURCES.get(j["source"], (j["source"],))[0] for j in all_jobs).most_common())
    meter(c3, "Ofertas por modalidad", Counter(radar.modality(j) for j in all_jobs).most_common())
    meter(c3, "Empresas con más ofertas", Counter(j["company"] for j in all_jobs if j["company"]).most_common(15))


# ---------------- Personalización ----------------
with tailoring_tab:
    import tailor
    t = tailor.settings(cfg)
    st.markdown("Para cada postulación, Claude adapta tu CV de Word y escribe la carta. **Regla fija:** lo que pide el aviso "
                "y no tenés nunca entra en el resumen ni en la experiencia; solo puede ir en *Habilidades técnicas* y en la "
                "carta. El código lo verifica y revierte cualquier violación antes de generar el PDF.")
    left, right = st.columns([1, 1])
    with left:
        st.subheader("Exclusiones")
        ex = agent.exclusions(cfg)
        with st.form("exclusions"):
            words = st.text_input("Palabras en el cargo que excluyen la oferta (separadas por coma)",
                                  ", ".join(ex["title_keywords"]), help="Palabra completa, sin importar mayúsculas: «Lead» excluye «Lead Data Engineer» pero no «Leading».")
            exceptions = st.text_input("Excepciones en el cargo (no excluyen aunque contengan una palabra de arriba)",
                                       ", ".join(ex.get("title_exceptions", [])),
                                       help="Ej.: «Semi Senior» no se excluye aunque contenga «Senior».")
            max_years = st.number_input("Máximo de años de experiencia que puede pedir el aviso (0 = sin límite)", 0, 15,
                                        int(ex.get("max_years", 3)),
                                        help="Con 3, se excluye todo aviso que pida 4 años o más (no cuenta si dice «deseable»).")
            companies = st.text_input("Empresas a excluir (separadas por coma)", ", ".join(ex["companies"]))
            rules = st.text_area("Otras reglas para que Claude descarte ofertas (una por línea)", ex["rules"], height=110,
                                 help="Claude las aplica al leer cada aviso antes de postular.")
            st.markdown("**Nube: solo ecosistema Microsoft**")
            cloud = agent.cloud_filter(cfg)
            cloud_on = st.toggle("Excluir ofertas que piden AWS o Google Cloud (las que no especifican nube sí pasan)",
                                 value=cloud["enabled"])
            mixed = st.toggle("Permitir si además menciona algo de Microsoft (ej.: «Azure y AWS»)",
                              value=cloud["allow_if_microsoft_too"])
            blocked_terms = st.text_area("Términos que excluyen (separados por coma)", ", ".join(cloud["blocked_terms"]), height=80)
            ms_terms = st.text_area("Términos que cuentan como ecosistema Microsoft", ", ".join(cloud["microsoft_terms"]), height=80)
            e1, e2 = st.columns(2)
            save_ex = e1.form_submit_button("Guardar exclusiones", type="primary")
            apply_ex = e2.form_submit_button("Guardar y aplicar a pendientes")
            if save_ex or apply_ex:
                cfg["exclusions"] = {"title_keywords": [w.strip() for w in words.split(",") if w.strip()],
                                     "title_exceptions": [w.strip() for w in exceptions.split(",") if w.strip()],
                                     "max_years": int(max_years),
                                     "companies": [c.strip() for c in companies.split(",") if c.strip()], "rules": rules}
                cfg["cloud_filter"] = {"enabled": cloud_on, "allow_if_microsoft_too": mixed,
                                       "blocked_terms": [t.strip() for t in blocked_terms.split(",") if t.strip()],
                                       "microsoft_terms": [t.strip() for t in ms_terms.split(",") if t.strip()]}
                save_config(cfg)
                moved = agent.apply_exclusions(cfg) if apply_ex else 0
                st.success("Guardado" + (f" · {moved} ofertas pendientes excluidas" if apply_ex else ""))
                st.rerun()
        st.caption("Se aplican a ofertas nuevas y a tu lista de LinkedIn. Los links que agregás vos nunca se excluyen.")
        st.subheader("Idioma de la postulación")
        lang_cfg = cfg.get("language", {"mode": "auto", "fallback": "es"})
        modes = {"auto": "Según el idioma del aviso (inglés → CV y carta en inglés, español → en español)",
                 "es": "Siempre en español", "en": "Siempre en inglés"}
        with st.form("language"):
            mode = st.radio("Idioma del CV, la carta y el correo", list(modes), index=list(modes).index(lang_cfg["mode"]),
                            format_func=modes.get)
            fallback = st.selectbox("Si no se puede detectar el idioma del aviso", ["es", "en"],
                                    index=["es", "en"].index(lang_cfg.get("fallback", "es")),
                                    format_func={"es": "Español", "en": "Inglés"}.get)
            if st.form_submit_button("Guardar idioma"):
                cfg["language"] = {"mode": mode, "fallback": fallback}
                save_config(cfg)
                st.success("Guardado")
                st.rerun()
        st.caption("Las respuestas a reclutadores siempre van en el idioma en que te escriben.")
        with st.form("tailoring"):
            st.subheader("Reglas")
            enabled = st.toggle("Personalizar CV y carta en cada postulación", value=t["enabled"])
            t_summary = st.toggle("Adaptar el resumen profesional", value=t["tailor_summary"])
            t_exp = st.toggle("Reescribir y reordenar viñetas de experiencia (sin hechos nuevos)", value=t["tailor_experience"])
            add_missing = st.toggle("Agregar a Habilidades técnicas lo que pide el aviso y no tengo", value=t["add_missing_skills"])
            max_added = st.number_input("Máximo de tecnologías agregadas por CV", 0, 15, int(t["max_added_skills"]))
            in_letter = st.toggle("Mencionar esas tecnologías en la carta de presentación", value=t["missing_in_cover_letter"])
            rules = st.text_area("Instrucciones para adaptar el CV", t["rules"], height=150)
            letter = st.text_area("Instrucciones para la carta de presentación", t["cover_letter_rules"], height=190)
            c1, c2 = st.columns(2)
            if c1.form_submit_button("Guardar", type="primary"):
                cfg["tailoring"] = {"enabled": enabled, "tailor_summary": t_summary, "tailor_experience": t_exp,
                                    "add_missing_skills": add_missing, "max_added_skills": int(max_added),
                                    "missing_in_cover_letter": in_letter, "rules": rules, "cover_letter_rules": letter}
                save_config(cfg)
                st.success("Guardado")
                st.rerun()
            if c2.form_submit_button("Restaurar valores por defecto"):
                cfg["tailoring"] = dict(tailor.DEFAULTS)
                save_config(cfg)
                st.rerun()
        st.subheader("CV base")
        st.dataframe(pd.DataFrame([{"id": c["id"], "Idioma": c["language"], "Se usa para": c["focus"],
                                    "Word que se adapta": c.get("docx", "—")} for c in cfg["cvs"]]),
                     hide_index=True, width="stretch")
        st.caption("Claude elige el CV base según el idioma y las tecnologías del aviso. Si editás tus .docx, se usan tal cual.")
    with right:
        st.subheader("Vista previa (no envía nada)")
        candidates = sorted([j for j in all_jobs if j["status"] in ("new", "manual", "blocked", "skipped")],
                            key=lambda j: j["title"])
        if candidates:
            pick = st.selectbox("Oferta", candidates, format_func=lambda j: f"{j['score']} · {j['title']} · {j['company']}")
            p1, p2 = st.columns(2)
            cv_id = p1.selectbox("CV base", [c["id"] for c in cfg["cvs"]])
            lang = p2.selectbox("Idioma", ["es", "en"], index=1 if cv_id == "en" else 0)
            if st.button("Generar CV y carta para esta oferta"):
                with st.spinner("Claude está adaptando el CV y Word genera el PDF (≈1 min)..."):
                    try:
                        cv = next(c for c in cfg["cvs"] if c["id"] == cv_id)
                        st.session_state["preview"] = tailor.build(pick, cv, cfg, lang, as_email=bool(pick["contact_email"]),
                                                                   folder="preview")
                    except Exception as exc:
                        st.error(f"No se pudo generar: {exc}")
        result = st.session_state.get("preview")
        if result:
            st.markdown("**Agregadas a Habilidades técnicas:** " + (", ".join(result["added"]) or "ninguna"))
            if result["reverted"]:
                st.warning("Revertido por la regla de experiencia: " + " · ".join(result["reverted"]))
            with st.expander("Lo que pide el aviso y no está en tu experiencia"):
                st.write("\n".join(f"- {m}" for m in result["missing"]) or "Nada")
            st.markdown("**Carta de presentación**")
            st.text(result["cover_letter"])
            if result["summary"] != result["base_summary"]:
                with st.expander("Resumen: antes y después"):
                    st.caption("Antes")
                    st.write(result["base_summary"])
                    st.caption("Después")
                    st.write(result["summary"])
            pdf = Path(result["pdf"])
            if pdf.exists():
                st.download_button("Descargar PDF", pdf.read_bytes(), file_name=pdf.name, mime="application/pdf")
                import pymupdf
                with pymupdf.open(pdf) as doc:
                    for page in doc:
                        st.image(page.get_pixmap(dpi=110).tobytes("png"), width="stretch")


# ---------------- Fuentes ----------------
with sources_tab:
    st.markdown("Plataformas de las que el radar obtiene ofertas y cómo postula el agente en cada una.")
    last = radar.stats()["run"]
    errors = json.loads(last["errors"]) if last else []
    found = Counter(j["source"] for j in all_jobs)
    applied_by = Counter(j["source"] for j in applied)
    enabled = {"linkedin_alert": cfg["gmail"]["enabled"], "indeed": cfg["indeed"]["enabled"],
               "getonbrd": cfg.get("getonbrd", {}).get("enabled", False),
               "greenhouse": any(b["provider"] == "greenhouse" for b in cfg["boards"]),
               "lever": any(b["provider"] == "lever" for b in cfg["boards"]),
               "computrabajo": cfg.get("computrabajo", {}).get("enabled", False),
               "bumeran": cfg.get("bumeran", {}).get("enabled", False),
               **{k: cfg.get("remote_boards", {}).get("enabled", False) for k in ("himalayas", "remotive", "jobicy")},
               "link": True, "linkedin_ext": cfg.get("linkedin_external", {}).get("enabled", True)}
    prefixes = {"linkedin_alert": "LinkedIn", "indeed": "Indeed", "getonbrd": "Get on Board",
                "greenhouse": "greenhouse", "lever": "lever", "computrabajo": "Computrabajo", "bumeran": "Bumeran",
                "himalayas": "Remotos", "remotive": "Remotos", "jobicy": "Remotos", "link": "Link", "linkedin_ext": "LinkedIn externo"}
    rows = []
    for key, (name, how, apply_how) in radar.SOURCES.items():
        problems = [e for e in errors if e.startswith(prefixes[key])]
        rows.append({"Plataforma": name, "Activa": "Sí" if enabled[key] else "No",
                     "Ofertas encontradas": found.get(key, 0), "Postulaciones": applied_by.get(key, 0),
                     "Última búsqueda": ("[!] " + problems[0][:80]) if problems else ("OK" if last and enabled[key] else "—"),
                     "Cómo se obtienen": how, "Cómo se postula": apply_how})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("Empresas seguidas por API: " + ", ".join(f"{b['name']} ({b['provider']})" for b in cfg["boards"])
               + ". Se agregan en config.json → boards.")
    st.caption("Si un aviso indica un correo, el agente postula desde tu Gmail con el CV adjunto en lugar de usar el formulario.")
    st.subheader("Sitios externos a los que te manda LinkedIn")
    external = [j for j in all_jobs if j["source"] == "linkedin_ext"]
    if external:
        def site(url: str) -> str:
            host = radar.urlparse(url).netloc.lower().replace("www.", "")
            for ats in ("greenhouse", "lever", "workday", "myworkdayjobs", "ashby", "smartrecruiters", "bamboohr",
                        "recruitee", "teamtailor", "workable", "jobvite", "icims", "breezy", "personio", "gupy"):
                if ats in host:
                    return ats.replace("myworkdayjobs", "workday").capitalize() + " (ATS)"
            return host
        by_site: dict[str, list] = {}
        for j in external:
            by_site.setdefault(site(j["url"]), []).append(j)
        st.dataframe(pd.DataFrame([{
            "Sitio": name, "Ofertas": len(items),
            "Postuladas": sum(1 for j in items if j.get("sent_at")),
            "No se pudo": sum(1 for j in items if j["status"] in ("blocked", "failed")),
            "En cola": sum(1 for j in items if j["status"] == "new"),
            "Empresas": ", ".join(sorted({j["company"] for j in items if j["company"]}))[:120],
        } for name, items in sorted(by_site.items(), key=lambda kv: -len(kv[1]))]), hide_index=True, width="stretch")
    else:
        st.caption("Todavía no hay. Aparecen a medida que el agente revisa los avisos de LinkedIn con botón «Solicitar».")


# ---------------- Actividad ----------------
with activity:
    log = radar.events(300)
    if log:
        st.dataframe(pd.DataFrame([{"Cuándo": local(e["at"]), "Hace": radar.ago(e["at"]), "Empresa": e["company"] or "",
                                    "Cargo": e["title"] or "", "Evento": e["kind"], "Detalle": e["detail"]} for e in log]),
                     hide_index=True, width="stretch", height=600)
    else:
        st.info("Sin actividad todavía.")


# ---------------- Configuración ----------------
with settings_general:
    s1, s2 = st.columns(2)
    with s1:
        st.subheader("Piloto automático")
        with st.form("pilot"):
            enabled = st.toggle("Activo (postula, responde y hace seguimientos solo)", value=pilot["enabled"])
            per_day = st.number_input("Máximo de postulaciones por día", 0, 50, pilot["max_applications_per_day"])
            followup = st.number_input("Días sin respuesta antes del seguimiento", 2, 30, pilot["followup_after_days"])
            ah = st.slider("Horario de postulaciones", 0, 24, tuple(pilot["apply_hours"]))
            rh = st.slider("Horario de respuestas", 0, 24, tuple(pilot["reply_hours"]))
            check_hours = st.number_input("Revisar correos y ofertas cada (horas)", 1, 24, int(pilot.get("check_every_hours", 4)),
                                          help="Al cambiarlo, la tarea de Windows se reprograma sola.")
            headless = st.toggle("Navegador invisible (headless)", value=pilot.get("browser_headless", False))
            if st.form_submit_button("Guardar"):
                pilot.update(enabled=enabled, max_applications_per_day=int(per_day),
                             followup_after_days=int(followup), apply_hours=list(ah), reply_hours=list(rh),
                             check_every_hours=int(check_hours), browser_headless=headless)
                save_config(cfg)
                import subprocess
                subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                str(radar.ROOT / "install_task.ps1")], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                next_run_text.clear()
                st.success("Guardado")
                st.rerun()
        st.subheader("Gmail")
        if not (radar.SECRETS / "credentials.json").exists():
            st.warning("Falta .secrets/credentials.json (ver README).")
        for role, label in (("main", "Principal: postulaciones y reclutadores"),
                            ("alerts", "Notificaciones: alertas de LinkedIn y avisos del radar")):
            address = radar.gmail_account(role)
            try:
                connected = radar.gmail_service(role=role) is not None
            except Exception:
                connected = False
            if connected:
                st.success(f"{label} · {address} · conectado")
            else:
                st.warning(f"{label} · {address} · sin conectar (o el token venció)")
                if st.button(f"Autorizar {address}", key=f"auth-{role}"):
                    try:
                        radar.gmail_service(interactive=True, role=role)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        st.subheader("Navegador del agente")
        st.write("Para formularios y LinkedIn el agente abre un Edge aparte, con su propio perfil, y lo cierra al terminar. "
                 "Iniciá sesión una vez en ese Edge en Indeed, Get on Board, Computrabajo y Bumeran. LinkedIn no se usa: ahí te postulás vos.")
        if st.button("Abrir Edge del agente"):
            agent.open_agent_edge()
            st.info("Se abrió el Edge del agente. Iniciá sesión y cerralo. No lo uses mientras corre un ciclo.")
        st.subheader("Último ciclo")
        log_path = radar.ROOT / "data" / "scheduler.log"
        lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()] if log_path.exists() else []
        if lines:
            st.json(json.loads(lines[-1]), expanded=False)
        info = radar.stats()["run"]
        if info:
            st.caption(f"Última búsqueda de ofertas: {local(info['finished_at'])} · {info['added']} nuevas")
            for error in json.loads(info["errors"]):
                st.warning(error)
    with s2:
        st.subheader("Tu perfil y preferencias")
        path = radar.ROOT / cfg["profile_path"]
        text = path.read_text(encoding="utf-8")
        missing = text.count("COMPLETAR")
        if missing:
            st.warning(f"Hay {missing} datos marcados COMPLETAR. Si un reclutador pregunta por alguno, la postulación pasa a «Requiere tu atención».")
        with st.form("profile"):
            new_text = st.text_area("El agente lee esto antes de cada correo", text, height=620)
            if st.form_submit_button("Guardar perfil"):
                path.write_text(new_text, encoding="utf-8")
                st.success("Perfil guardado")
                st.rerun()
        st.caption("CV: " + " · ".join(f"{c['id']} ({c['language']})" for c in cfg["cvs"]))
