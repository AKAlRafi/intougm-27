import hashlib
import json
import smtplib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

from into_store import _db, init_store, store


ROOT = Path(__file__).resolve().parents[1]
SERVER_KEY = "SB-Mid-server-test-key"
CLIENT_KEY = "SB-Mid-client-test-key"


class StoreFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        valid_until = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        self.app = Flask(__name__, template_folder=str(ROOT / "templates"))
        self.app.secret_key = "test-only-session-secret"
        self.app.config.update(
            TESTING=True,
            INTO_DATABASE=str(Path(self.temp.name) / "test.sqlite3"),
            PUBLIC_BASE_URL="https://into.example.test",
            SMTP_HOST="smtp.test.invalid",
            SMTP_PORT="587",
            SMTP_USER="test-user",
            SMTP_PASSWORD="test-password",
            MAIL_FROM="tickets@example.test",
            MIDTRANS_SERVER_KEY=SERVER_KEY,
            MIDTRANS_CLIENT_KEY=CLIENT_KEY,
            MIDTRANS_IS_PRODUCTION="false",
            INTO_VOUCHERS_JSON=json.dumps({
                "TESTHEMAT": {"discount_amount": 10000, "expires_at": valid_until, "active": True, "usage_limit": 1},
            }),
            WHATSAPP_GROUP_URL="https://chat.whatsapp.com/test-invite",
            INTO_CONTACT_PERSONS="Miftah & Steven",
        )
        self.app.register_blueprint(store)

        @self.app.get("/")
        def home():
            return "home"

        init_store(self.app)
        self.client = self.app.test_client()
        self.email_sender = patch("into_store._send_email")
        self.mock_send_email = self.email_sender.start()
        self.addCleanup(self.email_sender.stop)

    def tearDown(self):
        self.temp.cleanup()

    def post(self, path, payload):
        token = self.client.get("/api/auth/csrf").get_json()["csrf_token"]
        return self.client.post(path, json=payload, headers={"X-CSRF-Token": token})

    def registration_payload(self, **overrides):
        payload = {
            "full_name": "Ayu Maharani",
            "school": "SMA Negeri 1 Palembang",
            "phone": "+6281234567890",
            "email": "ayu@example.test",
            "password": "a-strong-test-password",
            "confirm_password": "a-strong-test-password",
            "consent": "on",
        }
        payload.update(overrides)
        return payload

    def register_user(self, **overrides):
        response = self.post("/api/auth/register", self.registration_payload(**overrides))
        self.assertEqual(response.status_code, 201, response.get_json())
        message = self.mock_send_email.call_args.args[2]
        verify_url = message.rsplit(" ", 1)[-1]
        return verify_url

    def login(self, email="ayu@example.test", password="a-strong-test-password"):
        response = self.post("/api/auth/login", {"email": email, "password": password})
        return response

    def register_and_login(self, **overrides):
        verify_url = self.register_user(**overrides)
        verified = self.client.get(verify_url.replace("https://into.example.test", ""))
        self.assertEqual(verified.status_code, 200)
        response = self.login(email=overrides.get("email", "ayu@example.test"))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.mock_send_email.reset_mock()
        return response.get_json()

    def participant(self, number=1, email=None):
        return {
            "full_name": f"Peserta {number}",
            "school": "SMA Negeri 1 Palembang",
            "email": email or f"peserta{number}@example.test",
            "phone": f"+62812345678{number:02d}",
        }

    def mock_midtrans(self):
        response = Mock()
        response.status_code = 201
        response.json.return_value = {"token": "snap-test-token", "redirect_url": "https://payments.example.test/snap"}
        return patch("into_store.requests.post", return_value=response)

    def create_order(self, package="Dewekan", participants=None, **extra):
        size = {"Dewekan": 1, "Betigo": 3, "Belimo": 5}[package]
        payload = {
            "package": package,
            "participants": participants
            or [self.participant(i) for i in range(1, size + 1)],
        }
        payload.update(extra)
        with self.mock_midtrans() as midtrans:
            response = self.post("/api/orders", payload)
            return response, midtrans

    def notify(
        self,
        order_id,
        transaction_status,
        amount,
        status_code="200",
        fraud_status=None,
        signature_override=None,
    ):
        gross_amount = f"{amount:.2f}"
        body = {
            "order_id": order_id,
            "status_code": status_code,
            "gross_amount": gross_amount,
            "transaction_status": transaction_status,
            "signature_key": hashlib.sha512(f"{order_id}{status_code}{gross_amount}{SERVER_KEY}".encode()).hexdigest(),
        }
        if fraud_status is not None:
            body["fraud_status"] = fraud_status
        if signature_override is not None:
            body["signature_key"] = signature_override
        return self.client.post("/api/midtrans/notification", json=body)

    def order_row(self, order_id):
        with self.app.app_context(), _db() as db:
            return db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

    def test_unknown_wrong_password_unverified_and_verified_login(self):
        self.assertEqual(self.login("missing@example.test", "a-strong-test-password").status_code, 401)
        verify_url = self.register_user()
        self.assertEqual(self.login(password="wrong-password").status_code, 401)
        unverified = self.login()
        self.assertEqual(unverified.status_code, 403)
        self.assertTrue(unverified.get_json()["unverified"])
        self.assertFalse(self.client.get("/api/auth/me").get_json()["authenticated"])
        verify_response = self.client.get(verify_url.replace("https://into.example.test", ""))
        self.assertEqual(verify_response.status_code, 200)
        self.assertEqual(self.login().status_code, 200)

    def test_short_auth_api_aliases_keep_database_and_verification_checks(self):
        unknown = self.post("/api/login", {"email": "missing@example.test", "password": "some-password"})
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(unknown.get_json(), {"message": "Email belum terdaftar"})
        registered = self.post("/api/register", self.registration_payload())
        self.assertEqual(registered.status_code, 201, registered.get_json())
        unverified = self.post("/api/login", {
            "email": "ayu@example.test",
            "password": "a-strong-test-password",
        })
        self.assertEqual(unverified.status_code, 403)
        self.assertTrue(unverified.get_json()["unverified"])

    def test_html_login_fallback_still_blocks_unverified_accounts(self):
        token = self.client.get("/api/auth/csrf").get_json()["csrf_token"]
        registration = self.client.post("/akun/register", data={
            **self.registration_payload(),
            "csrf_token": token,
        })
        self.assertEqual(registration.status_code, 302)
        self.assertNotIn("password", registration.headers["Location"])
        token = self.client.get("/api/auth/csrf").get_json()["csrf_token"]
        login = self.client.post("/akun/login", data={
            "csrf_token": token,
            "email": "ayu@example.test",
            "password": "a-strong-test-password",
        })
        self.assertEqual(login.status_code, 302)
        self.assertNotIn("password", login.headers["Location"])
        with self.client.session_transaction() as user_session:
            self.assertNotIn("user_id", user_session)
            self.assertEqual(user_session["auth_view"], "login")

    def test_html_login_fallback_authenticates_only_verified_account(self):
        verify_url = self.register_user()
        verification = self.client.get(verify_url.replace("https://into.example.test", ""))
        self.assertEqual(verification.status_code, 200)
        token = self.client.get("/api/auth/csrf").get_json()["csrf_token"]
        response = self.client.post("/akun/login", data={
            "csrf_token": token,
            "email": "ayu@example.test",
            "password": "a-strong-test-password",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/#profile")
        with self.client.session_transaction() as user_session:
            self.assertIn("user_id", user_session)

    def test_registration_rejects_malformed_duplicate_and_mismatched_data(self):
        invalid = self.post("/api/auth/register", self.registration_payload(email="not-an-email"))
        self.assertEqual(invalid.status_code, 400)
        mismatch = self.post("/api/auth/register", self.registration_payload(confirm_password="different-password"))
        self.assertEqual(mismatch.status_code, 400)
        no_consent = self.post("/api/auth/register", self.registration_payload(consent=""))
        self.assertEqual(no_consent.status_code, 400)
        self.register_user()
        duplicate = self.post("/api/auth/register", self.registration_payload())
        self.assertEqual(duplicate.status_code, 409)

    def test_expired_verification_token_does_not_verify_account(self):
        verify_url = self.register_user()
        with self.app.app_context(), _db() as db:
            db.execute("UPDATE users SET verify_expires_at=? WHERE email=?",
                       ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "ayu@example.test"))
        response = self.client.get(verify_url.replace("https://into.example.test", ""))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.login().status_code, 403)

    def test_unverified_forged_session_cannot_use_protected_routes(self):
        self.register_user()
        with self.app.app_context(), _db() as db:
            user_id = db.execute("SELECT id FROM users WHERE email=?", ("ayu@example.test",)).fetchone()["id"]
        with self.client.session_transaction() as user_session:
            user_session["user_id"] = user_id
            user_session["csrf_token"] = "test-csrf"
        response = self.client.post("/api/vouchers/validate", json={"code": "TESTHEMAT", "package": "Dewekan"},
                                    headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/auth/me").get_json(), {"authenticated": False})

    def test_group_size_is_enforced_and_amount_is_server_calculated(self):
        self.register_and_login()
        with self.mock_midtrans() as midtrans:
            missing = self.post("/api/orders", {"package": "Betigo", "participants": [self.participant(1)]})
            self.assertEqual(missing.status_code, 400)
            self.assertEqual(midtrans.call_count, 0)
        payload = {"package": "Dewekan", "amount": 1, "participants": [self.participant(1)]}
        with self.mock_midtrans() as midtrans:
            response = self.post("/api/orders", payload)
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(response.get_json()["amount"], 48000)
        self.assertEqual(midtrans.call_args.kwargs["json"]["transaction_details"]["gross_amount"], 48000)

    def test_expired_and_quota_exhausted_vouchers_never_discount(self):
        self.register_and_login()
        expired = self.post("/api/vouchers/validate", {"package": "Dewekan", "code": "HOTSALESMANSA"}).get_json()
        self.assertEqual(expired["message"], "Voucher sudah kedaluwarsa.")
        self.assertEqual(expired["discount_amount"], 0)
        self.assertFalse(expired["valid"])
        active = self.post("/api/vouchers/validate", {"package": "Dewekan", "code": "TESTHEMAT"}).get_json()
        self.assertTrue(active["valid"])
        self.assertEqual(active["total"], 38000)
        ordered, _midtrans = self.create_order(voucher_code="TESTHEMAT")
        self.assertEqual(ordered.status_code, 201, ordered.get_json())
        exhausted = self.post("/api/vouchers/validate", {"package": "Dewekan", "code": "TESTHEMAT"}).get_json()
        self.assertEqual(exhausted["message"], "Kuota voucher sudah habis.")
        self.assertEqual(exhausted["discount_amount"], 0)

    def test_signed_settlement_is_idempotent_and_issues_unique_tickets(self):
        self.register_and_login()
        response, _midtrans = self.create_order("Betigo")
        self.assertEqual(response.status_code, 201, response.get_json())
        order_id = response.get_json()["order_id"]
        amount = response.get_json()["amount"]
        forged = self.notify(order_id, "settlement", amount, signature_override="bad")
        self.assertEqual(forged.status_code, 401)
        self.assertEqual(self.order_row(order_id)["status"], "PENDING")
        wrong_amount = self.notify(order_id, "settlement", amount + 1)
        self.assertEqual(wrong_amount.status_code, 400)
        self.assertEqual(self.order_row(order_id)["status"], "PENDING")
        paid = self.notify(order_id, "settlement", amount)
        self.assertEqual(paid.status_code, 200, paid.get_json())
        self.assertEqual(self.order_row(order_id)["status"], "PAID")
        self.assertEqual(self.mock_send_email.call_count, 3)
        with self.app.app_context(), _db() as db:
            people = db.execute(
                "SELECT ticket_code,email_sent_at FROM order_participants WHERE order_id=?",
                (order_id,),
            ).fetchall()
        tickets = [person["ticket_code"] for person in people]
        self.assertEqual(len(set(tickets)), 3)
        self.assertTrue(all(ticket and len(ticket) == 6 for ticket in tickets))
        self.assertTrue(all(person["email_sent_at"] for person in people))
        recipients = [call.args[0] for call in self.mock_send_email.call_args_list]
        self.assertEqual(recipients, ["ayu@example.test", "peserta2@example.test", "peserta3@example.test"])
        self.assertIn("https://chat.whatsapp.com/test-invite", self.mock_send_email.call_args.args[3])
        self.assertIn("Miftah &amp; Steven", self.mock_send_email.call_args.args[3])
        repeat = self.notify(order_id, "settlement", amount)
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(self.mock_send_email.call_count, 3)

    def test_concurrent_settlement_notifications_do_not_duplicate_tickets_or_email(self):
        self.register_and_login()
        response, _midtrans = self.create_order()
        order_id, amount = response.get_json()["order_id"], response.get_json()["amount"]
        gross_amount, status_code = f"{amount:.2f}", "200"
        notification = {
            "order_id": order_id,
            "status_code": status_code,
            "gross_amount": gross_amount,
            "transaction_status": "settlement",
            "signature_key": hashlib.sha512(f"{order_id}{status_code}{gross_amount}{SERVER_KEY}".encode()).hexdigest(),
        }

        def send_notification():
            with self.app.test_client() as client:
                return client.post("/api/midtrans/notification", json=notification).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(lambda _index: send_notification(), range(2)))
        self.assertTrue(all(status in (200, 500) for status in statuses), statuses)
        self.assertEqual(self.order_row(order_id)["status"], "PAID")
        self.assertTrue(self.order_row(order_id)["email_sent_at"])
        self.assertEqual(self.mock_send_email.call_count, 1)
        with self.app.app_context(), _db() as db:
            codes = [
                row[0]
                for row in db.execute(
                    "SELECT ticket_code FROM order_participants WHERE order_id=?",
                    (order_id,),
                )
            ]
        self.assertEqual(len(codes), 1)
        self.assertTrue(codes[0])

    def test_email_failure_keeps_paid_state_and_authentic_retry_finishes_dispatch(self):
        self.register_and_login()
        response, _midtrans = self.create_order()
        order_id, amount = response.get_json()["order_id"], response.get_json()["amount"]
        self.mock_send_email.side_effect = [smtplib.SMTPException("temporary SMTP outage"), None]
        with patch.object(self.app.logger, "exception") as log_exception:
            first = self.notify(order_id, "settlement", amount)
            log_exception.assert_called_once()
        self.assertEqual(first.status_code, 500)
        self.assertEqual(self.order_row(order_id)["status"], "PAID")
        retry = self.notify(order_id, "settlement", amount)
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(self.order_row(order_id)["email_sent_at"])
        self.assertEqual(self.mock_send_email.call_count, 2)

    def test_capture_needs_accept_and_terminal_status_never_regresses(self):
        self.register_and_login()
        response, _midtrans = self.create_order()
        order_id, amount = response.get_json()["order_id"], response.get_json()["amount"]
        challenge = self.notify(order_id, "capture", amount, fraud_status="challenge")
        self.assertEqual(challenge.status_code, 200)
        self.assertEqual(self.order_row(order_id)["status"], "PENDING")
        cancelled = self.notify(order_id, "cancel", amount)
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(self.order_row(order_id)["status"], "CANCELLED")
        stale_pending = self.notify(order_id, "pending", amount)
        self.assertEqual(stale_pending.status_code, 200)
        self.assertEqual(self.order_row(order_id)["status"], "CANCELLED")
        self.assertEqual(self.mock_send_email.call_count, 0)

    def test_order_status_is_private_to_owner_and_email_runs_only_after_paid(self):
        self.register_and_login()
        response, _midtrans = self.create_order()
        order_id, amount = response.get_json()["order_id"], response.get_json()["amount"]
        with self.app.app_context():
            from into_store import _create_tickets_and_email
            self.assertTrue(_create_tickets_and_email(order_id))
        self.assertEqual(self.mock_send_email.call_count, 0)
        self.assertEqual(self.client.get(f"/api/orders/{order_id}").status_code, 200)
        self.assertEqual(self.notify(order_id, "settlement", amount).status_code, 200)
        self.assertEqual(self.client.get(f"/api/orders/{order_id}").get_json()["email_sent"], True)
        csrf_token = self.client.get("/api/auth/csrf").get_json()["csrf_token"]
        self.client.post(
            "/api/auth/logout",
            json={},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.register_and_login(email="budi@example.test", full_name="Budi Santoso")
        self.assertEqual(self.client.get(f"/api/orders/{order_id}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
