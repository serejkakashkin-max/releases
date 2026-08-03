from __future__ import annotations

import io
import json
import multiprocessing
import tempfile
import time
import unittest
from unittest import mock
import zipfile
from pathlib import Path

from docx import Document
from flask import Flask

from tests._support import PROJECT_ROOT, prepare_config_import

prepare_config_import()

from routes.document_template_routes import document_template_bp
from services.document_template_candidate_service import validate_staged_candidate
from services.document_template_auth_service import check_or_update_rate_limit, client_source, safe_next, strong_session_secret
import services.document_template_auth_service as auth_service
from services.document_template_read_service import build_document_whitelist
from services.document_template_storage_service import get_candidate, list_history, update_candidate, write_uploaded_candidate
from services.document_template_publish_service import publish_candidate, recover_stale_operations, rollback_version
from services.document_template_read_service import resolve_document
from services.document_template_validation_service import inspect_docx
from services.release_template_catalog_service import clear_template_catalog_cache


SECRET = "stage2-strong-secret-0123456789abcdef0123456789"
TOKEN = "shared-editor-token"


def _rate_limit_worker(runtime_root: str, start_event, result_queue) -> None:
    app = Flask("rate-worker")
    app.config.update(SECRET_KEY=SECRET, DOCUMENT_TEMPLATE_CENTER_RUNTIME_ROOT=Path(runtime_root))
    try:
        start_event.wait(10)
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "198.51.100.44"}):
            status = check_or_update_rate_limit("Concurrent Editor", failure=True)
        result_queue.put((status.allowed, status.code))
    except Exception as exc:
        result_queue.put((False, type(exc).__name__))


def make_template(path: Path, *, heading: str = "Шаблон") -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = Document()
    document.add_heading(heading, level=1)
    document.add_paragraph("Версия RELEASE_VERSION")
    document.add_paragraph("Дата DATE")
    table = document.add_table(rows=1, cols=4)
    headers = table.rows[0].cells
    headers[0].text = "№"
    headers[1].text = "ЗНИ/JIRA ID"
    headers[2].text = "Issue"
    headers[3].text = "Issue Type"
    document.save(path)
    return path.read_bytes()


def build_app(root: Path, runtime: Path, *, secret=SECRET, enabled=True) -> Flask:
    app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"), static_folder=str(PROJECT_ROOT / "static"))
    app.config.update(
        TESTING=True, SECRET_KEY=secret,
        DOCUMENT_TEMPLATE_CENTER_ENABLED=enabled,
        DOCUMENT_TEMPLATE_CENTER_ROOT=root,
        DOCUMENT_TEMPLATE_CENTER_RUNTIME_ROOT=runtime,
        DOCUMENT_TEMPLATE_EDITOR_TOKEN=TOKEN,
    )
    app.add_url_rule("/", endpoint="main.index", view_func=lambda: "home")
    app.add_url_rule("/help", endpoint="main.help_page", view_func=lambda: "help")
    app.add_url_rule("/dashboard", endpoint="dashboard.dashboard", view_func=lambda: "dashboard")
    app.add_url_rule("/release-monitor", endpoint="dashboard.release_monitor_page", view_func=lambda: "monitor")
    app.add_url_rule("/mpr", endpoint="mpr.mpr_page", view_func=lambda: "mpr")
    app.register_blueprint(document_template_bp)
    return app


class Stage2AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "doc_templates"
        make_template(self.root / "CAT" / "Комплект PL (12345)" / "Шаблон.docx")
        clear_template_catalog_cache()
        self.app = build_app(self.root, base / "runtime")
        self.client = self.app.test_client()

    def tearDown(self):
        clear_template_catalog_cache(); self.temp.cleanup()

    def test_secret_is_fail_closed_for_missing_short_default_and_repeated(self):
        for secret in (None, "short", "super_secret_key", "a" * 64):
            app = build_app(self.root, Path(self.temp.name) / ("runtime-" + str(len(str(secret)))), secret=secret)
            response = app.test_client().get("/admin/document-templates/login")
            self.assertEqual(503, response.status_code)
            self.assertNotIn(str(secret), response.get_data(as_text=True))
        with self.app.app_context():
            self.assertGreaterEqual(len(strong_session_secret()), 32)

    def test_full_and_htmx_auth_redirects(self):
        full = self.client.get("/admin/document-templates")
        htmx = self.client.get("/admin/document-templates", headers={"HX-Request": "true"})
        mutation = self.client.post("/admin/document-templates/session/logout")
        self.assertEqual(302, full.status_code)
        self.assertIn("/admin/document-templates/login", full.headers["Location"])
        self.assertEqual(403, htmx.status_code); self.assertIn("HX-Redirect", htmx.headers)
        self.assertNotIn("Войти", htmx.get_data(as_text=True))
        self.assertEqual(403, mutation.status_code)

    def test_login_csrf_logout_preserves_other_session_keys(self):
        with self.client.session_transaction() as session:
            session["sup_admin_authenticated"] = True
        bad = self.client.post("/admin/document-templates/session/login", data={"display_name": "Редактор", "token": "bad"})
        self.assertEqual(403, bad.status_code)
        good = self.client.post("/admin/document-templates/session/login", data={"display_name": "Редактор", "token": TOKEN})
        self.assertEqual(303, good.status_code)
        rejected = self.client.post("/admin/document-templates/session/logout")
        self.assertEqual(403, rejected.status_code)
        with self.client.session_transaction() as session:
            csrf = session["document_template_editor_csrf_nonce"]
        logout = self.client.post("/admin/document-templates/session/logout", data={"_csrf_token": csrf})
        self.assertEqual(303, logout.status_code)
        with self.client.session_transaction() as session:
            self.assertTrue(session["sup_admin_authenticated"])
            self.assertFalse([key for key in session if key.startswith("document_template_editor_")])

    def test_idle_expiry_uses_htmx_redirect_and_mutation_is_not_replayed(self):
        self.client.post("/admin/document-templates/session/login", data={"display_name": "Редактор", "token": TOKEN})
        with self.client.session_transaction() as session:
            session["document_template_editor_last_seen"] = int(time.time()) - 31 * 60
            csrf = session["document_template_editor_csrf_nonce"]
        expired_get = self.client.get("/admin/document-templates", headers={"HX-Request": "true"})
        self.assertEqual(403, expired_get.status_code); self.assertIn("HX-Redirect", expired_get.headers)
        expired_mutation = self.client.post("/admin/document-templates/session/logout", headers={"HX-Request": "true", "X-CSRF-Token": csrf})
        self.assertEqual(403, expired_mutation.status_code); self.assertIn("HX-Redirect", expired_mutation.headers)

    def test_absolute_session_lifetime_expires_even_when_recently_seen(self):
        self.client.post("/admin/document-templates/session/login", data={"display_name": "Редактор", "token": TOKEN})
        with self.client.session_transaction() as session:
            session["document_template_editor_login_at"] = int(time.time()) - 4 * 60 * 60 - 1
            session["document_template_editor_last_seen"] = int(time.time())
        self.assertEqual(302, self.client.get("/admin/document-templates").status_code)

    def test_token_rotation_invalidates_existing_session(self):
        self.client.post("/admin/document-templates/session/login", data={"display_name": "Редактор", "token": TOKEN})
        self.app.config["DOCUMENT_TEMPLATE_EDITOR_TOKEN"] = "rotated-token"
        response = self.client.get("/admin/document-templates")
        self.assertEqual(302, response.status_code)

    def test_blueprint_csp_and_no_store_are_applied(self):
        self.client.post("/admin/document-templates/session/login", data={"display_name": "Редактор", "token": TOKEN})
        response = self.client.get("/admin/document-templates")
        self.assertEqual("no-store", response.headers["Cache-Control"])
        self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("connect-src 'self'", csp); self.assertIn("object-src 'none'", csp)
        html = response.get_data(as_text=True)
        self.assertNotRegex(html, r'(?:src|href|action|hx-get)=["\'](?:https?:)?//')

    def test_safe_next_rejects_external_and_encoded_escape(self):
        with self.app.test_request_context("/admin/document-templates/login", headers={"X-Forwarded-Prefix": "/oplot"}):
            fallback = "/oplot/admin/document-templates/"
            self.assertEqual(fallback, safe_next("https://evil.example/"))
            self.assertEqual(fallback, safe_next("//evil.example/path"))
            self.assertEqual(fallback, safe_next("/admin/document-templates/%2e%2e/help"))
            self.assertEqual(fallback, safe_next("/admin/document-templates/%5c%5cevil"))
            self.assertEqual(fallback, safe_next("/admin/document-templates?token=x"))
            self.assertEqual("/oplot/admin/document-templates?q=test&page=2", safe_next("/oplot/admin/document-templates?q=test&page=2"))

    def test_rate_limit_state_has_no_raw_identifiers(self):
        for _ in range(2):
            self.client.post("/admin/document-templates/session/login", data={"display_name": "Очень Особое Имя", "token": "wrong"}, environ_base={"REMOTE_ADDR": "203.0.113.99"})
        state = (Path(self.temp.name) / "runtime/cache/document_template_center/auth/rate_limit.json").read_text(encoding="utf-8")
        self.assertNotIn("Очень Особое Имя", state)
        self.assertNotIn("203.0.113.99", state)
        self.assertNotIn("wrong", state)
        self.assertIsInstance(json.loads(state)["buckets"], dict)

    def test_rate_limit_multiprocess_updates_are_atomic(self):
        context = multiprocessing.get_context("spawn")
        event = context.Event(); queue = context.Queue()
        runtime = str(Path(self.temp.name) / "multiprocess-runtime")
        processes = [context.Process(target=_rate_limit_worker, args=(runtime, event, queue)) for _ in range(4)]
        for process in processes: process.start()
        event.set()
        results = [queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(20); self.assertEqual(0, process.exitcode)
        self.assertTrue(all(result[0] for result in results), results)
        payload = json.loads((Path(runtime) / "cache/document_template_center/auth/rate_limit.json").read_text(encoding="utf-8"))
        self.assertEqual([4, 4], sorted(len(item["attempts"]) for item in payload["buckets"].values()))

    def test_proxy_headers_are_opt_in(self):
        self.app.config["TRUST_PROXY_HEADERS"] = False
        with self.app.test_request_context("/", headers={"X-Forwarded-For": "203.0.113.8"}, environ_base={"REMOTE_ADDR": "127.0.0.9"}):
            self.assertEqual("127.0.0.9", client_source())
        self.app.config["TRUST_PROXY_HEADERS"] = True
        with self.app.test_request_context("/", headers={"X-Forwarded-For": "203.0.113.8, garbage"}, environ_base={"REMOTE_ADDR": "127.0.0.9"}):
            self.assertEqual("203.0.113.8", client_source())

    def test_corrupt_rate_limit_state_fails_closed(self):
        path = Path(self.temp.name) / "runtime/cache/document_template_center/auth/rate_limit.json"
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text("not-json", encoding="utf-8")
        response = self.client.post("/admin/document-templates/session/login", data={"display_name": "Редактор", "token": TOKEN})
        self.assertEqual(503, response.status_code)
        self.assertEqual("not-json", path.read_text(encoding="utf-8"))

    def test_rate_limit_capacity_does_not_evict_active_buckets_and_expired_are_cleaned(self):
        path = Path(self.temp.name) / "runtime/cache/document_template_center/auth/rate_limit.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        path.write_text(json.dumps({"version": 1, "buckets": {"a": {"attempts": [now], "blocked_until": 0}, "b": {"attempts": [now], "blocked_until": 0}}}), encoding="utf-8")
        original_limit = auth_service.MAX_BUCKETS
        auth_service.MAX_BUCKETS = 2
        try:
            with self.app.test_request_context("/", environ_base={"REMOTE_ADDR": "192.0.2.10"}):
                status = check_or_update_rate_limit("Capacity User", failure=True, now=now)
            self.assertFalse(status.allowed); self.assertEqual("rate_limit_capacity", status.code)
            self.assertEqual({"a", "b"}, set(json.loads(path.read_text(encoding="utf-8"))["buckets"]))
            path.write_text(json.dumps({"version": 1, "buckets": {"expired": {"attempts": [now - 1000], "blocked_until": 0}}}), encoding="utf-8")
            with self.app.test_request_context("/", environ_base={"REMOTE_ADDR": "192.0.2.10"}):
                cleaned = check_or_update_rate_limit("Capacity User", failure=True, now=now)
            self.assertTrue(cleaned.allowed)
            self.assertNotIn("expired", json.loads(path.read_text(encoding="utf-8"))["buckets"])
        finally:
            auth_service.MAX_BUCKETS = original_limit

    def test_coarse_source_bucket_blocks_display_name_rotation(self):
        with self.app.test_request_context("/", environ_base={"REMOTE_ADDR": "192.0.2.55"}):
            last = None
            for index in range(50):
                last = check_or_update_rate_limit(f"Rotating User {index}", failure=True, now=1000 + index)
            self.assertFalse(last.allowed); self.assertEqual("rate_limited", last.code)


class Stage2WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "doc_templates"
        self.active_path = self.root / "CAT" / "Комплект PL (12345)" / "Шаблон.docx"
        self.original = make_template(self.active_path, heading="Активный")
        self.candidate = make_template(base / "source.docx", heading="Кандидат")
        clear_template_catalog_cache()
        self.app = build_app(self.root, base / "runtime")
        self.client = self.app.test_client()
        self.client.post("/admin/document-templates/session/login", data={"display_name": "Редактор", "token": TOKEN})
        with self.client.session_transaction() as session:
            self.csrf = session["document_template_editor_csrf_nonce"]
        self.document_id = next(iter(build_document_whitelist(self.root)))

    def tearDown(self):
        clear_template_catalog_cache(); self.temp.cleanup()

    def _upload(self, payload=None):
        return self.client.post(f"/admin/document-templates/documents/{self.document_id}/candidates", data={
            "_csrf_token": self.csrf, "comment": "Обновлено оформление",
            "file": (io.BytesIO(payload or self.candidate), "Новая версия.docx"),
        }, content_type="multipart/form-data")

    def _upload_and_validate(self, payload=None):
        upload = self._upload(payload)
        self.assertEqual(303, upload.status_code)
        candidate_uuid = upload.headers["Location"].rstrip("/").split("/")[-1]
        response = self.client.post(f"/admin/document-templates/documents/{self.document_id}/candidates/{candidate_uuid}/validate", data={"_csrf_token": self.csrf})
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        return candidate_uuid

    def test_upload_validate_preview_publish_history_and_rollback(self):
        upload = self._upload(); self.assertEqual(303, upload.status_code)
        candidate_uuid = upload.headers["Location"].rstrip("/").split("/")[-1]
        with self.app.app_context():
            metadata = get_candidate(candidate_uuid, document_id=self.document_id)
        self.assertEqual("uploaded", metadata["state"])
        validate = self.client.post(f"/admin/document-templates/documents/{self.document_id}/candidates/{candidate_uuid}/validate", data={"_csrf_token": self.csrf})
        self.assertEqual(200, validate.status_code, validate.get_data(as_text=True))
        with self.app.app_context():
            metadata = get_candidate(candidate_uuid, document_id=self.document_id)
        self.assertEqual("valid", metadata["state"])
        preview = self.client.get(f"/admin/document-templates/documents/{self.document_id}/candidates/{candidate_uuid}/preview")
        test_preview = self.client.get(f"/admin/document-templates/documents/{self.document_id}/candidates/{candidate_uuid}/test-document/preview")
        self.assertEqual(self.candidate, preview.data); self.assertEqual(200, test_preview.status_code)
        published = self.client.post(f"/admin/document-templates/documents/{self.document_id}/candidates/{candidate_uuid}/publish", data={"_csrf_token": self.csrf})
        self.assertEqual(303, published.status_code)
        self.assertEqual(self.candidate, self.active_path.read_bytes())
        audit_text = (Path(self.temp.name) / "runtime/data/document_template_center/audit/events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, audit_text); self.assertNotIn(self.csrf, audit_text); self.assertNotIn(str(self.root), audit_text)
        self.assertEqual("publish", json.loads(audit_text.splitlines()[-1])["action"])
        with self.app.app_context():
            versions = list_history(self.document_id)
        self.assertEqual(1, len(versions))
        self.assertEqual(self.original, (Path(self.temp.name) / f"runtime/data/document_template_center/history/{self.document_id}/{versions[0]['version_uuid']}/document.docx").read_bytes())
        rolled = self.client.post(f"/admin/document-templates/documents/{self.document_id}/history/{versions[0]['version_uuid']}/rollback", data={"_csrf_token": self.csrf, "reason": "Возвращаем проверенный вариант", "expected_active_sha": metadata["candidate_sha"]})
        self.assertEqual(303, rolled.status_code, rolled.get_data(as_text=True))
        self.assertEqual(self.original, self.active_path.read_bytes())

    def test_multiple_candidates_become_conflicted_after_publish(self):
        first = self._upload_and_validate()
        second_bytes = make_template(Path(self.temp.name) / "second.docx", heading="Второй кандидат")
        second = self._upload_and_validate(second_bytes)
        response = self.client.post(f"/admin/document-templates/documents/{self.document_id}/candidates/{first}/publish", data={"_csrf_token": self.csrf})
        self.assertEqual(303, response.status_code)
        with self.app.app_context():
            self.assertEqual("published", get_candidate(first)["state"])
            self.assertEqual("conflict", get_candidate(second)["state"])

    def test_recovery_never_repeats_replace(self):
        candidate_uuid = self._upload_and_validate()
        class SimulatedCrash(BaseException):
            pass
        with self.app.app_context():
            document = resolve_document(self.document_id, self.root)
            def injector(point):
                if point == "after_replace":
                    raise SimulatedCrash()
            with self.assertRaises(SimulatedCrash):
                publish_candidate(document, candidate_uuid, "Редактор", failure_injector=injector)
            changed = self.active_path.read_bytes()
            outcome = recover_stale_operations(document_resolver=lambda value: resolve_document(value, self.root), now=time.time() + 180)
            self.assertEqual(1, outcome["published"])
            self.assertEqual(changed, self.active_path.read_bytes())
            self.assertEqual("published", get_candidate(candidate_uuid)["state"])

    def test_prepared_history_crash_recovers_without_replace(self):
        candidate_uuid = self._upload_and_validate()
        class SimulatedCrash(BaseException):
            pass
        with self.app.app_context():
            document = resolve_document(self.document_id, self.root)
            def injector(point):
                if point == "after_prepared_history":
                    raise SimulatedCrash()
            with self.assertRaises(SimulatedCrash):
                publish_candidate(document, candidate_uuid, "Редактор", failure_injector=injector)
            self.assertEqual(self.original, self.active_path.read_bytes())
            outcome = recover_stale_operations(document_resolver=lambda value: resolve_document(value, self.root), now=time.time() + 180)
            self.assertEqual(1, outcome["recovered"])
            self.assertEqual(self.original, self.active_path.read_bytes())

    def test_stale_validation_is_interrupted_without_automatic_retry(self):
        candidate_uuid = self._upload_and_validate()
        with self.app.app_context():
            update_candidate(candidate_uuid, {"state": "validating"})
            outcome = recover_stale_operations(document_resolver=lambda value: resolve_document(value, self.root), now=time.time() + 16 * 60)
            self.assertEqual(1, outcome["validation_interrupted"])
            self.assertEqual("validation_interrupted", get_candidate(candidate_uuid)["state"])

    def test_publish_failure_injection_points_have_deterministic_recovery(self):
        points = {
            "before_history": None,
            "after_prepared_history": "recovered",
            "after_publishing_metadata": "recovered",
            "before_replace": "recovered",
            "after_replace": "published",
            "after_active_verification": "published",
            "during_sibling_invalidation": "terminal_pending",
            "before_audit": "terminal_pending",
            "after_audit": "terminal_done",
        }

        class SimulatedCrash(BaseException):
            pass

        for point, expected in points.items():
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary); root = base / "doc_templates"; runtime = base / "runtime"
                active_path = root / "CAT" / "Комплект PL (12345)" / "Шаблон.docx"
                old_bytes = make_template(active_path, heading="Active")
                new_bytes = make_template(base / "new.docx", heading="Candidate")
                app = build_app(root, runtime)
                with app.app_context():
                    document = next(iter(build_document_whitelist(root).values()))
                    candidate = write_uploaded_candidate(io.BytesIO(new_bytes), document_id=document.document_id, source_filename="new.docx", active_filename=document.filename, active_sha=document.sha256, uploaded_by="Failure Tester", comment="Failure injection coverage")
                    validate_staged_candidate(document.document_id, candidate["candidate_uuid"], document.path)
                    sibling_uuid = None
                    if point == "during_sibling_invalidation":
                        sibling = write_uploaded_candidate(io.BytesIO(new_bytes), document_id=document.document_id, source_filename="sibling.docx", active_filename=document.filename, active_sha=document.sha256, uploaded_by="Failure Tester", comment="Sibling candidate")
                        validate_staged_candidate(document.document_id, sibling["candidate_uuid"], document.path)
                        sibling_uuid = sibling["candidate_uuid"]

                    def injector(current):
                        if current == point:
                            raise SimulatedCrash()

                    with self.assertRaises(SimulatedCrash):
                        publish_candidate(document, candidate["candidate_uuid"], "Failure Tester", failure_injector=injector)
                    before_recovery = active_path.read_bytes()
                    outcome = recover_stale_operations(document_resolver=lambda value: resolve_document(value, root), now=time.time() + 180)
                    self.assertEqual(before_recovery, active_path.read_bytes(), "Recovery must never repeat replace")
                    if expected is None:
                        self.assertEqual(old_bytes, active_path.read_bytes())
                        self.assertEqual("valid", get_candidate(candidate["candidate_uuid"])["state"])
                    elif expected in {"terminal_pending", "terminal_done"}:
                        self.assertEqual("published", get_candidate(candidate["candidate_uuid"])["state"])
                        self.assertEqual(1 if expected == "terminal_pending" else 0, outcome["audit_completed"])
                    else:
                        self.assertEqual(1, outcome[expected])
                        expected_state = "valid" if point == "after_prepared_history" else expected
                        self.assertEqual(expected_state, get_candidate(candidate["candidate_uuid"])["state"])
                    if sibling_uuid and expected in {"published", "terminal_pending", "terminal_done"}:
                        self.assertEqual("conflict", get_candidate(sibling_uuid)["state"])

    def test_rollback_failure_injection_points_have_deterministic_recovery(self):
        points = {
            "before_history": None,
            "after_prepared_history": "recovered",
            "after_publishing_metadata": "recovered",
            "before_replace": "recovered",
            "after_replace": "published",
            "after_active_verification": "published",
            "before_audit": "terminal_pending",
            "after_audit": "terminal_done",
        }

        class SimulatedCrash(BaseException):
            pass

        for point, expected in points.items():
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary); root = base / "doc_templates"; runtime = base / "runtime"
                active_path = root / "CAT" / "Комплект PL (12345)" / "Шаблон.docx"
                original = make_template(active_path, heading="Original")
                replacement = make_template(base / "replacement.docx", heading="Replacement")
                app = build_app(root, runtime)
                with app.app_context():
                    document = next(iter(build_document_whitelist(root).values()))
                    candidate = write_uploaded_candidate(io.BytesIO(replacement), document_id=document.document_id, source_filename="replacement.docx", active_filename=document.filename, active_sha=document.sha256, uploaded_by="Rollback Tester", comment="Prepare rollback history")
                    validate_staged_candidate(document.document_id, candidate["candidate_uuid"], document.path)
                    publish_candidate(document, candidate["candidate_uuid"], "Rollback Tester")
                    target = list_history(document.document_id)[0]
                    active = resolve_document(document.document_id, root)

                    def injector(current):
                        if current == point:
                            raise SimulatedCrash()

                    with self.assertRaises(SimulatedCrash):
                        rollback_version(active, target["version_uuid"], actor="Rollback Tester", reason="Failure injection rollback", expected_active_sha=active.sha256, failure_injector=injector)
                    before_recovery = active_path.read_bytes()
                    outcome = recover_stale_operations(document_resolver=lambda value: resolve_document(value, root), now=time.time() + 180)
                    self.assertEqual(before_recovery, active_path.read_bytes(), "Rollback recovery must never repeat replace")
                    if expected is None:
                        self.assertEqual(replacement, active_path.read_bytes())
                    elif expected in {"terminal_pending", "terminal_done"}:
                        self.assertEqual(1 if expected == "terminal_pending" else 0, outcome["audit_completed"])
                        self.assertEqual(original, active_path.read_bytes())
                    else:
                        self.assertEqual(1, outcome[expected])
                        self.assertEqual(original if expected == "published" else replacement, active_path.read_bytes())

    def test_upload_actual_byte_limit_removes_partial_candidate(self):
        response = self._upload(b"x" * (10 * 1024 * 1024 + 1))
        try:
            self.assertEqual(413, response.status_code)
        finally:
            response.request.environ["wsgi.input"].close()
            response.close()
        candidates = Path(self.temp.name) / "runtime/cache/document_template_center/candidates"
        self.assertFalse(candidates.exists() and any(candidates.iterdir()))

    def test_upload_request_content_length_is_rejected_early(self):
        response = self.client.open(
            f"/admin/document-templates/documents/{self.document_id}/candidates",
            method="POST",
            headers={"X-CSRF-Token": self.csrf, "Content-Type": "multipart/form-data; boundary=x"},
            environ_overrides={"CONTENT_LENGTH": str(10 * 1024 * 1024 + 256 * 1024 + 1)},
            data=b"",
        )
        self.assertEqual(413, response.status_code)

    def test_csrf_and_unknown_candidate_are_rejected(self):
        response = self.client.post(f"/admin/document-templates/documents/{self.document_id}/candidates", data={"comment": "Комментарий", "file": (io.BytesIO(self.candidate), "x.docx")}, content_type="multipart/form-data")
        self.assertEqual(403, response.status_code)
        missing = self.client.get(f"/admin/document-templates/documents/{self.document_id}/candidates/00000000-0000-4000-8000-000000000000")
        self.assertEqual(404, missing.status_code)
        malformed = self.client.get(f"/admin/document-templates/documents/{self.document_id}/candidates/not-a-uuid")
        self.assertEqual(400, malformed.status_code)

    def test_synthetic_generation_never_resolves_real_jira(self):
        upload = self._upload(); candidate_uuid = upload.headers["Location"].rstrip("/").split("/")[-1]
        with mock.patch("services.docx_service.get_jira_domain_and_token", side_effect=AssertionError("network configuration must not be used")):
            response = self.client.post(f"/admin/document-templates/documents/{self.document_id}/candidates/{candidate_uuid}/validate", data={"_csrf_token": self.csrf})
        self.assertEqual(200, response.status_code)

    def test_security_rejects_traversal_zip_and_dtd(self):
        unsafe = Path(self.temp.name) / "unsafe.docx"
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("../escape.xml", "x")
            archive.writestr("[Content_Types].xml", "<!DOCTYPE x [<!ENTITY y SYSTEM 'file:///x'>]><x>&y;</x>")
            archive.writestr("_rels/.rels", "<Relationships/>")
            archive.writestr("word/document.xml", "<document/>")
        errors, _ = inspect_docx(unsafe)
        codes = {item.code for item in errors}
        self.assertIn("zip_unsafe_path", codes); self.assertNotIn("xml_unsafe", codes)


if __name__ == "__main__":
    unittest.main()
