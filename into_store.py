"""Authentication, ticket ordering, Midtrans Snap, and email delivery."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
import string
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

store = Blueprint("store", __name__)
PACKAGES = {
    "Single": {"size": 1, "price": 50000},
    "Child-Free/Duo": {"size": 2, "price": 80000},
    "Keluarga Berencana/Trio": {"size": 3, "price": 145000},
    "Keluarga Besar/Circle": {"size": 5, "price": 225000},
}
LEGACY_PACKAGE_ALIASES = {
    "Dewekan": "Single", "Betigo": "Keluarga Berencana/Trio", "Belimo": "Keluarga Besar/Circle"
}
PACKAGE_PHASES = ("Early Bird", "Presale", "Normal", "Flashsale", "Sosialisasi")
PHASE_DEFAULT_PRICES = {
    "Early Bird": {"Single": 38000, "Child-Free/Duo": 0, "Keluarga Berencana/Trio": 108000, "Keluarga Besar/Circle": 0},
    "Presale": {"Single": 45000, "Child-Free/Duo": 80000, "Keluarga Berencana/Trio": 0, "Keluarga Besar/Circle": 0},
    "Normal": {"Single": 50000, "Child-Free/Duo": 0, "Keluarga Berencana/Trio": 145000, "Keluarga Besar/Circle": 225000},
    "Flashsale": {"Single": 50000, "Child-Free/Duo": 80000, "Keluarga Berencana/Trio": 145000, "Keluarga Besar/Circle": 225000},
    "Sosialisasi": {"Single": 38000, "Child-Free/Duo": 0, "Keluarga Berencana/Trio": 105000, "Keluarga Besar/Circle": 0},
}
JAKARTA = ZoneInfo("Asia/Jakarta")
TICKET_ALPHABET = string.ascii_uppercase + string.digits
EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ().-]{7,22}$")
_PASSWORD_HASHER = PasswordHasher(type=Type.ID, time_cost=3, memory_cost=65536, parallelism=2)
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("not-a-real-user-password")
PENDING_REGISTRATION_TTL = timedelta(minutes=20)
PENDING_REGISTRATION_TTL_MINUTES = int(PENDING_REGISTRATION_TTL.total_seconds() // 60)


def _hash_password(password):
    """Create a salted Argon2id password hash for a newly registered user."""
    return _PASSWORD_HASHER.hash(password)


def _verify_password(encoded_hash, password):
    """Verify Argon2id hashes and transparently upgrade older Werkzeug hashes."""
    if encoded_hash.startswith("$argon2id$"):
        try:
            valid = _PASSWORD_HASHER.verify(encoded_hash, password)
            return valid, _PASSWORD_HASHER.check_needs_rehash(encoded_hash)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False, False

    # Compatibility for accounts created before the Argon2id migration.
    try:
        valid = check_password_hash(encoded_hash, password)
    except (ValueError, TypeError):
        return False, False
    return valid, valid


@contextmanager
def _sqlite_connection(path):
    # Startup may overlap with another app process or a SQLite GUI editor.
    # Wait for a short-lived writer instead of failing immediately.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def _db():
    conn = sqlite3.connect(current_app.config["INTO_DATABASE"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _json_object():
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _valid_email(value):
    if not isinstance(value, str) or len(value) > 254 or not EMAIL_RE.fullmatch(value):
        return False
    local, domain = value.rsplit("@", 1)
    return (
        len(local) <= 64
        and not local.startswith(".")
        and not local.endswith(".")
        and ".." not in local
        and all(len(label) <= 63 for label in domain.split("."))
    )


def _valid_phone(value):
    return bool(PHONE_RE.fullmatch(value))


def _valid_whatsapp_group(value):
    try:
        parsed = urlsplit(value.strip())
        return (
            parsed.scheme == "https"
            and parsed.hostname == "chat.whatsapp.com"
            and len(parsed.path.strip("/")) >= 8
            and "replace-with" not in parsed.path.lower()
        )
    except (AttributeError, ValueError):
        return False


def init_store(app):
    Path(app.config["INTO_DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    with _sqlite_connection(app.config["INTO_DATABASE"]) as db:
        db.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, school TEXT NOT NULL,
          phone TEXT NOT NULL, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
          password_hash TEXT NOT NULL, is_verified INTEGER NOT NULL DEFAULT 0,
          role TEXT NOT NULL DEFAULT 'user', referral_code TEXT UNIQUE COLLATE NOCASE,
          verify_token_hash TEXT, verify_expires_at TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_users (
          id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, school TEXT NOT NULL,
          phone TEXT NOT NULL, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
          password_hash TEXT NOT NULL, verify_token_hash TEXT UNIQUE,
          expires_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS vouchers (
          code TEXT PRIMARY KEY COLLATE NOCASE, discount_amount INTEGER NOT NULL,
          expires_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          referral_type TEXT NOT NULL DEFAULT 'other',
          usage_limit INTEGER
        );
        CREATE TABLE IF NOT EXISTS ticket_pricing_settings (
          id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL DEFAULT 'manual',
          manual_phase TEXT NOT NULL DEFAULT 'Normal'
        );
        CREATE TABLE IF NOT EXISTS ticket_phase_prices (
          phase TEXT NOT NULL, package TEXT NOT NULL, price INTEGER NOT NULL,
          starts_at TEXT, ends_at TEXT, PRIMARY KEY(phase,package)
        );
        CREATE TABLE IF NOT EXISTS voucher_discount_settings (
          referral_type TEXT NOT NULL CHECK(referral_type IN ('school','ba')),
          package TEXT NOT NULL,
          discount_amount INTEGER CHECK(discount_amount IS NULL OR discount_amount >= 0),
          PRIMARY KEY(referral_type, package)
        );
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          package TEXT NOT NULL, amount INTEGER NOT NULL, discount_amount INTEGER NOT NULL DEFAULT 0,
          voucher_code TEXT, status TEXT NOT NULL DEFAULT 'PENDING',
          midtrans_token TEXT, midtrans_redirect_url TEXT,
          created_at TEXT NOT NULL, paid_at TEXT, email_sent_at TEXT,
          sheets_synced_at TEXT, sheets_claimed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ba_missions (
          mission_key TEXT PRIMARY KEY, stage TEXT NOT NULL, title TEXT NOT NULL,
          sort_order INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ba_referral_codes (
          code TEXT PRIMARY KEY COLLATE NOCASE,
          ba_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_migrations (
          name TEXT PRIMARY KEY, applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ba_mission_submissions (
          id INTEGER PRIMARY KEY, ba_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          mission_key TEXT NOT NULL REFERENCES ba_missions(mission_key),
          file_name TEXT NOT NULL, mime_type TEXT NOT NULL, evidence BLOB NOT NULL,
          status TEXT NOT NULL DEFAULT 'PENDING', submitted_at TEXT NOT NULL,
          reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT, review_note TEXT,
          UNIQUE(ba_user_id, mission_key)
        );
        CREATE TABLE IF NOT EXISTS order_participants (
          id INTEGER PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
          full_name TEXT NOT NULL, school TEXT NOT NULL, email TEXT NOT NULL,
          phone TEXT NOT NULL, ticket_code TEXT UNIQUE, email_sent_at TEXT,
          email_claimed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_order_participants_order ON order_participants(order_id);
        CREATE INDEX IF NOT EXISTS idx_ba_submissions_status ON ba_mission_submissions(status, submitted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_ba_submissions_user ON ba_mission_submissions(ba_user_id, mission_key);
        CREATE INDEX IF NOT EXISTS idx_ba_referral_codes_user ON ba_referral_codes(ba_user_id, active);
        CREATE INDEX IF NOT EXISTS idx_pending_users_expiry ON pending_users(expires_at);
        """)
        db.execute("INSERT OR IGNORE INTO ticket_pricing_settings(id,mode,manual_phase) VALUES(1,'manual','Normal')")
        for phase in PACKAGE_PHASES:
            for package_name, package in PACKAGES.items():
                db.execute("""INSERT OR IGNORE INTO ticket_phase_prices(phase,package,price)
                    VALUES(?,?,?)""", (phase, package_name, PHASE_DEFAULT_PRICES[phase][package_name]))
        for referral_type in ("school", "ba"):
            for package_name in PACKAGES:
                db.execute("""INSERT OR IGNORE INTO voucher_discount_settings
                    (referral_type,package,discount_amount) VALUES(?,?,NULL)""",
                    (referral_type, package_name))
        participant_columns = {row[1] for row in db.execute("PRAGMA table_info(order_participants)")}
        if "email_sent_at" not in participant_columns:
            db.execute("ALTER TABLE order_participants ADD COLUMN email_sent_at TEXT")
        if "email_claimed_at" not in participant_columns:
            db.execute("ALTER TABLE order_participants ADD COLUMN email_claimed_at TEXT")
        voucher_columns = {row[1] for row in db.execute("PRAGMA table_info(vouchers)")}
        if "usage_limit" not in voucher_columns:
            db.execute("ALTER TABLE vouchers ADD COLUMN usage_limit INTEGER")
        user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "role" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        if "referral_code" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN referral_code TEXT")
        if "referral_type" not in voucher_columns:
            db.execute("ALTER TABLE vouchers ADD COLUMN referral_type TEXT NOT NULL DEFAULT 'other'")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code ON users(referral_code) WHERE referral_code IS NOT NULL")
        legacy_code_migration = db.execute(
            "SELECT 1 FROM app_migrations WHERE name='ba_self_created_codes_v1'"
        ).fetchone()
        if not legacy_code_migration:
            # Preserve generated referral codes for historical order attribution, but let
            # each existing BA choose their own active code once under the new flow.
            old_codes = db.execute(
                "SELECT id,referral_code FROM users WHERE role='ba' AND referral_code IS NOT NULL"
            ).fetchall()
            migration_time = datetime.now(timezone.utc).isoformat()
            for old_code in old_codes:
                db.execute("""INSERT OR IGNORE INTO ba_referral_codes(code,ba_user_id,active,created_at)
                    VALUES(?,?,0,?)""", (old_code["referral_code"], old_code["id"], migration_time))
                db.execute("UPDATE users SET referral_code=NULL WHERE id=?", (old_code["id"],))
            db.execute("INSERT INTO app_migrations(name,applied_at) VALUES('ba_self_created_codes_v1',?)",
                       (migration_time,))
        missions = [
            ("stage-1-feed", "Tahap 1", "Unggah feed perkenalan InTO UGM", 1),
            ("stage-1-story", "Tahap 1", "Bagikan poster di Instagram Story", 2),
            ("stage-2-wa", "Tahap 2", "Sebarkan informasi ke 5 grup WhatsApp sekolah", 3),
            ("stage-2-friends", "Tahap 2", "Ajak 3 teman mendaftar", 4),
            ("stage-3-report", "Tahap 3", "Kirim bukti publikasi ke panitia", 5),
        ]
        db.executemany(
            "INSERT OR IGNORE INTO ba_missions(mission_key,stage,title,sort_order) VALUES(?,?,?,?)",
            missions,
        )
        order_columns = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
        if "sheets_synced_at" not in order_columns:
            db.execute("ALTER TABLE orders ADD COLUMN sheets_synced_at TEXT")
        if "sheets_claimed_at" not in order_columns:
            db.execute("ALTER TABLE orders ADD COLUMN sheets_claimed_at TEXT")

        # Move old, unverified registrations out of the main account table.
        # Existing rows with orders are retained so foreign-key relationships
        # and historical transactions are never disturbed.
        now = datetime.now(timezone.utc)
        pending_expiry_limit = now + PENDING_REGISTRATION_TTL
        legacy_unverified = db.execute("""SELECT u.* FROM users u
            WHERE u.is_verified=0
            AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id=u.id)""").fetchall()
        for legacy in legacy_unverified:
            old_expiry = None
            try:
                if legacy["verify_expires_at"]:
                    old_expiry = datetime.fromisoformat(legacy["verify_expires_at"])
                    if old_expiry.tzinfo is None:
                        old_expiry = old_expiry.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                old_expiry = None
            # A legacy emailed token may continue to work for at most the new
            # staging window. Expired or absent tokens can be resent.
            usable_old_token = bool(legacy["verify_token_hash"] and old_expiry and old_expiry > now)
            expiry = min(old_expiry, pending_expiry_limit) if usable_old_token else pending_expiry_limit
            db.execute("""INSERT OR IGNORE INTO pending_users
                (full_name,school,phone,email,password_hash,verify_token_hash,expires_at,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (legacy["full_name"], legacy["school"], legacy["phone"], legacy["email"],
                 legacy["password_hash"], legacy["verify_token_hash"] if usable_old_token else None,
                 expiry.isoformat(), legacy["created_at"]))
            db.execute("DELETE FROM users WHERE id=? AND is_verified=0", (legacy["id"],))

        db.execute("DELETE FROM pending_users WHERE expires_at<=?", (now.isoformat(),))
        try:
            vouchers = json.loads(app.config.get("INTO_VOUCHERS_JSON", "{}"))
            if not isinstance(vouchers, dict):
                raise ValueError("Voucher config harus berupa object JSON")
            for code, info in vouchers.items():
                if not isinstance(info, dict):
                    raise ValueError(f"Konfigurasi voucher {code} tidak valid")
                discount = int(info["discount_amount"])
                limit = info.get("usage_limit")
                limit = None if limit is None else int(limit)
                referral_type = str(info.get("referral_type", "other")).lower()
                expires = datetime.fromisoformat(str(info["expires_at"]))
                if (not str(code).strip() or discount < 0 or (limit is not None and limit < 0)
                        or referral_type not in {"school", "other"}):
                    raise ValueError(f"Konfigurasi voucher {code} tidak valid")
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                db.execute("""INSERT INTO vouchers(code,discount_amount,expires_at,active,referral_type,usage_limit)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET
                    discount_amount=excluded.discount_amount, expires_at=excluded.expires_at,
                    active=excluded.active, referral_type=excluded.referral_type, usage_limit=excluded.usage_limit""",
                    (str(code).strip().upper(), discount, expires.isoformat(),
                     int(bool(info.get("active", True))), referral_type, limit))
        except (ValueError, TypeError, KeyError, AttributeError):
            app.logger.exception("INTO_VOUCHERS_JSON is invalid; voucher configuration skipped")
        # This example code is always expired, including on databases from earlier deployments.
        db.execute("""INSERT INTO vouchers(code,discount_amount,expires_at,active,usage_limit)
            VALUES(?,?,?,?,0) ON CONFLICT(code) DO UPDATE SET discount_amount=0,
            expires_at=excluded.expires_at,active=0,usage_limit=0""",
            ("HOTSALESMANSA", 0, "2020-01-01T00:00:00+00:00", 0))


def ticket_pricing(db, now=None):
    """Return the currently effective phase and authoritative server-side prices."""
    settings = db.execute("SELECT mode,manual_phase FROM ticket_pricing_settings WHERE id=1").fetchone()
    if not settings:
        phase, mode = "Normal", "manual"
    else:
        phase, mode = settings["manual_phase"], settings["mode"]
    manual_phase = phase
    now = now or datetime.now(timezone.utc)
    schedule_active = mode != "automatic"
    if mode == "automatic":
        rows = db.execute("""SELECT phase,starts_at,ends_at FROM ticket_phase_prices
            WHERE starts_at IS NOT NULL AND ends_at IS NOT NULL""").fetchall()
        active = set()
        for row in rows:
            try:
                start = datetime.fromisoformat(row["starts_at"])
                end = datetime.fromisoformat(row["ends_at"])
            except (TypeError, ValueError):
                continue
            if start <= now < end:
                active.add(row["phase"])
        if len(active) == 1:
            phase = next(iter(active))
            schedule_active = True
    prices = {name: PHASE_DEFAULT_PRICES[phase][name] for name in PACKAGES}
    for row in db.execute("SELECT package,price FROM ticket_phase_prices WHERE phase=?", (phase,)):
        if row["package"] in PACKAGES:
            prices[row["package"]] = row["price"]
    return {"phase": phase, "mode": mode, "manual_phase": manual_phase,
            "prices": prices, "schedule_active": schedule_active}


def _phase_datetime_local(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JAKARTA)
    return parsed.astimezone(timezone.utc).isoformat()


def _csrf_valid():
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    return bool(supplied and expected and hmac.compare_digest(supplied, expected))


def csrf_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not _csrf_valid():
            return jsonify(message="Sesi kamu sudah berakhir. Muat ulang halaman, lalu coba lagi."), 400
        return fn(*args, **kwargs)
    return wrapped


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        if not user_id:
            return jsonify(message="Masuk dulu untuk melanjutkan."), 401
        with _db() as db:
            user = db.execute(
                "SELECT is_verified,full_name,role FROM users WHERE id=?", (user_id,)
            ).fetchone()
        if not user or not user["is_verified"]:
            session.clear()
            return jsonify(message="Verifikasi emailmu dulu. Buka tautan yang kami kirim untuk mengaktifkan akun."), 403
        session["user_name"] = user["full_name"]
        session["role"] = user["role"]
        return fn(*args, **kwargs)
    return wrapped


def _send_email(to, subject, text_body, html_body):
    host = current_app.config.get("SMTP_HOST", "").strip()
    if not host:
        raise RuntimeError("Email belum dikonfigurasi. Isi SMTP_HOST dan pengaturan SMTP.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = current_app.config.get("MAIL_FROM", current_app.config.get("SMTP_USER", ""))
    msg["To"] = to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(host, int(current_app.config.get("SMTP_PORT", "587")), timeout=20) as smtp:
        smtp.starttls()
        username = current_app.config.get("SMTP_USER", "")
        password = current_app.config.get("SMTP_PASSWORD", "")
        if username:
            if host.lower() == "smtp.gmail.com":
                # Google displays App Passwords with optional grouping spaces.
                password = "".join(password.split())
                if len(password) != 16:
                    raise RuntimeError(
                        "SMTP_PASSWORD untuk Gmail harus berupa App Password Google 16 karakter, "
                        "bukan password login Gmail biasa."
                    )
            try:
                smtp.login(username, password)
            except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected) as exc:
                if host.lower() == "smtp.gmail.com":
                    raise RuntimeError(
                        "Gmail menolak autentikasi SMTP. Pastikan SMTP_USER benar, "
                        "2-Step Verification aktif, dan SMTP_PASSWORD berisi App Password yang masih berlaku."
                    ) from exc
                raise
        smtp.send_message(msg)


def public_base_url():
    """Return the canonical public origin, ignoring a stale localhost setting on production."""
    configured = current_app.config.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    hostname = request.host.split(":", 1)[0].strip("[]").lower()
    localhost_hosts = {"localhost", "127.0.0.1", "::1"}
    pythonanywhere_hosts = {
        "tickyinto2027.pythonanywhere.com",
        "into2027.pythonanywhere.com",  # legacy deployment hostname
    }

    # The deployment can accidentally inherit PUBLIC_BASE_URL=http://localhost:5000
    # from a copied local .env. Prefer the known public host in that case (or when
    # the setting still names the other legacy PythonAnywhere hostname).
    if hostname in pythonanywhere_hosts:
        configured_host = configured.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        if not configured or configured_host in localhost_hosts | pythonanywhere_hosts:
            return f"https://{hostname}"
    if configured:
        if not configured.startswith(("https://", "http://")):
            raise RuntimeError("PUBLIC_BASE_URL harus menggunakan http:// atau https://")
        return configured
    if hostname not in localhost_hosts:
        raise RuntimeError("PUBLIC_BASE_URL wajib diisi untuk pengiriman tautan pada deployment.")
    return request.url_root.rstrip("/")


def _app_url(endpoint, **kwargs):
    return public_base_url() + url_for(endpoint, **kwargs)


@store.get("/api/auth/csrf")
def csrf_token():
    token = session.setdefault("csrf_token", secrets.token_urlsafe(32))
    return jsonify(csrf_token=token)


def _create_account(data):
    name, school = str(data.get("full_name", "")).strip(), str(data.get("school", "")).strip()
    phone, email = str(data.get("phone", "")).strip(), str(data.get("email", "")).strip().lower()
    password, confirm = str(data.get("password", "")), str(data.get("confirm_password", ""))
    if not all((name, school, phone, email, password, confirm)):
        return "Lengkapi semua kolom pendaftaran dulu, ya.", 400
    if len(name) > 120 or len(school) > 140 or len(phone) > 24 or len(password) < 5 or len(password) > 256:
        return "Periksa kembali datamu. Kata sandi harus berisi minimal 5 karakter.", 400
    if not _valid_email(email):
        return "Format emailnya belum tepat. Coba periksa lagi.", 400
    if not _valid_phone(phone):
        return "Nomor WhatsApp atau teleponnya belum tepat. Coba periksa lagi.", 400
    if password != confirm:
        return "Kata sandinya belum sama. Periksa kolom konfirmasi.", 400
    if data.get("consent") != "on":
        return "Centang persetujuan penggunaan data untuk melanjutkan.", 400
    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = (now + PENDING_REGISTRATION_TTL).isoformat()
    with _db() as db:
        if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return "Email ini sudah terdaftar. Coba masuk atau gunakan email lain.", 400
    password_hash = _hash_password(password)
    try:
        with _db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM pending_users WHERE expires_at<=?", (now.isoformat(),))
            registered = db.execute(
                "SELECT 1 FROM users WHERE email=?", (email,)
            ).fetchone()
            if registered:
                return "Email ini sudah terdaftar. Coba masuk atau gunakan email lain.", 400
            db.execute(
                """INSERT INTO pending_users
                (full_name,school,phone,email,password_hash,verify_token_hash,expires_at,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(email) DO UPDATE SET
                  full_name=excluded.full_name, school=excluded.school,
                  phone=excluded.phone, password_hash=excluded.password_hash,
                  verify_token_hash=excluded.verify_token_hash,
                  expires_at=excluded.expires_at, created_at=excluded.created_at""",
                (
                    name,
                    school,
                    phone,
                    email,
                    password_hash,
                    hashlib.sha256(raw_token.encode()).hexdigest(),
                    expires,
                    now.isoformat(),
                ),
            )
    except sqlite3.IntegrityError:
        return "Email ini sudah terdaftar. Coba masuk atau gunakan email lain.", 400
    try:
        link = _app_url("store.verify_email_api_get", token=raw_token)
        _send_email(
            email,
            "Aktifkan akun InTO UGM 2027",
            (
                f"Halo {name}, buka tautan ini dalam {PENDING_REGISTRATION_TTL_MINUTES} menit "
                f"untuk mengaktifkan akunmu: {link}"
            ),
            render_template("emails/verify.html", name=name, verify_url=link),
        )
    except RuntimeError as exc:
        current_app.logger.exception("Verification email delivery failed")
        return (
            f"Pendaftaran tersimpan sementara selama {PENDING_REGISTRATION_TTL_MINUTES} menit, "
            "tetapi email verifikasi belum terkirim. Coba kirim ulang dari halaman masuk. "
            "Jika masih belum masuk, hubungi panitia."
        ), 503
    except (smtplib.SMTPException, OSError):
        current_app.logger.exception("Verification email delivery failed")
        return (
            f"Pendaftaran tersimpan sementara selama {PENDING_REGISTRATION_TTL_MINUTES} menit, "
            "tetapi email verifikasi belum terkirim. Coba kirim ulang dari halaman masuk. "
            "Jika masih belum masuk, hubungi panitia."
        ), 503
    return (
        "Akunmu hampir siap. Cek email untuk mengaktifkannya sebelum masuk."
    ), 201


@store.post("/api/register")
@store.post("/api/auth/register")
@csrf_required
def register():
    message, status = _create_account(_json_object())
    return jsonify(message=message), status


@store.get("/akun/<view>")
def auth_page(view):
    if view not in {"login", "register"}:
        return redirect(url_for("home"))
    session["auth_view"] = view
    return redirect(url_for("home"))


@store.post("/akun/register")
@csrf_required
def register_form():
    message, status = _create_account(request.form)
    session["auth_view"] = "register"
    session["auth_notice"] = message
    session["auth_notice_state"] = "success" if status == 201 else "error"
    session["auth_email"] = request.form.get("email", "").strip()[:254]
    return redirect(url_for("home", _anchor="daftar"))


@store.post("/api/auth/resend-verification")
@csrf_required
def resend_verification():
    data = _json_object()
    email = str(data.get("email", "")).strip().lower()
    now = datetime.now(timezone.utc)
    raw_token = secrets.token_urlsafe(32)
    expires = (now + PENDING_REGISTRATION_TTL).isoformat()
    expired_pending = False
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        expired_pending = (
            db.execute(
                "SELECT 1 FROM pending_users WHERE email=? AND expires_at<=?",
                (email, now.isoformat()),
            ).fetchone()
            is not None
        )
        db.execute("DELETE FROM pending_users WHERE expires_at<=?", (now.isoformat(),))
        user = db.execute(
            "SELECT id,full_name FROM pending_users WHERE email=? AND expires_at>?",
            (email, now.isoformat()),
        ).fetchone()
        if user:
            db.execute(
                "UPDATE pending_users SET verify_token_hash=?,expires_at=? WHERE id=?",
                (hashlib.sha256(raw_token.encode()).hexdigest(), expires, user["id"]),
            )
        else:
            # Compatibility for old unverified accounts tied to an existing
            # order; new registrations never enter this branch.
            legacy_user = db.execute(
                "SELECT id,full_name FROM users WHERE email=? AND is_verified=0",
                (email,),
            ).fetchone()
            if legacy_user:
                user = legacy_user
                db.execute(
                    "UPDATE users SET verify_token_hash=?,verify_expires_at=? WHERE id=?",
                    (hashlib.sha256(raw_token.encode()).hexdigest(), expires, user["id"]),
                )
    if user:
        try:
            link = _app_url("store.verify_email_api_get", token=raw_token)
            _send_email(
                email,
                "Aktifkan akun InTO UGM 2027",
                f"Buka tautan ini dalam {PENDING_REGISTRATION_TTL_MINUTES} menit untuk mengaktifkan akunmu: {link}",
                render_template("emails/verify.html", name=user["full_name"], verify_url=link),
            )
        except RuntimeError as exc:
            current_app.logger.exception("Verification resend failed")
            return jsonify(message="Email verifikasi belum terkirim. Coba lagi sebentar atau hubungi panitia."), 503
        except (smtplib.SMTPException, OSError):
            current_app.logger.exception("Verification resend failed")
            return jsonify(message="Email verifikasi belum terkirim. Coba lagi sebentar atau hubungi panitia."), 503
    elif expired_pending:
        return jsonify(message="Tautan pendaftaran sudah kedaluwarsa. Daftar ulang untuk meminta tautan baru."), 400
    return jsonify(message="Jika email terdaftar dan belum diverifikasi, tautan aktivasi sudah kami kirim.")


def _verify_registration_token(raw_token):
    if not isinstance(raw_token, str) or not raw_token or len(raw_token) > 256:
        return None
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM pending_users WHERE expires_at<=?", (now.isoformat(),))
        pending = db.execute(
            "SELECT * FROM pending_users WHERE verify_token_hash=?", (token_hash,)
        ).fetchone()
        if pending:
            try:
                expiry = datetime.fromisoformat(pending["expires_at"])
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                expiry = now - timedelta(seconds=1)
            if expiry <= now:
                db.execute("DELETE FROM pending_users WHERE id=?", (pending["id"],))
                return None
            try:
                cursor = db.execute(
                    """INSERT INTO users
                    (full_name,school,phone,email,password_hash,is_verified,
                     verify_token_hash,verify_expires_at,created_at)
                    VALUES(?,?,?,?,?,1,NULL,NULL,?)""",
                    (
                        pending["full_name"],
                        pending["school"],
                        pending["phone"],
                        pending["email"],
                        pending["password_hash"],
                        pending["created_at"],
                    ),
                )
            except sqlite3.IntegrityError:
                # A main-table account may have been created through a legacy
                # path while this registration was pending. Do not overwrite it.
                db.execute("DELETE FROM pending_users WHERE id=?", (pending["id"],))
                return None
            db.execute(
                "DELETE FROM pending_users WHERE id=? AND verify_token_hash=?",
                (pending["id"], token_hash),
            )
            user_id = cursor.lastrowid
            return {"id": user_id, "name": pending["full_name"], "email": pending["email"]}

        # Preserve old verification links only for pre-refactor rows that have
        # transaction history and therefore cannot be moved out of users.
        legacy = db.execute(
            "SELECT id,full_name,email,verify_expires_at FROM users "
            "WHERE verify_token_hash=? AND is_verified=0",
            (token_hash,),
        ).fetchone()
        if not legacy or not legacy["verify_expires_at"]:
            return None
        try:
            expiry = datetime.fromisoformat(legacy["verify_expires_at"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
        if expiry <= now:
            db.execute(
                "UPDATE users SET verify_token_hash=NULL,verify_expires_at=NULL WHERE id=?",
                (legacy["id"],),
            )
            return None
        db.execute(
            "UPDATE users SET is_verified=1,verify_token_hash=NULL,verify_expires_at=NULL WHERE id=?",
            (legacy["id"],),
        )
        return {"id": legacy["id"], "name": legacy["full_name"], "email": legacy["email"]}


@store.post("/api/verify-email")
@store.post("/api/auth/verify-email")
def verify_email_api():
    token = _json_object().get("token", "")
    user = _verify_registration_token(token)
    if not user:
        return jsonify(
            message=(
                "Tautan ini sudah tidak berlaku. Daftar ulang untuk meminta tautan verifikasi yang baru."
            )
        ), 400
    return jsonify(message="Emailmu sudah terverifikasi. Sekarang kamu bisa masuk."), 200


@store.get("/api/verify-email")
def verify_email_api_get():
    user = _verify_registration_token(request.args.get("token", ""))
    if not user:
        return render_template("verify_result.html", success=False), 400
    return render_template("verify_result.html", success=True)


@store.get("/verifikasi-email/<token>")
def verify_email(token):
    return redirect(url_for("store.verify_email_api_get", token=token), code=302)


@store.post("/api/login")
@store.post("/api/auth/login")
@csrf_required
def login():
    data = _json_object()
    email, password = str(data.get("email", "")).strip().lower(), str(data.get("password", ""))
    if len(email) > 254:
        return jsonify(message="Email atau kata sandinya belum cocok. Coba periksa lagi."), 401
    with _db() as db:
        user = db.execute(
            "SELECT id,full_name,email,password_hash,is_verified,role FROM users WHERE email=?",
            (email,),
        ).fetchone()
    if not user:
        if len(password) <= 256:
            _verify_password(_DUMMY_PASSWORD_HASH, password)
        return jsonify(message="Email atau kata sandinya belum cocok. Coba periksa lagi."), 401
    if not user["is_verified"]:
        return jsonify(message="Email atau kata sandinya belum cocok. Coba periksa lagi."), 401
    if len(password) > 256:
        return jsonify(message="Email atau kata sandinya belum cocok. Coba periksa lagi."), 401
    password_hash = user["password_hash"]
    password_matches, needs_upgrade = _verify_password(password_hash, password)
    if not password_matches:
        return jsonify(message="Email atau kata sandinya belum cocok. Coba periksa lagi."), 401
    if needs_upgrade:
        with _db() as db:
            db.execute("UPDATE users SET password_hash=? WHERE id=? AND password_hash=?",
                       (_hash_password(password), user["id"], password_hash))
    session.clear()
    session["user_id"] = user["id"]
    session["user_name"] = user["full_name"]
    session["role"] = user["role"]
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True
    profile = {"id": user["id"], "name": user["full_name"], "email": user["email"], "role": user["role"]}
    return jsonify(message="Berhasil masuk.", name=user["full_name"], user=profile,
                   csrf_token=session["csrf_token"])


@store.post("/akun/login")
@csrf_required
def login_form():
    email = str(request.form.get("email", "")).strip().lower()
    password = str(request.form.get("password", ""))
    if len(email) > 254:
        session["auth_notice"] = "Akun tidak ditemukan atau belum terdaftar"
        session["auth_email"] = email[:254]
        session["auth_view"] = "login"
        return redirect(url_for("home"))
    with _db() as db:
        user = db.execute(
            "SELECT id,full_name,email,password_hash,is_verified,role "
            "FROM users WHERE email=?",
            (email,),
        ).fetchone()
    if not user:
        if len(password) <= 256:
            _verify_password(_DUMMY_PASSWORD_HASH, password)
        session["auth_notice"] = "Akun tidak ditemukan atau belum terdaftar"
        session["auth_email"] = email
        session["auth_view"] = "login"
        return redirect(url_for("home"))
    if not user["is_verified"]:
        session["auth_notice"] = "Akun tidak ditemukan atau belum terdaftar"
        session["auth_email"] = email
        session["auth_view"] = "login"
        return redirect(url_for("home"))
    if len(password) > 256:
        password_matches = False
    else:
        password_hash = user["password_hash"]
        password_matches, needs_upgrade = _verify_password(password_hash, password)
    if not password_matches:
        session["auth_notice"] = "Email atau kata sandinya belum cocok. Coba periksa lagi."
        session["auth_email"] = email
        session["auth_view"] = "login"
        return redirect(url_for("home"))
    pending_package = session.get("pending_package")
    pending_package = LEGACY_PACKAGE_ALIASES.get(pending_package, pending_package)
    session.clear()
    session["user_id"] = user["id"]
    session["user_name"] = user["full_name"]
    session["role"] = user["role"]
    session["csrf_token"] = secrets.token_urlsafe(32)
    if pending_package in PACKAGES:
        session["pending_package"] = pending_package
    session.permanent = True
    return redirect(url_for("home", _anchor="profile"))


@store.get("/mulai/<path:package_name>")
def start_package(package_name):
    package_name = LEGACY_PACKAGE_ALIASES.get(package_name, package_name)
    if package_name not in PACKAGES:
        return redirect(url_for("home", _anchor="paket"))
    session["pending_package"] = package_name
    user_id = session.get("user_id")
    if user_id:
        with _db() as db:
            user = db.execute("SELECT is_verified FROM users WHERE id=?", (user_id,)).fetchone()
        if user and user["is_verified"]:
            return redirect(url_for("home", _anchor="paket"))
        session.clear()
        session["pending_package"] = package_name
    session["auth_view"] = "login"
    return redirect(url_for("home", _anchor="paket"))


@store.post("/api/auth/logout")
@csrf_required
def logout():
    session.clear()
    return jsonify(message="Kamu sudah keluar dari akun.")


@store.get("/api/auth/me")
def me():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify(authenticated=False)
    with _db() as db:
        user = db.execute(
            "SELECT email,full_name,is_verified,role FROM users WHERE id=?", (user_id,)
        ).fetchone()
    if not user or not user["is_verified"]:
        session.clear()
        return jsonify(authenticated=False)
    session["user_name"] = user["full_name"]
    session["role"] = user["role"]
    return jsonify(authenticated=True, name=user["full_name"], email=user["email"], role=user["role"])


def _voucher(db, code, subtotal, package_name):
    code = code.strip().upper()
    if not code:
        return "", 0, "", True
    ba_code = db.execute("SELECT ba_user_id FROM ba_referral_codes WHERE code=? AND active=1", (code,)).fetchone()
    if ba_code:
        configured = db.execute("""SELECT discount_amount FROM voucher_discount_settings
            WHERE referral_type='ba' AND package=?""", (package_name,)).fetchone()
        discount = min(subtotal, configured[0]) if configured and configured[0] is not None else 0
        return code, discount, "Kode referral BA berhasil dipakai.", True
    row = db.execute(
        "SELECT discount_amount,expires_at,active,usage_limit,referral_type FROM vouchers WHERE code=?",
        (code,),
    ).fetchone()
    if not row:
        return code, 0, "Kode voucher belum bisa digunakan. Periksa lagi kodenya.", False
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        current_app.logger.error("Invalid expiry date stored for voucher %s", code)
        return code, 0, "Kode voucher belum bisa digunakan. Periksa lagi kodenya.", False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if not row["active"] or expires <= datetime.now(timezone.utc):
        return code, 0, "Masa berlaku kode voucher ini sudah habis.", False
    if row["usage_limit"] is not None:
        pending_cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        used = db.execute("""SELECT COUNT(*) FROM orders WHERE voucher_code=? AND
            (status='PAID' OR (status='PENDING' AND created_at>=?))""", (code, pending_cutoff)).fetchone()[0]
        if used >= row["usage_limit"]:
            return code, 0, "Kuota kode voucher ini sudah terpakai semua.", False
    configured = None
    if row["referral_type"] in {"school", "ba"}:
        configured = db.execute("""SELECT discount_amount FROM voucher_discount_settings
            WHERE referral_type=? AND package=?""", (row["referral_type"], package_name)).fetchone()
    configured_amount = configured[0] if configured else None
    discount = min(subtotal, configured_amount if configured_amount is not None else row["discount_amount"])
    return code, discount, "Kode voucher berhasil dipakai.", True


@store.post("/api/vouchers/validate")
@login_required
@csrf_required
def validate_voucher():
    data = _json_object()
    requested_package = str(data.get("package", ""))
    package_name = LEGACY_PACKAGE_ALIASES.get(requested_package, requested_package)
    package = PACKAGES.get(package_name)
    if not package:
        return jsonify(message="Paket ini belum tersedia. Pilih paket yang ada di halaman tiket."), 400
    with _db() as db:
        pricing = ticket_pricing(db)
        base_price = pricing["prices"][package_name]
        if base_price <= 0:
            return jsonify(message="Paket ini tidak tersedia pada fase harga yang sedang aktif."), 400
        code, discount, message, valid = _voucher(db, str(data.get("code", "")), base_price, package_name)
    return jsonify(
        code=code,
        discount_amount=discount,
        total=base_price - discount,
        base_price=base_price,
        phase=pricing["phase"],
        message=message,
        valid=valid,
    )


@store.post("/api/orders")
@login_required
@csrf_required
def create_order():
    data = _json_object()
    server_key = current_app.config.get("MIDTRANS_SERVER_KEY", "").strip()
    client_key = current_app.config.get("MIDTRANS_CLIENT_KEY", "").strip()
    if not server_key or not client_key:
        return jsonify(
            message=(
                "Pembelian belum bisa dibuka. Panitia sedang menyiapkan pembayaran; coba lagi nanti."
            )
        ), 503
    if not current_app.config.get("SMTP_HOST", "").strip() or not current_app.config.get("MAIL_FROM", "").strip():
        return jsonify(
            message=(
                "Pemesanan belum bisa diproses saat ini. Coba lagi nanti atau hubungi panitia."
            )
        ), 503
    if not _valid_whatsapp_group(current_app.config.get("WHATSAPP_GROUP_URL", "")):
        return jsonify(message="Grup WhatsApp peserta belum tersedia. Hubungi panitia untuk bantuan."), 503
    try:
        finish_url = _app_url("home") + "#paket"
    except RuntimeError as exc:
        current_app.logger.exception("Public application URL is not configured")
        return jsonify(message="Pemesanan belum bisa diproses saat ini. Coba lagi nanti atau hubungi panitia."), 503
    package_name = str(data.get("package", ""))
    package_name = LEGACY_PACKAGE_ALIASES.get(package_name, package_name)
    package = PACKAGES.get(package_name)
    if not package:
        return jsonify(message="Paket ini belum tersedia. Pilih paket lain, ya."), 400
    participants = data.get("participants")
    if not isinstance(participants, list) or len(participants) != package["size"]:
        return jsonify(message=f"Paket {package_name} membutuhkan data untuk {package['size']} peserta."), 400
    clean = []
    for i, person in enumerate(participants, 1):
        if not isinstance(person, dict):
            return jsonify(message=f"Periksa kembali data peserta {i}."), 400
        row = {key: str(person.get(key, "")).strip() for key in ("full_name", "school", "email", "phone")}
        if (not all(row.values()) or len(row["full_name"]) > 120 or len(row["school"]) > 140
                or not _valid_email(row["email"]) or len(row["phone"]) > 24 or not _valid_phone(row["phone"])):
            return jsonify(message=f"Lengkapi nama, sekolah, email, dan WhatsApp peserta {i} dengan benar."), 400
        clean.append(row)
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        pricing = ticket_pricing(db)
        base_price = pricing["prices"][package_name]
        if base_price <= 0:
            return jsonify(message="Paket ini tidak tersedia pada fase harga yang sedang aktif."), 400
        owner = db.execute("SELECT email FROM users WHERE id=? AND is_verified=1", (session["user_id"],)).fetchone()
        if not owner:
            return jsonify(message="Sesi masukmu sudah berakhir. Masuk lagi untuk melanjutkan."), 401
        # Only the account email may receive the receipt for the registering purchaser.
        clean[0]["email"] = owner["email"]
        code, discount, voucher_message, _voucher_valid = _voucher(
            db, str(data.get("voucher_code", "")), base_price, package_name
        )
        if code and not _voucher_valid:
            return jsonify(message=voucher_message or "Kode voucher tidak bisa digunakan."), 400
        amount = base_price - discount
        order_id = "INTO-" + secrets.token_hex(8).upper()
        created = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO orders(id,user_id,package,amount,discount_amount,voucher_code,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                order_id,
                session["user_id"],
                package_name,
                amount,
                discount,
                code or None,
                "PENDING",
                created,
            ),
        )
        db.executemany(
            "INSERT INTO order_participants(order_id,full_name,school,email,phone) "
            "VALUES(?,?,?,?,?)",
            [
                (order_id, p["full_name"], p["school"], p["email"], p["phone"])
                for p in clean
            ],
        )
    is_production = (
        current_app.config.get("MIDTRANS_IS_PRODUCTION", "false").lower() == "true"
    )
    base = "https://app.midtrans.com" if is_production else "https://app.sandbox.midtrans.com"
    try:
        user = None
        with _db() as db:
            user = db.execute("SELECT full_name,email,phone FROM users WHERE id=?", (session["user_id"],)).fetchone()
        payload = {
            "transaction_details": {"order_id": order_id, "gross_amount": amount},
            "item_details": [
                {
                    "id": package_name.upper(),
                    "price": amount,
                    "quantity": 1,
                    "name": f"Paket InTO UGM 2027 {package_name}",
                }
            ],
            "customer_details": {"first_name": user["full_name"], "email": user["email"], "phone": user["phone"]},
            "callbacks": {"finish": finish_url},
            "expiry": {"unit": "hour", "duration": 24},
        }
        auth = requests.auth.HTTPBasicAuth(server_key, "")
        response = requests.post(base + "/snap/v1/transactions", json=payload, auth=auth, timeout=20)
        if not 200 <= response.status_code < 300:
            response.raise_for_status()
            raise requests.HTTPError("Unexpected Midtrans response status", response=response)
        response.raise_for_status()
        result = response.json()
        if not result.get("token"):
            raise ValueError("Midtrans response tidak berisi token Snap")
    except requests.HTTPError as exc:
        response = exc.response
        status_code = response.status_code if response is not None else 0
        midtrans_messages = []
        if response is not None:
            try:
                error_payload = response.json()
                raw_messages = error_payload.get("error_messages", []) if isinstance(error_payload, dict) else []
                if isinstance(raw_messages, list):
                    midtrans_messages = [str(item)[:160] for item in raw_messages[:3]]
                elif raw_messages:
                    midtrans_messages = [str(raw_messages)[:160]]
            except ValueError:
                midtrans_messages = []
        current_app.logger.error("Midtrans Snap rejected order %s (HTTP %s): %s",
                                 order_id, status_code,
                                 "; ".join(midtrans_messages) or "no safe error detail returned")
        # A definite client error means no Snap transaction exists; release this voucher reservation.
        if response is not None and 400 <= status_code < 500:
            with _db() as db:
                db.execute("UPDATE orders SET status='FAILED' WHERE id=? AND status='PENDING'", (order_id,))
        if status_code in (401, 403):
            return jsonify(
                message=(
                    "Pembayaran belum bisa dibuat. Coba lagi nanti atau hubungi panitia."
                )
            ), 502
        if status_code == 400:
            return jsonify(
                message=(
                    "Pembayaran belum bisa dibuat. Periksa data pesananmu atau hubungi panitia."
                )
            ), 502
        return jsonify(
            message=(
                "Pembayaran belum bisa dibuat. Periksa koneksi internetmu, lalu coba lagi."
            )
        ), 502
    except (requests.RequestException, ValueError) as exc:
        # Network/time-out failures are ambiguous: keep PENDING so an authentic late callback can still settle it.
        current_app.logger.exception("Midtrans Snap transaction creation outcome is unknown")
        return jsonify(message="Pembayaran belum bisa dibuat. Coba lagi sebentar."), 502
    with _db() as db:
        db.execute(
            "UPDATE orders SET midtrans_token=?,midtrans_redirect_url=? WHERE id=?",
            (result["token"], result.get("redirect_url", ""), order_id),
        )
    return jsonify(
        order_id=order_id,
        amount=amount,
        discount_amount=discount,
        voucher_message=voucher_message,
        snap_token=result["token"],
        snap_url=base + "/snap/snap.js",
        snap_client_key=client_key,
    ), 201


def _create_tickets_and_email(order_id):
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        order = db.execute(
            "SELECT o.*,u.full_name AS buyer_name FROM orders o "
            "JOIN users u ON u.id=o.user_id WHERE o.id=?",
            (order_id,),
        ).fetchone()
        if not order or order["status"] != "PAID":
            return True
        if order["email_sent_at"]:
            return True
        people = db.execute("SELECT * FROM order_participants WHERE order_id=?", (order_id,)).fetchall()
        for p in people:
            if not p["ticket_code"]:
                assigned = False
                for _ in range(20):
                    code = "".join(secrets.choice(TICKET_ALPHABET) for _ in range(6))
                    try:
                        result = db.execute(
                            "UPDATE order_participants SET ticket_code=? "
                            "WHERE id=? AND ticket_code IS NULL",
                            (code, p["id"]),
                        )
                        assigned = result.rowcount == 1
                        break
                    except sqlite3.IntegrityError:
                        continue
                if not assigned:
                    current_app.logger.error("Unable to allocate unique ticket code for participant %s", p["id"])
                    return False
        people = db.execute("SELECT * FROM order_participants WHERE order_id=?", (order_id,)).fetchall()
        if not people:
            current_app.logger.error("Paid order %s has no participants", order_id)
            return False
    group_url = current_app.config.get("WHATSAPP_GROUP_URL", "")
    contact = current_app.config.get("INTO_CONTACT_PERSONS", "Miftah & Steven")
    failed = False
    for person in people:
        if person["email_sent_at"]:
            continue
        now = datetime.now(timezone.utc)
        stale_claim = (now - timedelta(minutes=2)).isoformat()
        with _db() as db:
            claimed = db.execute(
                """UPDATE order_participants SET email_claimed_at=?
                WHERE id=? AND email_sent_at IS NULL
                AND (email_claimed_at IS NULL OR email_claimed_at < ?)""",
                (now.isoformat(), person["id"], stale_claim),
            ).rowcount == 1
        if not claimed:
            continue
        try:
            _send_email(
                person["email"],
                "Tiket resmi InTO UGM 2027 · " + person["ticket_code"],
                (
                    f"Halo {person['full_name']}, pembayaranmu sudah diterima. "
                    f"Kode tiket InTO UGM 2027-mu: {person['ticket_code']}. "
                    f"Grup WhatsApp peserta: {group_url}. Butuh bantuan? Hubungi {contact}."
                ),
                render_template(
                    "emails/ticket.html",
                    person=person,
                    order=order,
                    group_url=group_url,
                    contact=contact,
                ),
            )
            with _db() as db:
                db.execute(
                    "UPDATE order_participants SET email_sent_at=?,email_claimed_at=NULL "
                    "WHERE id=? AND email_sent_at IS NULL",
                    (datetime.now(timezone.utc).isoformat(), person["id"]),
                )
        except (smtplib.SMTPException, OSError, RuntimeError):
            failed = True
            with _db() as db:
                db.execute(
                    "UPDATE order_participants SET email_claimed_at=NULL "
                    "WHERE id=? AND email_sent_at IS NULL",
                    (person["id"],),
                )
            current_app.logger.exception("Ticket email failed for order %s", order_id)
    with _db() as db:
        remaining = db.execute(
            "SELECT COUNT(*) FROM order_participants "
            "WHERE order_id=? AND email_sent_at IS NULL",
            (order_id,),
        ).fetchone()[0]
    if not failed and remaining == 0:
        with _db() as db:
            db.execute("UPDATE orders SET email_sent_at=? WHERE id=? AND email_sent_at IS NULL",
                       (datetime.now(timezone.utc).isoformat(), order_id))
        return True
    return False


def _sync_paid_order_to_sheets(order_id):
    """Deliver a confirmed payment to Sheets once, with safe retry after failure."""
    webhook = current_app.config.get("INTO_SHEETS_WEBHOOK_URL", "").strip()
    token = current_app.config.get("INTO_SHEETS_TOKEN", "").strip()
    if not webhook or not token:
        current_app.logger.error("Paid order %s cannot sync: Google Sheets webhook is not configured", order_id)
        return False

    now = datetime.now(timezone.utc)
    stale_claim = (now - timedelta(minutes=2)).isoformat()
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        order = db.execute("""SELECT o.*,u.full_name AS buyer_name,u.email AS buyer_email,
            u.phone AS buyer_phone FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id=?""",
                           (order_id,)).fetchone()
        if not order or order["status"] != "PAID":
            return False
        if order["sheets_synced_at"]:
            return True
        claimed = db.execute("""UPDATE orders SET sheets_claimed_at=?
            WHERE id=? AND status='PAID' AND sheets_synced_at IS NULL
            AND (sheets_claimed_at IS NULL OR sheets_claimed_at < ?)""",
                             (now.isoformat(), order_id, stale_claim)).rowcount == 1
        if not claimed:
            return False
        participants = db.execute("""SELECT full_name,school,email,phone,ticket_code
            FROM order_participants WHERE order_id=? ORDER BY id""", (order_id,)).fetchall()
        payload = {
            "type": "paid_order",
            "token": token,
            "status": "PAID",
            "order": {
                "id": order["id"],
                "package": order["package"],
                "amount": order["amount"],
                "discount_amount": order["discount_amount"],
                "voucher_code": order["voucher_code"] or "",
                "paid_at": order["paid_at"] or now.isoformat(),
                "buyer_name": order["buyer_name"],
                "buyer_email": order["buyer_email"],
                "buyer_phone": order["buyer_phone"],
            },
            "participants": [dict(person) for person in participants],
        }

    synced = False
    try:
        response = requests.post(webhook, json=payload, timeout=20, allow_redirects=True)
        result = response.json()
        synced = bool(response.ok and isinstance(result, dict) and result.get("ok"))
        if not synced:
            current_app.logger.error("Google Sheets rejected paid order %s (HTTP %s): %s",
                                     order_id, response.status_code,
                                     str(result.get("message", "respons tidak berhasil"))[:240]
                                     if isinstance(result, dict) else "respons bukan object JSON")
    except (requests.RequestException, ValueError):
        current_app.logger.exception("Google Sheets delivery failed for paid order %s", order_id)
    with _db() as db:
        if synced:
            db.execute("UPDATE orders SET sheets_synced_at=?,sheets_claimed_at=NULL WHERE id=? AND status='PAID'",
                       (datetime.now(timezone.utc).isoformat(), order_id))
        else:
            db.execute("UPDATE orders SET sheets_claimed_at=NULL WHERE id=? AND sheets_synced_at IS NULL",
                       (order_id,))
    return synced


@store.post("/api/midtrans/notification")
def midtrans_notification():
    body = _json_object()
    order_id, status_code = str(body.get("order_id", "")), str(body.get("status_code", ""))
    gross_amount, signature = str(body.get("gross_amount", "")), str(body.get("signature_key", ""))
    server_key = current_app.config.get("MIDTRANS_SERVER_KEY", "")
    expected = hashlib.sha512((order_id + status_code + gross_amount + server_key).encode()).hexdigest()
    if not server_key or not hmac.compare_digest(expected, signature):
        return jsonify(message="signature tidak valid"), 401
    status, fraud = str(body.get("transaction_status", "")), str(body.get("fraud_status", ""))
    if status == "settlement" or (status == "capture" and fraud == "accept"):
        new_status = "PAID"
    elif status == "capture" and fraud == "deny":
        new_status = "DENIED"
    elif status == "expire":
        new_status = "EXPIRED"
    elif status == "cancel":
        new_status = "CANCELLED"
    elif status == "deny":
        new_status = "DENIED"
    elif status == "pending" or status == "capture":
        new_status = "PENDING"
    else:
        return jsonify(message="notification status diabaikan")
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id,status,amount FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            return jsonify(message="order tidak ditemukan"), 404
        try:
            if Decimal(gross_amount) != Decimal(row["amount"]):
                return jsonify(message="jumlah pembayaran tidak sesuai"), 400
        except InvalidOperation:
            return jsonify(message="jumlah pembayaran tidak valid"), 400
        current_status = row["status"]
        if new_status == "PAID":
            db.execute("UPDATE orders SET status='PAID',paid_at=COALESCE(paid_at,?) WHERE id=?",
                       (datetime.now(timezone.utc).isoformat(), order_id))
        elif current_status == "PENDING":
            db.execute("UPDATE orders SET status=? WHERE id=?", (new_status, order_id))
    if new_status == "PAID":
        tickets_sent = _create_tickets_and_email(order_id)
        sheets_synced = _sync_paid_order_to_sheets(order_id)
        if not tickets_sent or not sheets_synced:
            return jsonify(
                message=(
                    "pembayaran tercatat; pengiriman tiket atau pencatatan Sheets "
                    "akan dicoba ulang"
                )
            ), 500
    return jsonify(message="notification diterima")


@store.get("/api/orders/<order_id>")
@login_required
def order_status(order_id):
    with _db() as db:
        order = db.execute(
            "SELECT id,package,amount,discount_amount,status,created_at,paid_at,"
            "email_sent_at,sheets_synced_at,user_id FROM orders WHERE id=?",
            (order_id,),
        ).fetchone()
        if not order or order["user_id"] != session["user_id"]:
            return jsonify(message="Pesanan ini tidak ditemukan. Coba muat ulang halaman."), 404
        paid = order["status"] == "PAID"
    if paid:
        _create_tickets_and_email(order_id)
        _sync_paid_order_to_sheets(order_id)
    with _db() as db:
        order = db.execute(
            "SELECT id,package,amount,discount_amount,status,created_at,paid_at,"
            "email_sent_at,sheets_synced_at FROM orders WHERE id=?",
            (order_id,),
        ).fetchone()
        tickets = db.execute(
            "SELECT full_name,ticket_code FROM order_participants WHERE order_id=?",
            (order_id,),
        ).fetchall()
    return jsonify(
        order={
            key: order[key]
            for key in (
                "id",
                "package",
                "amount",
                "discount_amount",
                "status",
                "created_at",
                "paid_at",
            )
        },
        email_sent=bool(order["email_sent_at"]),
        sheets_synced=bool(order["sheets_synced_at"]),
        tickets=[dict(person) for person in tickets],
    )
