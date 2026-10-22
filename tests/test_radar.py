import tempfile
import unittest
import base64
from email import message_from_bytes
from pathlib import Path
from unittest.mock import MagicMock, patch

import agent
import tailor
import radar


ALERT = """Tu alerta de empleo para Ingeniero de datos en Argentina
Databricks Developer
QualityAI
Argentina

3 personas estudiaron en la misma institución educativa que tú

Ver anuncio de empleo: https://www.linkedin.com/comm/jobs/view/4472487644/?trackingId=abc

---------------------------------------------------------

Senior Data Engineer
SunnyData
Argentina

Solicitar con perfil y CV

Ver anuncio de empleo: https://www.linkedin.com/comm/jobs/view/4473957301/?trackingId=def
"""


class RadarTests(unittest.TestCase):
    def test_linkedin_digest_extracts_real_email_shape(self):
        jobs = radar.parse_linkedin_email(ALERT, "2026-10-01")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["title"], "Databricks Developer")
        self.assertEqual(jobs[0]["company"], "QualityAI")
        self.assertEqual(jobs[0]["location"], "Argentina")
        self.assertEqual(jobs[0]["external_id"], "4472487644")
        self.assertEqual(jobs[0]["url"], "https://www.linkedin.com/jobs/view/4472487644/")

    def test_senior_role_scores_below_junior_role(self):
        base = {"description": "Python SQL Azure Data Factory PostgreSQL ETL", "location": "Argentina"}
        junior = radar.job_score({**base, "title": "Data Engineer"})[0]
        senior = radar.job_score({**base, "title": "Senior Data Engineer"})[0]
        self.assertGreater(junior, senior)
        self.assertGreaterEqual(junior - senior, 30)

    def test_upsert_keeps_application_status_on_rescan(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(radar, "DB_PATH", Path(temp) / "radar.sqlite3"):
                job = {"source": "linkedin_alert", "external_id": "123", "title": "Data Engineer",
                       "company": "Empresa", "location": "Argentina", "url": "https://example.org/job/123"}
                with radar.connect() as db:
                    self.assertTrue(radar.upsert(db, job))
                with radar.connect() as db:
                    job_id = db.execute("SELECT id FROM jobs").fetchone()["id"]
                radar.set_status(job_id, "discarded")
                with radar.connect() as db:
                    self.assertFalse(radar.upsert(db, job))
                    self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()["status"], "discarded")

    def test_email_does_not_accept_multiple_recipients(self):
        self.assertTrue(radar.valid_recipient("jobs@example.org"))
        self.assertFalse(radar.valid_recipient("jobs@example.org,other@example.org"))
        self.assertFalse(radar.valid_recipient("jobs@example.org\nBcc:other@example.org"))

    def test_remote_boards_require_argentina_eligibility(self):
        job = {"source": "himalayas", "remote": "remote"}
        self.assertTrue(radar.eligible_location({**job, "location": "Argentina, Chile"}))
        self.assertTrue(radar.eligible_location({**job, "location": "Worldwide"}))
        self.assertFalse(radar.eligible_location({**job, "location": "Mexico"}))
        self.assertTrue(radar.eligible_location({"source": "bumeran", "location": "Munro, Buenos Aires"}))

    def test_duplicate_detects_same_offer_across_ids_and_platforms(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(radar, "DB_PATH", Path(temp) / "radar.sqlite3"):
                base = {"title": "Data Engineer GCP", "company": "Aliantec", "location": "Argentina",
                        "url": "https://example.org/1"}
                with radar.connect() as db:
                    radar.upsert(db, {**base, "source": "bumeran", "external_id": "1"})
                    self.assertTrue(radar.duplicate(db, {**base, "source": "bumeran", "external_id": "2"}))
                    self.assertTrue(radar.duplicate(db, {**base, "source": "linkedin_alert", "external_id": "9"}))
                    self.assertFalse(radar.duplicate(db, {**base, "source": "bumeran", "external_id": "1"}))
                    self.assertFalse(radar.duplicate(db, {**base, "company": "Confidencial", "source": "x", "external_id": "3"}))

    def test_date_sort_uses_available_date_and_keeps_missing_last(self):
        offers = [
            {"title": "Sin fecha", "posted_at": "", "seen_at": ""},
            {"title": "Antigua", "posted_at": "2026-09-01", "seen_at": ""},
            {"title": "Reciente", "posted_at": "2026-10-01T12:00:00Z", "seen_at": ""},
        ]
        self.assertEqual([j["title"] for j in radar.sort_jobs(offers, "date_new")],
                         ["Reciente", "Antigua", "Sin fecha"])
        self.assertEqual([j["title"] for j in radar.sort_jobs(offers, "date_old")],
                         ["Antigua", "Reciente", "Sin fecha"])

    def test_modality_and_location_sort(self):
        offers = [
            {"title": "Uno", "location": "Zárate", "remote": ""},
            {"title": "Dos", "location": "Buenos Aires", "remote": "remote"},
            {"title": "Tres", "location": "Mendoza", "remote": "hybrid"},
        ]
        self.assertEqual([j["title"] for j in radar.sort_jobs(offers, "remote_first")],
                         ["Dos", "Tres", "Uno"])
        self.assertEqual([j["title"] for j in radar.sort_jobs(offers, "location_asc")],
                         ["Dos", "Tres", "Uno"])

    def test_rescan_keeps_enriched_description(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(radar, "DB_PATH", Path(temp) / "radar.sqlite3"):
                alert = {"source": "linkedin_alert", "external_id": "9", "title": "Data Engineer",
                         "company": "Acme", "location": "Argentina", "url": "https://example.org/9"}
                with radar.connect() as db:
                    radar.upsert(db, dict(alert))
                    radar.upsert(db, {**alert, "description": "Python SQL. Enviar CV a talento@acme.com.ar"})
                    radar.upsert(db, dict(alert))
                    row = db.execute("SELECT description, contact_email FROM jobs").fetchone()
                self.assertIn("Python", row["description"])
                self.assertEqual(row["contact_email"], "talento@acme.com.ar")

    def test_exit_reason_groups_free_text(self):
        cases = {
            "Excluida por palabra en el cargo: «Senior»": "Seniority / años",
            "Excluida por experiencia: pide 5+ años (máximo configurado 3)": "Seniority / años",
            "Excluida por nube (no es ecosistema Microsoft): AWS, Glue": "Nube no Microsoft",
            "El aviso es remoto solo para residentes en Chile": "Ubicación / residencia",
            "Exige 5+ años, aunque el stack Azure y la ubicación en Argentina encajan.": "Seniority / años",
            "Exige permiso de trabajo vigente para Noruega y noruego profesional": "Permiso de trabajo",
            "El aviso exige sueco fluido, oral y escrito": "Idioma",
            "El aviso ya no acepta solicitudes": "Aviso cerrado",
            "Se postula por el link externo (oferta #207)": "Link externo",
        }
        for detail, expected in cases.items():
            self.assertEqual(radar.exit_reason("skipped", detail), expected, detail)
        self.assertEqual(radar.exit_reason("rejected", ""), "Rechazada por la empresa")
        self.assertEqual(radar.exit_reason("discarded", "Link de alerta cargado por error"), "Descartada por vos")


class TailorTests(unittest.TestCase):
    FACTS = {"summary": "Data Engineer con Python y SQL.",
             "skills": [{"label": "Lenguajes", "value": "Python, SQL (T-SQL, MySQL)"},
                        {"label": "Herramientas", "value": "Git, Docker"}],
             "roles": [{"header": "Data Engineer - Initech", "bullets": ["ETL con Python.", "Dashboards en Power BI."]}]}
    CFG = {"tailoring": {"max_added_skills": 2}}

    def test_new_technology_in_experience_is_reverted(self):
        proposal = {"summary": "Data Engineer con Python, SQL y Databricks.",
                    "skills": self.FACTS["skills"],
                    "roles": [{"bullets": ["ETL con Python y Snowflake.", "Dashboards en Power BI."]}]}
        content, added, reverted = tailor.guard(self.FACTS, proposal, self.CFG)
        self.assertEqual(content["summary"], self.FACTS["summary"])
        self.assertEqual(content["roles"][0]["bullets"], self.FACTS["roles"][0]["bullets"])
        self.assertEqual(len(reverted), 2)

    def test_rephrasing_known_facts_is_kept(self):
        proposal = {"summary": "Data Engineer enfocado en SQL y Python.", "skills": self.FACTS["skills"],
                    "roles": [{"bullets": ["Dashboards ejecutivos en Power BI.", "Pipelines ETL en Python."]}]}
        content, _, reverted = tailor.guard(self.FACTS, proposal, self.CFG)
        self.assertEqual(content["roles"][0]["bullets"][0], "Dashboards ejecutivos en Power BI.")
        self.assertEqual(reverted, [])

    def test_missing_skills_go_only_to_skills_with_limit_and_originals_kept(self):
        proposal = {"summary": self.FACTS["summary"], "roles": [{"bullets": self.FACTS["roles"][0]["bullets"]}],
                    "skills": [{"label": "Lenguajes", "value": "SQL (T-SQL, MySQL), Python, Scala"},
                               {"label": "Otras tecnologías", "value": "Databricks, Snowflake, dbt"}]}
        content, added, _ = tailor.guard(self.FACTS, proposal, self.CFG)
        self.assertEqual(added, ["Scala", "Databricks"])
        items = [i for x in content["skills"] for i in tailor.split_items(x["value"])]
        for original in ("Python", "SQL (T-SQL, MySQL)", "Git", "Docker"):
            self.assertIn(original, items)
        self.assertNotIn("dbt", items)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cv = root / "cv.pdf"
        self.cv.write_bytes(b"%PDF-1.4 example")
        self.cfg = {"candidate_name": "Ana Pérez", "candidate_email": "me@example.org", "profile_path": "profile.md",
                    "cvs": [{"id": "es", "language": "es", "focus": "general", "path": str(self.cv)}],
                    "autopilot": {"model": "sonnet", "max_applications_per_day": 1,
                                  "apply_hours": [0, 24], "reply_hours": [0, 24], "notify_email": "me@example.org"}}
        self.patches = [patch.object(radar, "DB_PATH", root / "radar.sqlite3"),
                        patch.object(agent, "PROGRESS", root / "progress.json"),
                        patch.object(radar, "gmail_service", return_value=None),  # never touch real Gmail in tests
                        patch.object(agent, "LOCK", root / "agent.lock"),
                        patch.object(agent, "cv_texts", return_value={"es": "CV text"})]
        for p in self.patches:
            p.start()
        self.service = MagicMock()
        msgs = self.service.users.return_value.messages.return_value
        msgs.send.return_value.execute.side_effect = [{"id": f"m{i}", "threadId": "t1"} for i in range(10)]
        self.msgs = msgs

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def add(self, external_id, description):
        with radar.connect() as db:
            radar.upsert(db, {"source": "indeed", "external_id": external_id, "title": "Data Engineer",
                              "company": "Acme", "location": "Argentina", "url": "https://example.org/" + external_id,
                              "description": description})
            return db.execute("SELECT id FROM jobs WHERE external_id=?", (external_id,)).fetchone()["id"]

    def decision(self, recipient="cv@acme.com"):
        return {"should_apply": True, "reason": "ok", "recipient": recipient, "language": "es", "cv_id": "es",
                "subject": "Data Engineer – Ana Pérez", "body": "Hola"}

    def test_auto_apply_sends_cv_once_and_respects_daily_cap(self):
        # Same seen_at for all three, so a clock tick between inserts can't put another offer first.
        with patch.object(radar, "now", return_value="2026-10-01T12:00:00+00:00"):
            first = self.add("1", "Python SQL ETL. Enviar CV a cv@acme.com")
            self.add("2", "Python SQL ETL. Enviar CV a jobs@other.com")
            self.add("3", "Python SQL ETL. Postulate en el formulario")
        report = {"applied": 0, "skipped": 0, "errors": []}
        with patch.object(agent.llm, "ask", return_value=self.decision()):
            agent.auto_apply(self.service, self.cfg, report)
        self.assertEqual(report["applied"], 1)
        self.assertEqual(self.msgs.send.call_count, 1)
        raw = self.msgs.send.call_args.kwargs["body"]["raw"]
        email = message_from_bytes(base64.urlsafe_b64decode(raw))
        self.assertEqual(email["To"], "cv@acme.com")
        self.assertEqual(email.get_payload()[-1].get_filename(), "cv.pdf")
        statuses = {j["external_id"]: j["status"] for j in radar.jobs("all")}
        self.assertEqual(statuses, {"1": "applied", "2": "new", "3": "new"})
        self.assertEqual(radar.job(first)["thread_id"], "t1")

    def test_address_not_in_posting_falls_back_to_form(self):
        job_id = self.add("1", "Python SQL ETL. Enviar CV a cv@acme.com")
        with (patch.object(agent.llm, "ask", return_value=self.decision("ceo@acme.com")),
              patch.object(agent, "apply_form", return_value="blocked") as form):
            status = agent.apply(self.service, radar.job(job_id), self.cfg)
        self.assertEqual(status, "blocked")
        form.assert_called_once()
        self.msgs.send.assert_not_called()

    def test_form_application_is_recorded_and_edge_closed(self):
        job_id = self.add("1", "Python SQL ETL. Postulate en el sitio")
        form = {"status": "applied", "reason": "Enviado", "site": "linkedin.com",
                "final_url": "https://www.linkedin.com/jobs/view/1", "answers": [{"question": "Inglés", "answer": "C1"}]}
        with (patch.object(agent.llm, "ask", side_effect=[self.decision(""), form]) as ask,
              patch.object(agent, "upload_copy", return_value=self.cv),
              patch.object(agent, "close_agent_edge") as close):
            status = agent.apply(self.service, radar.job(job_id), self.cfg)
        self.assertEqual(status, "applied")
        close.assert_called_once()
        self.assertIn("--mcp-config", ask.call_args.kwargs["extra"])
        record = radar.job(job_id)
        self.assertEqual((record["status"], record["channel"]), ("applied", "form"))
        self.msgs.send.assert_not_called()

    def test_account_wall_goes_to_attention_and_can_be_retried(self):
        job_id = self.add("1", "Python SQL ETL.")
        form = {"status": "blocked", "reason": "Redirige a register/talent: hay que crear una cuenta nueva.",
                "site": "unlp.talentia.com", "final_url": "", "answers": []}
        with (patch.object(agent.llm, "ask", side_effect=[self.decision(""), form]),
              patch.object(agent, "upload_copy", return_value=self.cv), patch.object(agent, "close_agent_edge")):
            self.assertEqual(agent.apply(self.service, radar.job(job_id), self.cfg), "action_needed")
        record = radar.job(job_id)
        self.assertEqual((record["status"], agent.attention_kind(record)), ("action_needed", "account"))
        agent.needs_account(self.service, record, self.cfg, "unlp.talentia.com", "pide cuenta",
                            {"login_url": "https://unlp.talentia.com/login", "login_google": True})
        record = radar.job(job_id)
        self.assertEqual((record["login_url"], record["login_google"]), ("https://unlp.talentia.com/login", 1))
        with patch.object(agent, "open_agent_edge") as opened:
            self.assertEqual(agent.open_login_pages([job_id]), 1)
        self.assertEqual(opened.call_args.args[0], ("https://unlp.talentia.com/login",))
        agent.retry_after_account(job_id)
        self.assertEqual(radar.job(job_id)["status"], "new")

    def test_form_login_required_keeps_offer_new(self):
        job_id = self.add("1", "Python SQL ETL.")
        form = {"status": "login_required", "reason": "Pide sesión", "site": "linkedin.com", "final_url": "", "answers": []}
        with (patch.object(agent.llm, "ask", side_effect=[self.decision(""), form]),
              patch.object(agent, "upload_copy", return_value=self.cv), patch.object(agent, "close_agent_edge")):
            status = agent.apply(self.service, radar.job(job_id), self.cfg)
        self.assertEqual(status, "login_required:linkedin.com")
        self.assertEqual(radar.job(job_id)["status"], "new")

    def test_interview_reply_moves_to_interview_and_replies_in_thread(self):
        job_id = self.add("1", "Python SQL ETL. Enviar CV a cv@acme.com")
        with patch.object(agent.llm, "ask", return_value=self.decision()):
            agent.apply(self.service, radar.job(job_id), self.cfg)
        message = {"id": "in1", "threadId": "t1", "internalDate": "1790000000000",
                   "payload": {"mimeType": "text/plain", "headers": [
                       {"name": "From", "value": "Ana <cv@acme.com>"}, {"name": "Subject", "value": "Entrevista"},
                       {"name": "Message-ID", "value": "<abc@acme.com>"}],
                       "body": {"data": base64.urlsafe_b64encode("¿Podés el martes 15hs?".encode()).decode()}}}
        result = {"kind": "interview_proposed", "summary": "Proponen martes 15hs", "needs_human": False,
                  "needs_human_reason": "", "should_reply": True, "reply_body": "Perfecto, confirmo.",
                  "interview_at": "2026-10-06T15:00:00-03:00", "interview_link": ""}
        report = {"inbound": 0, "replied": 0, "interviews": 0, "action_needed": 0, "errors": []}
        app = {**radar.job(job_id)}
        import lifeprime
        with (patch.object(agent.llm, "ask", return_value=result),
              patch.object(lifeprime, "add_interview", return_value={"created": True, "already_existed": False,
                                                                     "event_id": "e1", "error": ""}) as calendar):
            cfg = {**self.cfg, "lifeprime": {"enabled": True, "url": "https://example.org/mcp"}}
            agent.handle_inbound(self.service, cfg, app, message, report)
        self.assertEqual(calendar.call_args.args[2], "2026-10-06T15:00:00-03:00")
        self.assertEqual(radar.job(job_id)["status"], "interview")
        self.assertEqual(radar.job(job_id)["interview_at"], "2026-10-06T15:00:00-03:00")
        reply = self.msgs.send.call_args_list[1].kwargs["body"]
        self.assertEqual(reply["threadId"], "t1")
        email = message_from_bytes(base64.urlsafe_b64decode(reply["raw"]))
        self.assertEqual(email["In-Reply-To"], "<abc@acme.com>")
        self.assertEqual(report["interviews"], 1)

    def test_linkedin_offers_wait_for_manual_application(self):
        with radar.connect() as db:
            radar.upsert(db, {"source": "linkedin_alert", "external_id": "77", "title": "Data Engineer",
                              "company": "Acme", "location": "Argentina", "url": "https://www.linkedin.com/jobs/view/77/",
                              "description": "Python SQL ETL"})
            job_id = db.execute("SELECT id FROM jobs").fetchone()["id"]
        self.assertEqual(agent.triage_manual(self.cfg), 1)
        self.assertEqual(radar.job(job_id)["status"], "manual")
        with patch.object(agent.llm, "ask") as ask:
            self.assertEqual(agent.apply(self.service, radar.job(job_id), self.cfg), "manual")
            ask.assert_not_called()
        agent.mark_applied_manually(job_id)
        record = radar.job(job_id)
        self.assertEqual((record["status"], record["channel"]), ("applied", "manual"))
        self.msgs.send.assert_not_called()

    def test_user_link_inherits_linkedin_offer_and_is_applied_with_priority(self):
        with radar.connect() as db:
            radar.upsert(db, {"source": "linkedin_alert", "external_id": "5", "title": "Data Engineer", "company": "Acme",
                              "location": "Argentina", "url": "https://www.linkedin.com/jobs/view/5/",
                              "description": "Python SQL ETL"})
            li = db.execute("SELECT id FROM jobs").fetchone()["id"]
        with patch.object(radar, "page_summary", return_value={"title": "", "company": "", "description": ""}):
            with self.assertRaisesRegex(ValueError, "LinkedIn"):
                radar.add_link("https://www.linkedin.com/jobs/view/5/")
            link = radar.add_link("https://acme.com/careers/42", linked_job_id=li)
            with self.assertRaisesRegex(ValueError, "ya está cargado"):
                radar.add_link("https://acme.com/careers/42")
        record = radar.job(link)
        self.assertEqual((record["title"], record["company"], record["status"]), ("Data Engineer", "Acme", "new"))
        self.assertEqual(radar.job(li)["status"], "skipped")
        rejected = {**self.decision(""), "should_apply": False}
        with (patch.object(agent.llm, "ask", return_value=rejected),
              patch.object(agent, "apply_form", return_value="applied") as form):
            self.assertEqual(agent.apply(self.service, record, self.cfg), "applied")
        form.assert_called_once()

    def test_language_follows_posting_and_picks_matching_cv(self):
        en_pdf = Path(self.temp.name) / "en.pdf"
        en_pdf.write_bytes(b"%PDF-1.4")
        cfg = {"cvs": [{"id": "es", "language": "es", "path": str(self.cv)}, {"id": "en", "language": "en", "path": str(en_pdf)}],
               "language": {"mode": "auto"}}
        en_job = {"title": "Data Engineer", "description": "We are looking for a data engineer to join our team. "
                  "You will work with the data platform and the analytics team, with experience in SQL and Python."}
        es_job = {"title": "Data Engineer", "description": "Buscamos un data engineer para sumarse a la empresa. "
                  "Requisitos: experiencia de dos años con SQL y Python, conocimientos de la nube y de los procesos de datos."}
        self.assertEqual(agent.application_language(en_job, cfg, "es"), "en")
        self.assertEqual(agent.application_language(es_job, cfg, "en"), "es")
        self.assertEqual(agent.application_language({"title": "x", "description": ""}, cfg, "en"), "en")
        self.assertEqual(agent.cv_for(cfg, "en")["id"], "en")
        en_pdf.unlink()  # renamed/deleted CV: fall back to the one that exists and report it
        self.assertEqual(agent.cv_for(cfg, "en")["id"], "es")
        self.assertEqual(agent.missing_cvs(cfg), [str(en_pdf)])
        self.assertEqual(agent.application_language(en_job, {**cfg, "language": {"mode": "es"}}, "en"), "es")

    def test_exclusions_from_config_skip_offers_but_not_user_links(self):
        cfg = {**self.cfg, "exclusions": {"title_keywords": ["Lead", "Sr"], "companies": ["Globant"], "rules": ""}}
        self.assertIn("Lead", agent.excluded({"title": "Lead Data Engineer", "company": "Acme"}, cfg))
        self.assertIn("Sr", agent.excluded({"title": "Sr. Data Engineer", "company": "Acme"}, cfg))
        self.assertEqual(agent.excluded({"title": "Data Engineer (Leading bank)", "company": "Acme"}, cfg), "")
        self.assertIn("Globant", agent.excluded({"title": "Data Engineer", "company": "Globant S.A."}, cfg))
        lead = self.add("1", "Python")
        with radar.connect() as db:
            db.execute("UPDATE jobs SET title='Lead Data Engineer' WHERE id=?", (lead,))
            radar.upsert(db, {"source": "link", "external_id": "https://x.com/1", "title": "Lead Data Engineer",
                              "company": "Acme", "url": "https://x.com/1", "description": "Python"})
        self.assertEqual(agent.apply_exclusions(cfg), 1)
        self.assertEqual(radar.job(lead)["status"], "skipped")
        self.assertEqual([j["status"] for j in radar.jobs("all") if j["source"] == "link"], ["new"])

    def test_cloud_filter_keeps_azure_or_unspecified_and_blocks_aws_gcp(self):
        job = lambda d: {"title": "Data Engineer", "company": "Acme", "description": d}
        self.assertEqual(agent.excluded(job("Pipelines en AWS con Glue y Redshift."), self.cfg), "")  # off by default
        cfg = {**self.cfg, "cloud_filter": {"enabled": True}}
        job = lambda d: {"title": "Data Engineer", "company": "Acme", "description": d}
        self.assertEqual(agent.excluded(job("Python, SQL y PySpark. Experiencia en la nube."), cfg), "")
        self.assertEqual(agent.excluded(job("Azure Data Factory, Databricks y Synapse."), cfg), "")
        self.assertIn("AWS", agent.excluded(job("Pipelines en AWS con Glue y Redshift."), cfg))
        self.assertIn("BigQuery", agent.excluded({"title": "Data Engineer - GCP", "company": "X",
                                                  "description": "BigQuery y Cloud Composer."}, cfg))
        self.assertEqual(agent.excluded(job("Azure y AWS, multicloud."), cfg), "")
        strict = {**cfg, "cloud_filter": {"enabled": True, "allow_if_microsoft_too": False}}
        self.assertIn("AWS", agent.excluded(job("Azure y AWS, multicloud."), strict))
        self.assertEqual(agent.excluded(job("Power BI dataflows y SQL."), cfg), "")

    def test_tailoring_never_adds_aws_or_gcp_skills(self):
        facts = TailorTests.FACTS
        proposal = {"summary": facts["summary"], "roles": [{"bullets": facts["roles"][0]["bullets"]}],
                    "skills": facts["skills"] + [{"label": "Otras", "value": "AWS S3, BigQuery, Databricks"}]}
        _, added, _ = tailor.guard(facts, proposal, {"tailoring": {"max_added_skills": 5},
                                                     "cloud_filter": {"enabled": True}})
        self.assertEqual(added, ["Databricks"])

    def test_user_completes_missing_form_data_and_offer_returns_to_queue(self):
        profile = Path(self.temp.name) / "profile.md"
        profile.write_text("# Perfil\n", encoding="utf-8")
        cfg = {**self.cfg, "profile_path": str(profile)}
        job_id = self.add("1", "Python SQL ETL")
        needs = {"status": "needs_human", "reason": "Pide años en fintech", "site": "ashby", "final_url": "", "answers": []}
        with (patch.object(agent.llm, "ask", side_effect=[self.decision(""), needs]),
              patch.object(agent, "upload_copy", return_value=self.cv), patch.object(agent, "close_agent_edge")):
            self.assertEqual(agent.apply(self.service, radar.job(job_id), cfg), "action_needed")
        record = radar.job(job_id)
        self.assertEqual(agent.attention_kind(record), "form")
        agent.resolve_form(job_id, "No trabajé en fintech.\n1 año en roles similares.", cfg, remember=True)
        record = radar.job(job_id)
        self.assertEqual(record["status"], "new")
        self.assertIn("fintech", record["user_input"])
        self.assertIn("- No trabajé en fintech.", profile.read_text(encoding="utf-8"))

    def test_user_answer_to_recruiter_is_sent_in_thread(self):
        job_id = self.add("1", "Python SQL ETL. Enviar CV a cv@acme.com")
        with patch.object(agent.llm, "ask", return_value=self.decision()):
            agent.apply(self.service, radar.job(job_id), self.cfg)
        with radar.connect() as db:
            db.execute("UPDATE applications SET last_outbound_at='2026-10-01' WHERE job_id=?", (job_id,))
            db.execute("""INSERT INTO messages(gmail_id,job_id,thread_id,direction,sender,subject,body,kind,summary,at)
                          VALUES('in9',?,'t1','in','Ana <cv@acme.com>','Pretensión','¿Cuál es tu pretensión?','question','','2026-10-03')""",
                       (job_id,))
        radar.set_status(job_id, "action_needed", "Pregunta la pretensión")
        msgs = self.service.users.return_value.messages.return_value
        msgs.get.return_value.execute.return_value = {"payload": {"headers": [
            {"name": "Message-ID", "value": "<q@acme.com>"}, {"name": "From", "value": "Ana <cv@acme.com>"}]}}
        self.assertEqual(agent.attention_kind(radar.job(job_id)), "reply")
        agent.resolve_reply(self.service, job_id, "Mi pretensión es X.", self.cfg, remember=False, verbatim=True)
        sent = msgs.send.call_args.kwargs["body"]
        email = message_from_bytes(base64.urlsafe_b64decode(sent["raw"]))
        self.assertEqual((sent["threadId"], email["In-Reply-To"]), ("t1", "<q@acme.com>"))
        self.assertEqual(radar.job(job_id)["status"], "conversation")
        sends = msgs.send.call_count
        with self.assertRaises(ValueError):
            agent.resolve_reply(self.service, job_id, "Mi pretensión es X.", self.cfg, remember=False, verbatim=True)
        self.assertEqual(msgs.send.call_count, sends)

    def test_senior_and_years_exclusions(self):
        cfg = {**self.cfg, "exclusions": {"title_keywords": ["Senior", "Sr"], "max_years": 3,
                                          "title_exceptions": ["Semi Senior", "SSr"], "companies": [], "rules": ""}}
        self.assertIn("Senior", agent.excluded({"title": "Senior Data Engineer", "company": "A"}, cfg))
        self.assertIn("Sr", agent.excluded({"title": "Sr. Data Engineer", "company": "A"}, cfg))
        self.assertEqual(agent.excluded({"title": "Semi Senior Data Engineer", "company": "A"}, cfg), "")
        self.assertIn("5+", agent.excluded({"title": "Data Engineer", "company": "A",
                                             "description": "Requirements: 5+ years of experience with SQL."}, cfg))
        self.assertEqual(agent.excluded({"title": "Data Engineer", "company": "A",
                                         "description": "Experiencia de 2 a 3 años. 5 años de experiencia deseable."}, cfg), "")
        for text in ("Entre 3 y 5 años de experiencia como ingeniero de datos", "3-5 years of experience", "3 to 6 years experience"):
            self.assertEqual(agent.required_years(text), 3, text)

    def test_mail_organizer_detects_job_mail_only(self):
        import mail_organizer as mo
        job = lambda sender, subject, thread="t", known=set(): mo.is_job_mail(sender, subject, thread, known, set(), "m")
        self.assertTrue(job("no-reply <no-reply@us.greenhouse-mail.io>", "We Appreciate You Applying to DoiT"))
        self.assertTrue(job("Get on Board <hola@getonbrd.com>", "Completa tu aplicación"))
        self.assertTrue(job("Metrica España <rrhh@metrica.es>", "Confirmación candidatura"))
        self.assertTrue(job("Ana <ana@empresa.com>", "Hola", thread="t9", known={"t9"}))
        self.assertFalse(job("Airbnb <automated@airbnb.com>", "Booking request sent for Suite Maracanã"))
        self.assertFalse(job("Superprof Argentina <hola@superprof.com>", "Nuevo mensaje Elsa"))
        self.assertFalse(job("Deel <no-reply@deel.com>", "Resumen diario para Oct 2nd"))

    def test_country_from_location(self):
        self.assertEqual(radar.country({"location": "Capital Federal, Buenos Aires"}), "Argentina")
        self.assertEqual(radar.country({"location": "Córdoba, X, AR"}), "Argentina")
        self.assertEqual(radar.country({"location": "Toronto, Ontario, Canadá"}), "Canadá")
        self.assertEqual(radar.country({"location": "Netherlands"}), "Países Bajos")
        self.assertEqual(radar.country({"location": "Worldwide"}), "Remoto global")

    def test_linkedin_external_button_becomes_queued_offer_and_easy_apply_stays(self):
        import linkedin_bot
        ids = []
        for n in ("1", "2"):
            with radar.connect() as db:
                radar.upsert(db, {"source": "linkedin_alert", "external_id": n, "title": "Data Engineer", "company": f"Co{n}",
                                  "location": "Argentina", "url": f"https://www.linkedin.com/jobs/view/{n}/",
                                  "description": "Python SQL Azure"})
                ids.append(db.execute("SELECT id FROM jobs WHERE external_id=?", (n,)).fetchone()["id"])
        agent.triage_manual(self.cfg)
        results = {f"https://www.linkedin.com/jobs/view/{n}/": r for n, r in (
            ("1", {"kind": "external", "url": "https://boards.greenhouse.io/co1/jobs/9", "detail": ""}),
            ("2", {"kind": "easy", "url": "", "detail": ""}))}
        cfg = {**self.cfg, "linkedin_external": {"pause_seconds": 0, "max_visits": 5}}
        with (patch.object(linkedin_bot, "visit", side_effect=lambda job, cfg: results[job["url"]]),
              patch.object(radar, "page_summary", return_value={"title": "", "company": "", "description": ""})):
            report = {"errors": []}
            linkedin_bot.resolve(cfg, report)
        self.assertEqual(radar.job(ids[1])["status"], "manual")
        self.assertEqual(radar.job(ids[1])["linkedin_apply"], "easy")
        external = [j for j in radar.jobs("all") if j["source"] == "linkedin_ext"]
        self.assertEqual((len(external), external[0]["status"], external[0]["company"]), (1, "new", "Co1"))
        self.assertFalse(agent.is_manual(external[0]))
        self.assertEqual(linkedin_bot.visits_in_window(cfg), 2)

    def test_linkedin_visits_respect_window_limit(self):
        import linkedin_bot
        with radar.connect() as db:
            for _ in range(5):
                radar.log_event(db, None, "linkedin_visit", "external: x")
        cfg = {**self.cfg, "linkedin_external": {"max_visits": 5, "window_hours": 4}}
        self.assertIsNotNone(linkedin_bot.next_slot(cfg))
        with patch.object(linkedin_bot, "visit") as visit:
            linkedin_bot.resolve(cfg, {"errors": []})
            visit.assert_not_called()

    def test_match_job_by_address_domain_and_company(self):
        apps = [{"recipient": "cv@acme.com", "company": "Acme"}, {"recipient": "x@gmail.com", "company": "Globant"}]
        self.assertIs(agent.match_job("Ana <ana@acme.com>", "", apps), apps[0])
        self.assertIsNone(agent.match_job("Bob <bob@gmail.com>", "hola", apps))
        self.assertIs(agent.match_job("rrhh@talent.io", "Proceso en Globant", apps), apps[1])
        self.assertIsNone(agent.match_job("jobalerts-noreply@linkedin.com", "Globant", apps))


class GmailExpiryTests(unittest.TestCase):
    def test_revoked_token_raises_gmail_expired_and_cycle_reports_it(self):
        from google.auth.exceptions import RefreshError
        with tempfile.TemporaryDirectory() as temp:
            secrets = Path(temp)
            (secrets / "credentials.json").write_text("{}", encoding="utf-8")
            (secrets / "token_main.json").write_text("{}", encoding="utf-8")
            creds = MagicMock(expired=True, refresh_token="r")
            creds.refresh.side_effect = RefreshError("invalid_grant: Token has been expired or revoked.")
            with patch.object(radar, "SECRETS", secrets), \
                 patch.object(radar, "gmail_account", return_value="me@example.org"), \
                 patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=creds):
                with self.assertRaises(radar.GmailExpired) as caught:
                    radar.gmail_service()
                self.assertIn("me@example.org", str(caught.exception))
                with patch.object(agent, "LOCK", secrets / "agent.lock"), \
                     patch.object(agent, "PROGRESS", secrets / "progress.json"), \
                     patch.object(radar, "config", return_value={"autopilot": {}}), \
                     patch.object(agent, "collect_due", return_value=False), \
                     patch.object(agent, "missing_cvs", return_value=[]), \
                     patch.object(agent, "apply_exclusions", return_value=0), \
                     patch.object(agent, "triage_manual", return_value=0), \
                     patch.object(agent, "progress"), patch.object(agent, "queue_size", return_value=0):
                    report = agent.run()
        self.assertTrue(any("Venció la autorización de Gmail" in e for e in report["errors"]))

if __name__ == "__main__":
    unittest.main()
