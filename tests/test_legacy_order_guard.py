"""The retained Apps Script route must not bypass verified checkout sessions."""
import os
import tempfile
import unittest
from pathlib import Path

_test_storage = tempfile.TemporaryDirectory(prefix="into-legacy-guard-")
os.environ["INTO_DATABASE"] = str(Path(_test_storage.name) / "legacy.sqlite3")
os.environ["FLASK_SECRET_KEY"] = "legacy-route-tests-only-secret"

from app import app  
from into_store import _db  # noqa: E402


class LegacyOrderGuardTests(unittest.TestCase):
    def test_home_page_and_hash_targets_render(self):
        response = app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for marker in ('id="paket"', 'id="acara"', 'id="profile"', 'href="#paket"'):
            self.assertIn(marker, html)

    def test_auth_forms_post_credentials_and_assets_are_cache_busted(self):
        html = app.test_client().get("/").get_data(as_text=True)
        self.assertIn('id="login-form" class="modal-form" method="post" action="/akun/login"', html)
        self.assertIn('id="register-form" class="modal-form" method="post" action="/akun/register"', html)
        self.assertIn('id="checkout-form" method="post" action="/api/orders"', html)
        self.assertIn('name="csrf_token" value="', html)
        self.assertIn("app.js?v=20261015-4", html)
        self.assertIn("ice-blue.css?v=20261015-4", html)
        self.assertIn('href="/mulai/Betigo"', html)

    def test_old_get_credentials_are_stripped_and_never_authenticate(self):
        client = app.test_client()
        response = client.get("/?email=unknown%40example.test&password=example-secret")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/")
        self.assertNotIn("example-secret", response.headers["Location"])
        with client.session_transaction() as user_session:
            self.assertNotIn("user_id", user_session)

    def test_post_login_fallback_returns_errors_without_credentials_in_url(self):
        client = app.test_client()
        csrf = client.get("/api/auth/csrf").get_json()["csrf_token"]
        response = client.post("/akun/login", data={
            "csrf_token": csrf,
            "email": "unknown@example.test",
            "password": "wrong-secret-password",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        page = client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn('id="auth-modal" aria-labelledby="auth-title" open', page)
        self.assertIn("Email belum terdaftar", page)
        self.assertNotIn("wrong-secret-password", page)

    def test_registration_link_opens_registration_view_without_javascript(self):
        page = app.test_client().get("/akun/register", follow_redirects=True).get_data(as_text=True)
        self.assertIn('id="auth-modal" aria-labelledby="auth-title" open', page)
        self.assertIn('id="register-form" class="modal-form" method="post" action="/akun/register"', page)
        self.assertNotIn('id="register-form" class="modal-form" method="post" action="/akun/register" hidden', page)

    def test_legacy_sheet_route_requires_verified_session_and_csrf(self):
        with app.app_context(), _db() as db:
            db.execute("""INSERT INTO users(full_name,school,phone,email,password_hash,is_verified,created_at)
                VALUES(?,?,?,?,?,0,?)""", ("Ayu Maharani", "SMA 1", "+6281234567890", "legacy@example.test",
                                             "unused-test-hash", "2026-01-01T00:00:00+00:00"))
            user_id = db.execute("SELECT id FROM users WHERE email=?", ("legacy@example.test",)).fetchone()["id"]
        client = app.test_client()
        with client.session_transaction() as user_session:
            user_session["user_id"] = user_id
            user_session["csrf_token"] = "legacy-csrf"
        unverified = client.post("/api/pemesanan", data={})
        self.assertEqual(unverified.status_code, 403)

        with app.app_context(), _db() as db:
            db.execute("UPDATE users SET is_verified=1 WHERE id=?", (user_id,))
        with client.session_transaction() as user_session:
            user_session["user_id"] = user_id
            user_session["csrf_token"] = "legacy-csrf"
        missing_csrf = client.post("/api/pemesanan", data={})
        self.assertEqual(missing_csrf.status_code, 400)
        allowed_to_validate = client.post("/api/pemesanan", data={"csrf_token": "legacy-csrf", "paket": "invalid"})
        self.assertEqual(allowed_to_validate.status_code, 400)
        self.assertEqual(allowed_to_validate.get_json()["message"], "Pilih paket yang tersedia.")


if __name__ == "__main__":
    unittest.main()
