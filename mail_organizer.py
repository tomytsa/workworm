"""Keep the main inbox clean: job-search mail gets the 'Ofertas de trabajo' label and leaves the inbox."""

from __future__ import annotations

import re
from email.utils import parseaddr

import radar
from radar import connect, log_event, normalize

DEFAULTS = {"enabled": True, "label": "Ofertas de trabajo", "archive": True, "days": 60}
JOB_SENDERS = ("jobs-noreply@linkedin.com", "jobalerts-noreply@linkedin.com", "jobs-listings@linkedin.com",
               "getonbrd", "greenhouse", "lever.co", "ashbyhq", "myworkday", "workday", "bumeran", "zonajobs",
               "computrabajo", "indeed", "jobgether", "breezy", "icims", "smartrecruiters", "teamtailor", "workable",
               "recruitee", "bamboohr", "gupy", "himalayas", "remotive", "jobicy", "reachmee", "successfactors",
               "personio", "jobvite", "hireology", "talent", "recruit", "careers", "rrhh", "seleccion")
JOB_SUBJECT = re.compile(r"postulaci|candidatur|your application|tu aplicaci|tu solicitud de empleo|applying|"
                         r"application (received|status|update)|solicitud de trabajo|proceso de selecci|entrevista|"
                         r"interview|job offer|oferta laboral|oferta de trabajo|vacante|reclutador|recruiter|"
                         r"we appreciate you applying|thank you for applying|gracias por postularte", re.I)


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg.get("mail_organizer", {})}


def label_id(service, name: str) -> str:
    for label in service.users().labels().list(userId="me").execute().get("labels", []):
        if label["name"].lower() == name.lower():
            return label["id"]
    created = service.users().labels().create(userId="me", body={
        "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}).execute()
    return created["id"]


def is_job_mail(sender: str, subject: str, thread_id: str, known_threads: set[str], known_ids: set[str], msg_id: str) -> bool:
    if msg_id in known_ids or thread_id in known_threads:
        return True
    address = parseaddr(sender)[1].lower()
    domain = address.split("@")[-1]
    if any(key in address or key in domain for key in JOB_SENDERS):
        return True
    return bool(JOB_SUBJECT.search(subject or ""))


def organize(service, cfg: dict, report: dict | None = None, days: int | None = None) -> int:
    """Label and (optionally) archive job-search mail still in the inbox. Returns how many were moved."""
    s = settings(cfg)
    if not s["enabled"]:
        return 0
    target = label_id(service, s["label"])
    with connect() as db:
        known_ids = {r["gmail_id"] for r in db.execute("SELECT gmail_id FROM messages WHERE job_id IS NOT NULL")}
        known_threads = {r["thread_id"] for r in db.execute(
            "SELECT thread_id FROM messages WHERE job_id IS NOT NULL UNION SELECT thread_id FROM applications")} - {""}
    query = f"in:inbox newer_than:{int(days or s['days'])}d"
    moved, page = [], None
    while True:
        args = {"userId": "me", "q": query, "maxResults": 200}
        if page:
            args["pageToken"] = page
        listing = service.users().messages().list(**args).execute()
        for item in listing.get("messages", []):
            meta = service.users().messages().get(userId="me", id=item["id"], format="metadata",
                                                  metadataHeaders=["From", "Subject"]).execute()
            headers = {h["name"].lower(): h["value"] for h in meta.get("payload", {}).get("headers", [])}
            if is_job_mail(headers.get("from", ""), headers.get("subject", ""), item["threadId"],
                           known_threads, known_ids, item["id"]):
                moved.append(item["id"])
        page = listing.get("nextPageToken")
        if not page:
            break
    for start in range(0, len(moved), 500):
        service.users().messages().batchModify(userId="me", body={
            "ids": moved[start:start + 500], "addLabelIds": [target],
            "removeLabelIds": ["INBOX"] if s["archive"] else []}).execute()
    if moved:
        with connect() as db:
            log_event(db, None, "mail_organized", f"{len(moved)} correos a «{s['label']}»"
                      + (" (fuera de Recibidos)" if s["archive"] else ""))
    if report is not None:
        report["organized"] = len(moved)
    return len(moved)
