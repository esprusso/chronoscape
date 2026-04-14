import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = PROJECT_ROOT / "main.py"


def load_app_module(data_dir: str):
    os.environ["DATA_DIR"] = data_dir
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("POSTGRES_URL", None)
    os.environ.pop("POSTGRES_URL_NON_POOLING", None)
    module_name = f"chronoscape_main_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.module = load_app_module(self.temp_dir.name)
        self.client = TestClient(self.module.app)
        self.clients = [self.client]
        self._clear_database()

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.temp_dir.cleanup()
        os.environ.pop("DATA_DIR", None)

    def _clear_database(self):
        db = self.module.SessionLocal()
        try:
            db.query(self.module.UserSession).delete()
            db.query(self.module.BackupAudit).delete()
            db.query(self.module.LLMReflection).delete()
            db.query(self.module.Event).delete()
            db.query(self.module.Era).delete()
            db.query(self.module.UserSetting).delete()
            db.query(self.module.User).delete()
            db.commit()
        finally:
            db.close()

    def _create_user(self, email: str):
        db = self.module.SessionLocal()
        try:
            user = self.module.User(
                google_sub=f"sub-{uuid.uuid4().hex}",
                email=email,
                email_verified=True,
                display_name=email.split("@")[0],
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            return user
        finally:
            db.close()

    def _authenticated_client(self, email: str):
        user = self._create_user(email)
        db = self.module.SessionLocal()
        try:
            session = self.module.create_user_session(db, user.id)
            db.commit()
            session_id = session.session_id
            csrf_token = session.csrf_token
        finally:
            db.close()

        client = TestClient(self.module.app)
        self.clients.append(client)
        client.cookies.set(
            self.module.SESSION_COOKIE_NAME,
            self.module.sign_session_cookie_value(session_id),
        )
        client.cookies.set(self.module.CSRF_COOKIE_NAME, csrf_token)
        return client, user

    def _csrf_headers(self, client: TestClient):
        return {"X-CSRF-Token": client.cookies.get(self.module.CSRF_COOKIE_NAME)}

    def _create_sample_records(self, client: TestClient, prefix: str):
        headers = self._csrf_headers(client)
        era = client.post(
            "/api/eras",
            headers=headers,
            json={
                "name": f"{prefix} Era",
                "start_date": "2010-01-01",
                "start_date_precision": "year",
                "end_date": "2014-12-31",
                "end_date_precision": "day",
                "color_hex": "#B8C4D4",
            },
        ).json()

        first_event = client.post(
            "/api/events",
            headers=headers,
            json={
                "headline": f"{prefix} moved away",
                "explanation": f"{prefix} explanation",
                "date": "2011-02-01",
                "date_precision": "month",
                "sentiment_score": 2,
                "era_id": era["id"],
                "reflection_qa": {
                    "questions": ["What stayed with you?"],
                    "answers": ["The train platform at dusk."],
                },
            },
        ).json()
        second_event = client.post(
            "/api/events",
            headers=headers,
            json={
                "headline": f"{prefix} graduation",
                "explanation": "Caps in the air.",
                "date": "2013-06-01",
                "date_precision": "month",
                "sentiment_score": 4,
            },
        ).json()

        reorder = client.post(
            "/api/events/reorder",
            headers=headers,
            json={"ids": [second_event["id"], first_event["id"]]},
        )
        self.assertEqual(reorder.status_code, 200)
        return era, first_event, second_event

    def _backup_text(self, client: TestClient, fmt: str):
        response = client.get(f"/api/backup?format={fmt}")
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8")

    def _restore_text(self, client: TestClient, filename: str, content: str):
        return client.post(
            "/api/restore",
            headers=self._csrf_headers(client),
            files={"file": (filename, content.encode("utf-8"), "text/plain")},
        )

    def _set_auth_flow_cookie(self, client: TestClient, *, state: str, nonce: str, verifier: str):
        cookie_value = self.module.auth_flow_cookie_serializer.dumps(
            {
                "state": state,
                "nonce": nonce,
                "code_verifier": verifier,
                "next_path": "/",
            }
        )
        client.cookies.set(self.module.AUTH_FLOW_COOKIE_NAME, cookie_value)

    def test_protected_routes_require_authentication(self):
        cases = [
            ("GET", "/api/events", None),
            ("POST", "/api/events", {"headline": "x", "date": "2020-01-01", "sentiment_score": 1}),
            ("GET", "/api/eras", None),
            ("POST", "/api/eras", {"name": "era", "start_date": "2020-01-01", "end_date": "2020-12-31"}),
            ("GET", "/api/settings", None),
            ("PUT", "/api/settings", {"llm_model": "x"}),
            ("PUT", "/api/onboarding", {"welcome_dismissed": True}),
            ("GET", "/api/backup?format=csv", None),
            ("POST", "/auth/logout", None),
            ("POST", "/reflect/probe", {"headline": "x", "date": "2020-01-01", "sentiment_score": 1}),
            ("POST", "/reflect/synthesize", {"headline": "x", "date": "2020-01-01", "sentiment_score": 1, "questions": [], "answers": []}),
            ("POST", "/health/llm", {"llm_model": "x"}),
        ]

        for method, path, payload in cases:
            with self.subTest(method=method, path=path):
                if method == "GET":
                    response = self.client.get(path)
                else:
                    response = self.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 401)

    def test_database_url_normalization_prefers_sqlite_fallback_and_rewrites_postgres_urls(self):
        self.assertTrue(self.module.RESOLVED_DATABASE_URL.startswith("sqlite:///"))
        self.assertEqual(
            self.module.normalize_database_url("postgres://user:pass@host/db"),
            "postgresql+psycopg://user:pass@host/db",
        )
        self.assertEqual(
            self.module.normalize_database_url("postgresql://user:pass@host/db"),
            "postgresql+psycopg://user:pass@host/db",
        )
        self.assertEqual(
            self.module.normalize_database_url("postgresql+psycopg://user:pass@host/db"),
            "postgresql+psycopg://user:pass@host/db",
        )

    def test_mutating_routes_require_csrf(self):
        client, _ = self._authenticated_client("csrf@example.com")

        event_response = client.post(
            "/api/events",
            json={
                "headline": "No CSRF",
                "date": "2020-01-01",
                "sentiment_score": 1,
            },
        )
        logout_response = client.post("/auth/logout")
        restore_response = client.post(
            "/api/restore",
            files={"file": ("backup.md", b"hello", "text/plain")},
        )
        onboarding_response = client.put("/api/onboarding", json={"welcome_dismissed": True})

        self.assertEqual(event_response.status_code, 403)
        self.assertEqual(logout_response.status_code, 403)
        self.assertEqual(restore_response.status_code, 403)
        self.assertEqual(onboarding_response.status_code, 403)

    def test_startup_migration_adds_onboarding_columns_to_existing_user_settings_table(self):
        with tempfile.TemporaryDirectory() as migration_dir:
            db_path = Path(migration_dir, "timeline.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE user_settings (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL UNIQUE,
                        llm_base_url TEXT NOT NULL,
                        llm_model VARCHAR(255) NOT NULL,
                        llm_api_key_encrypted TEXT,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            migrated_module = load_app_module(migration_dir)
            columns = {
                column["name"]
                for column in migrated_module.inspect(migrated_module.engine).get_columns("user_settings")
            }
            self.assertIn("welcome_dismissed", columns)
            self.assertIn("first_event_completed", columns)
            self.assertIn("ai_nudge_dismissed", columns)

    def test_auth_me_includes_onboarding_state(self):
        client, _ = self._authenticated_client("auth-me@example.com")

        response = client.get("/auth/me")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("onboarding", payload)
        self.assertEqual(
            payload["onboarding"],
            {
                "welcome_dismissed": False,
                "first_event_completed": False,
                "ai_nudge_dismissed": False,
            },
        )

    def test_put_onboarding_updates_only_current_users_state(self):
        client_a, user_a = self._authenticated_client("onboarding-a@example.com")
        client_b, user_b = self._authenticated_client("onboarding-b@example.com")
        client_a.get("/auth/me")
        client_b.get("/auth/me")

        response = client_a.put(
            "/api/onboarding",
            headers=self._csrf_headers(client_a),
            json={"welcome_dismissed": True, "ai_nudge_dismissed": True},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "welcome_dismissed": True,
                "first_event_completed": False,
                "ai_nudge_dismissed": True,
            },
        )

        db = self.module.SessionLocal()
        try:
            settings_a = db.query(self.module.UserSetting).filter(self.module.UserSetting.user_id == user_a.id).first()
            settings_b = db.query(self.module.UserSetting).filter(self.module.UserSetting.user_id == user_b.id).first()
            self.assertTrue(settings_a.welcome_dismissed)
            self.assertTrue(settings_a.ai_nudge_dismissed)
            self.assertFalse(settings_a.first_event_completed)
            self.assertFalse(settings_b.welcome_dismissed)
            self.assertFalse(settings_b.ai_nudge_dismissed)
            self.assertFalse(settings_b.first_event_completed)
        finally:
            db.close()

    def test_creating_first_event_marks_onboarding_complete_only_for_current_user(self):
        client_a, user_a = self._authenticated_client("first-event-a@example.com")
        client_b, user_b = self._authenticated_client("first-event-b@example.com")
        client_a.get("/auth/me")
        client_b.get("/auth/me")

        response = client_a.post(
            "/api/events",
            headers=self._csrf_headers(client_a),
            json={
                "headline": "First marked memory",
                "date": "2020-01-01",
                "sentiment_score": 2,
            },
        )

        self.assertEqual(response.status_code, 201)

        db = self.module.SessionLocal()
        try:
            settings_a = db.query(self.module.UserSetting).filter(self.module.UserSetting.user_id == user_a.id).first()
            settings_b = db.query(self.module.UserSetting).filter(self.module.UserSetting.user_id == user_b.id).first()
            self.assertTrue(settings_a.first_event_completed)
            self.assertFalse(settings_b.first_event_completed)
        finally:
            db.close()

        auth_payload = client_a.get("/auth/me").json()
        self.assertTrue(auth_payload["onboarding"]["first_event_completed"])

    def test_cross_tenant_access_and_backup_restore_are_isolated(self):
        client_a, user_a = self._authenticated_client("alice@example.com")
        client_b, user_b = self._authenticated_client("bob@example.com")

        era_a, _, _ = self._create_sample_records(client_a, "Alice")
        era_b, event_b, _ = self._create_sample_records(client_b, "Bob")

        attach_foreign_era = client_a.post(
            "/api/events",
            headers=self._csrf_headers(client_a),
            json={
                "headline": "Bad attach",
                "date": "2022-01-01",
                "sentiment_score": 1,
                "era_id": era_b["id"],
            },
        )
        update_foreign_event = client_a.put(
            f"/api/events/{event_b['id']}",
            headers=self._csrf_headers(client_a),
            json={"headline": "Not allowed"},
        )
        delete_foreign_event = client_a.delete(
            f"/api/events/{event_b['id']}",
            headers=self._csrf_headers(client_a),
        )
        reorder_foreign = client_a.post(
            "/api/events/reorder",
            headers=self._csrf_headers(client_a),
            json={"ids": [event_b["id"]]},
        )

        self.assertEqual(attach_foreign_era.status_code, 400)
        self.assertEqual(update_foreign_event.status_code, 404)
        self.assertEqual(delete_foreign_event.status_code, 404)
        self.assertEqual(reorder_foreign.status_code, 400)

        backup_text = self._backup_text(client_a, "csv")
        self.assertIn("Alice moved away", backup_text)
        self.assertNotIn("Bob moved away", backup_text)

        client_a.post(
            "/api/events",
            headers=self._csrf_headers(client_a),
            json={
                "headline": "Alice temporary",
                "date": "2025-01-01",
                "sentiment_score": 0,
            },
        )
        client_b.post(
            "/api/events",
            headers=self._csrf_headers(client_b),
            json={
                "headline": "Bob temporary",
                "date": "2025-02-01",
                "sentiment_score": -1,
            },
        )

        restored = self._restore_text(client_a, "backup.csv", backup_text)
        self.assertEqual(restored.status_code, 200)

        events_a = client_a.get("/api/events").json()
        events_b = client_b.get("/api/events").json()
        self.assertEqual([event["headline"] for event in events_a], ["Alice graduation", "Alice moved away"])
        self.assertIn("Bob temporary", [event["headline"] for event in events_b])

        db = self.module.SessionLocal()
        try:
            audits_a = (
                db.query(self.module.BackupAudit)
                .filter(self.module.BackupAudit.user_id == user_a.id)
                .order_by(self.module.BackupAudit.created_at, self.module.BackupAudit.id)
                .all()
            )
            audits_b = (
                db.query(self.module.BackupAudit)
                .filter(self.module.BackupAudit.user_id == user_b.id)
                .all()
            )
            self.assertEqual([audit.action for audit in audits_a], ["backup", "restore"])
            self.assertEqual(audits_b, [])
        finally:
            db.close()

    def test_settings_mask_secret_and_reflection_and_health_use_current_users_settings(self):
        client_a, user_a = self._authenticated_client("reflect-a@example.com")
        client_b, user_b = self._authenticated_client("reflect-b@example.com")

        headers_a = self._csrf_headers(client_a)
        headers_b = self._csrf_headers(client_b)
        client_a.put(
            "/api/settings",
            headers=headers_a,
            json={
                "llm_base_url": "http://llm-a.local/v1",
                "llm_model": "model-a",
                "llm_api_key": "secret-a-1234",
            },
        )
        client_b.put(
            "/api/settings",
            headers=headers_b,
            json={
                "llm_base_url": "http://llm-b.local/v1",
                "llm_model": "model-b",
                "llm_api_key": "secret-b-9876",
            },
        )

        settings_response = client_a.get("/api/settings")
        self.assertEqual(settings_response.status_code, 200)
        payload = settings_response.json()
        self.assertNotIn("llm_api_key", payload)
        self.assertTrue(payload["llm_api_key_set"])
        self.assertIn("*", payload["llm_api_key_masked"])

        llm_calls = []

        class FakeLLMClient:
            def __init__(self, settings):
                llm_calls.append(settings)
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(
                        create=lambda **kwargs: SimpleNamespace(
                            choices=[SimpleNamespace(message=SimpleNamespace(content='{"questions":["Q1","Q2","Q3"]}'))]
                        )
                    )
                )

        class FakeOpenAI:
            def __init__(self, base_url, api_key):
                llm_calls.append({"llm_base_url": base_url, "llm_api_key": api_key})
                self.models = SimpleNamespace(
                    list=lambda: SimpleNamespace(data=[SimpleNamespace(id="model-a"), SimpleNamespace(id="backup-model")])
                )

        self.module.llm_client = lambda settings: FakeLLMClient(settings)
        self.module.OpenAI = FakeOpenAI

        probe = client_a.post(
            "/reflect/probe",
            json={"headline": "A memory", "date": "2020-01-01", "sentiment_score": 3},
        )
        health = client_a.post("/health/llm", json={})

        self.assertEqual(probe.status_code, 200)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(llm_calls[0]["llm_base_url"], "http://llm-a.local/v1")
        self.assertEqual(llm_calls[0]["llm_model"], "model-a")
        self.assertEqual(llm_calls[0]["llm_api_key"], "secret-a-1234")
        self.assertEqual(llm_calls[1]["llm_base_url"], "http://llm-a.local/v1")
        self.assertEqual(llm_calls[1]["llm_api_key"], "secret-a-1234")

        db = self.module.SessionLocal()
        try:
            settings_a = db.query(self.module.UserSetting).filter(self.module.UserSetting.user_id == user_a.id).first()
            settings_b = db.query(self.module.UserSetting).filter(self.module.UserSetting.user_id == user_b.id).first()
            self.assertNotEqual(settings_a.llm_base_url, settings_b.llm_base_url)
        finally:
            db.close()

    def test_legacy_claim_runs_once_for_first_login_only(self):
        db = self.module.SessionLocal()
        try:
            legacy_era = self.module.Era(
                user_id=None,
                name="Legacy Era",
                start_date=self.module.date_type(2000, 1, 1),
                end_date=self.module.date_type(2005, 1, 1),
                color_hex="#B8C4D4",
            )
            db.add(legacy_era)
            db.flush()
            legacy_event = self.module.Event(
                user_id=None,
                headline="Legacy Event",
                explanation="Before auth",
                date=self.module.date_type(2001, 1, 1),
                sentiment_score=1,
                era_id=legacy_era.id,
                sort_index=0,
            )
            db.add(legacy_event)
            db.commit()
        finally:
            db.close()

        Path(self.temp_dir.name, "settings.json").write_text(
            json.dumps(
                {
                    "llm_base_url": "http://legacy.local/v1",
                    "llm_model": "legacy-model",
                    "llm_api_key": "legacy-key",
                }
            ),
            encoding="utf-8",
        )

        async def fake_exchange(**kwargs):
            return {"id_token": "fake-token"}, {"issuer": "https://accounts.google.com", "jwks_uri": "https://example.invalid"}

        async def first_validate(token, metadata, nonce):
            self.assertEqual(nonce, "nonce-one")
            return {
                "sub": "google-user-1",
                "email": "first@example.com",
                "email_verified": True,
                "name": "First User",
                "given_name": "First",
                "family_name": "User",
                "picture": "https://example.com/first.png",
                "nonce": nonce,
            }

        self.module.exchange_google_code_for_token = fake_exchange
        self.module.validate_google_id_token = first_validate

        self._set_auth_flow_cookie(self.client, state="state-one", nonce="nonce-one", verifier="verifier-one")
        callback = self.client.get("/auth/callback?code=ok&state=state-one", follow_redirects=False)
        self.assertEqual(callback.status_code, 303)

        db = self.module.SessionLocal()
        try:
            first_user = db.query(self.module.User).filter(self.module.User.email == "first@example.com").first()
            settings = db.query(self.module.UserSetting).filter(self.module.UserSetting.user_id == first_user.id).first()
            legacy_event = db.query(self.module.Event).filter(self.module.Event.headline == "Legacy Event").first()
            legacy_era = db.query(self.module.Era).filter(self.module.Era.name == "Legacy Era").first()

            self.assertEqual(legacy_event.user_id, first_user.id)
            self.assertEqual(legacy_era.user_id, first_user.id)
            self.assertEqual(settings.llm_base_url, "http://legacy.local/v1")
            self.assertEqual(self.module.decrypt_secret(settings.llm_api_key_encrypted), "legacy-key")
        finally:
            db.close()

        db = self.module.SessionLocal()
        try:
            stray_era = self.module.Era(
                user_id=None,
                name="Unclaimed Era",
                start_date=self.module.date_type(2010, 1, 1),
                end_date=self.module.date_type(2012, 1, 1),
            )
            db.add(stray_era)
            db.commit()
        finally:
            db.close()

        async def second_validate(token, metadata, nonce):
            self.assertEqual(nonce, "nonce-two")
            return {
                "sub": "google-user-2",
                "email": "second@example.com",
                "email_verified": True,
                "name": "Second User",
                "nonce": nonce,
            }

        self.module.validate_google_id_token = second_validate
        self._set_auth_flow_cookie(self.client, state="state-two", nonce="nonce-two", verifier="verifier-two")
        callback_two = self.client.get("/auth/callback?code=ok&state=state-two", follow_redirects=False)
        self.assertEqual(callback_two.status_code, 303)

        db = self.module.SessionLocal()
        try:
            second_user = db.query(self.module.User).filter(self.module.User.email == "second@example.com").first()
            stray_era = db.query(self.module.Era).filter(self.module.Era.name == "Unclaimed Era").first()
            self.assertIsNotNone(second_user)
            self.assertIsNone(stray_era.user_id)
        finally:
            db.close()

    def test_oauth_callback_rejects_missing_state_nonce_and_pkce_failures(self):
        missing_cookie = self.client.get("/auth/callback?code=ok&state=whatever", follow_redirects=False)
        self.assertEqual(missing_cookie.status_code, 400)
        self.assertIn("flow cookie", missing_cookie.json()["detail"])

        self._set_auth_flow_cookie(self.client, state="expected", nonce="nonce", verifier="verifier")
        mismatch = self.client.get("/auth/callback?code=ok&state=wrong", follow_redirects=False)
        self.assertEqual(mismatch.status_code, 400)
        self.assertIn("state mismatch", mismatch.json()["detail"])

        async def pkce_failure(**kwargs):
            raise HTTPException(400, "PKCE exchange failed")

        async def fake_validate(token, metadata, nonce):
            return {"sub": "never", "email": "never@example.com", "nonce": nonce}

        self.module.exchange_google_code_for_token = pkce_failure
        self.module.validate_google_id_token = fake_validate

        self._set_auth_flow_cookie(self.client, state="state-three", nonce="nonce-three", verifier="verifier-three")
        pkce = self.client.get("/auth/callback?code=ok&state=state-three", follow_redirects=False)
        self.assertEqual(pkce.status_code, 400)
        self.assertIn("PKCE exchange failed", pkce.json()["detail"])

        async def fake_exchange(**kwargs):
            return {"id_token": "fake"}, {"issuer": "https://accounts.google.com", "jwks_uri": "https://example.invalid"}

        async def nonce_failure(token, metadata, nonce):
            raise HTTPException(400, "ID token nonce mismatch")

        self.module.exchange_google_code_for_token = fake_exchange
        self.module.validate_google_id_token = nonce_failure

        self._set_auth_flow_cookie(self.client, state="state-four", nonce="nonce-four", verifier="verifier-four")
        nonce = self.client.get("/auth/callback?code=ok&state=state-four", follow_redirects=False)
        self.assertEqual(nonce.status_code, 400)
        self.assertIn("nonce mismatch", nonce.json()["detail"])

    def test_logout_invalidates_server_side_session(self):
        client, user = self._authenticated_client("logout@example.com")

        response = client.post("/auth/logout", headers=self._csrf_headers(client))
        self.assertEqual(response.status_code, 200)

        db = self.module.SessionLocal()
        try:
            sessions = db.query(self.module.UserSession).filter(self.module.UserSession.user_id == user.id).all()
            self.assertEqual(sessions, [])
        finally:
            db.close()

        after_logout = client.get("/api/events")
        self.assertEqual(after_logout.status_code, 401)


if __name__ == "__main__":
    unittest.main()
