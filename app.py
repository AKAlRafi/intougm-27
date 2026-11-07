import base64
from io import BytesIO
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path
from functools import wraps
from datetime import datetime, timedelta, timezone

import click
import requests
from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for
from werkzeug.utils import secure_filename
from into_store import (LEGACY_PACKAGE_ALIASES, PACKAGE_PHASES, PACKAGES, _phase_datetime_local,
                        _hash_password, csrf_required, init_store, login_required, store as store_blueprint,
                        public_base_url, ticket_pricing)

# WSGI servers may start with a working directory outside the project. Resolve
# the deployment's .env beside this file so configuration loads consistently.
load_dotenv(dotenv_path=Path(__file__).resolve().with_name(".env"), override=False)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or ""
if not app.secret_key:
    if os.environ.get("FLASK_ENV", "").lower() == "production" or os.environ.get("VERCEL") == "1":
        raise RuntimeError("FLASK_SECRET_KEY harus dikonfigurasi pada deployment production.")
    app.secret_key = secrets.token_hex(32)
    app.logger.warning("FLASK_SECRET_KEY is unset; sessions will reset whenever the server restarts.")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=2),
    SESSION_COOKIE_SECURE=os.environ.get(
        "COOKIE_SECURE",
        "true"
        if os.environ.get("VERCEL") == "1"
        or os.environ.get("FLASK_ENV", "").lower() == "production"
        else "false",
    ).lower() == "true",
    INTO_DATABASE=os.environ.get("INTO_DATABASE", os.path.join(app.instance_path, "into.sqlite3")),
    PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", ""),
    SMTP_HOST=os.environ.get("SMTP_HOST", ""), SMTP_PORT=os.environ.get("SMTP_PORT") or "587",
    SMTP_USER=os.environ.get("SMTP_USER", ""), SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD", ""),
    MAIL_FROM=os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER", ""),
    MIDTRANS_SERVER_KEY=os.environ.get("MIDTRANS_SERVER_KEY", ""),
    MIDTRANS_CLIENT_KEY=os.environ.get("MIDTRANS_CLIENT_KEY", ""),
    MIDTRANS_IS_PRODUCTION=os.environ.get("MIDTRANS_IS_PRODUCTION", "false"),
    INTO_SHEETS_WEBHOOK_URL=os.environ.get("INTO_SHEETS_WEBHOOK_URL", ""),
    INTO_SHEETS_TOKEN=os.environ.get("INTO_SHEETS_TOKEN", ""),
    INTO_VOUCHERS_JSON=os.environ.get("INTO_VOUCHERS_JSON", "{}"),
    WHATSAPP_GROUP_URL=os.environ.get("WHATSAPP_GROUP_URL", ""),
    INTO_CONTACT_PERSONS=os.environ.get("INTO_CONTACT_PERSONS", "Miftah & Steven"),
)
app.register_blueprint(store_blueprint)
try:
    init_store(app)
except sqlite3.OperationalError as exc:
    if "database is locked" not in str(exc).lower():
        raise
    raise RuntimeError(
        "Database SQLite sedang dikunci proses lain. Tutup database di DB Browser/SQLite editor "
        "dan hentikan server Flask lain yang memakai file yang sama, lalu coba lagi."
    ) from exc

PACKAGE_SIZES = {name: package["size"] for name, package in PACKAGES.items()}
ALLOWED_PROOF_TYPES = {"image/jpeg", "image/png", "image/webp"}
BA_CODE_RE = re.compile(r"^[A-Z0-9]{4,20}$")


def _dashboard_role_required(*allowed_roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user_id = session.get("user_id")
            if not user_id:
                session["auth_view"] = "login"
                session["auth_notice"] = "Masuk untuk membuka dashboard."
                session["auth_notice_state"] = "error"
                return redirect(url_for("home"))
            with sqlite3.connect(app.config["INTO_DATABASE"]) as db:
                db.row_factory = sqlite3.Row
                user = db.execute(
                    "SELECT role,is_verified,full_name FROM users WHERE id=?", (user_id,)
                ).fetchone()
            if not user or not user["is_verified"]:
                session.clear()
                session["auth_view"] = "login"
                session["auth_notice"] = "Sesi masuk berakhir. Silakan masuk kembali."
                return redirect(url_for("home"))
            # Refresh role from the database so demotions take effect immediately.
            session["role"] = user["role"]
            session["user_name"] = user["full_name"]
            if user["role"] not in allowed_roles:
                session["auth_notice"] = "Akunmu belum memiliki akses ke halaman tersebut."
                session["auth_notice_state"] = "error"
                return redirect(url_for("home"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


ba_required = _dashboard_role_required("ba", "admin", "dev")
ba_member_required = _dashboard_role_required("ba")
admin_required = _dashboard_role_required("admin", "dev")
dev_required = _dashboard_role_required("dev")

DEV_EDITABLE_COLUMNS = {
    "users": {
        "full_name": ("text", 120), "school": ("text", 140), "phone": ("text", 24),
        "email": ("email", 254), "role": ("select", ("user", "ba", "admin", "dev")),
    },
    "vouchers": {
        "code": ("code", 20),
        "discount_amount": ("integer", (0, 100_000_000)),
        "expires_at": ("text", 40), "active": ("select", ("0", "1")),
        "referral_type": ("select", ("other", "school", "ba")),
        "usage_limit": ("integer_optional", (1, 1_000_000)),
    },
    "ticket_pricing_settings": {
        "mode": ("select", ("manual", "automatic")),
        "manual_phase": ("select", PACKAGE_PHASES),
    },
    "ticket_phase_prices": {
        "price": ("integer", (0, 100_000_000)), "starts_at": ("text_optional", 40),
        "ends_at": ("text_optional", 40),
    },
    "voucher_discount_settings": {
        "discount_amount": ("integer_optional", (0, 100_000_000)),
    },
    "ba_missions": {
        "stage": ("text", 80), "title": ("text", 180),
        "sort_order": ("integer", (0, 1_000_000)),
    },
    "ba_referral_codes": {
        "code": ("code", 20), "active": ("select", ("0", "1")),
    },
}

DEV_TABLE_TITLES = {
    "users": "Akun pengguna", "vouchers": "Voucher",
    "ticket_pricing_settings": "Pengaturan harga", "ticket_phase_prices": "Harga tiap fase",
    "voucher_discount_settings": "Diskon kode",
    "ba_missions": "Daftar misi BA", "ba_referral_codes": "Kode referral BA",
}


@app.cli.command("grant-dev")
@click.argument("email")
def grant_dev_command(email):
    """Grant dev access to an existing verified account (run from a server console)."""
    db = _dashboard_db()
    try:
        user = db.execute(
            "SELECT id,is_verified FROM users WHERE email=? COLLATE NOCASE", (email.strip(),)
        ).fetchone()
        if not user:
            raise click.ClickException("Akun dengan email tersebut tidak ditemukan.")
        if not user["is_verified"]:
            raise click.ClickException("Akun harus memverifikasi email sebelum diberi role dev.")
        db.execute("UPDATE users SET role='dev' WHERE id=?", (user["id"],))
        db.commit()
    finally:
        db.close()
    click.echo(f"Role dev diberikan ke {email.strip()}.")


@app.get("/")
def home():
    # Older cached login forms submitted with GET and exposed credentials in the
    # address bar. Never use query-string credentials; immediately clear them.
    if any(key in request.args for key in ("email", "password", "confirm_password")):
        return redirect(url_for("home"), code=303)
    db = _dashboard_db()
    try:
        pricing = ticket_pricing(db)
    finally:
        db.close()
    return render_template(
        "index.html",
        csrf_token=session.setdefault("csrf_token", secrets.token_urlsafe(32)),
        auth_view=session.pop("auth_view", ""),
        auth_notice=session.pop("auth_notice", ""),
        auth_notice_state=session.pop("auth_notice_state", "error"),
        auth_email=session.pop("auth_email", ""),
        pending_package=session.pop("pending_package", ""),
        user_role=session.get("role", ""),
        pricing=pricing,
    )


def _dashboard_db():
    db = sqlite3.connect(app.config["INTO_DATABASE"])
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _dev_database_value(table, column, raw_value):
    kind, rule = DEV_EDITABLE_COLUMNS[table][column]
    value = raw_value.strip()
    if kind == "select":
        if value not in rule:
            raise ValueError(f"Nilai untuk {column} tidak valid.")
        return int(value) if column == "active" else value
    if kind in {"integer", "integer_optional"}:
        if not value and kind == "integer_optional":
            return None
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{column} harus berupa angka bulat.") from exc
        low, high = rule
        if not low <= number <= high:
            raise ValueError(f"Nilai {column} harus antara {low} dan {high}.")
        return number
    if kind in {"text", "text_optional"}:
        if not value and kind == "text_optional":
            return None
        if not value or len(value) > rule:
            raise ValueError(f"{column} wajib diisi dan maksimal {rule} karakter.")
        return value
    if kind == "email":
        if len(value) > rule or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Format email tidak valid.")
        return value.lower()
    if kind == "code":
        value = value.upper()
        if not BA_CODE_RE.fullmatch(value) or len(value) > rule:
            raise ValueError("Kode harus terdiri dari 4–20 huruf atau angka, tanpa spasi atau simbol.")
        return value
    raise ValueError("Jenis data tidak didukung.")


@app.get("/dev")
@dev_required
def developer_dashboard():
    db = _dashboard_db()
    try:
        table_stats = []
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall():
            table_name = row["name"]
            quoted_table = '"' + table_name.replace('"', '""') + '"'
            count = db.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
            table_stats.append({"name": table_name, "count": count})

        selected_table = request.args.get("table", "users")
        if selected_table not in DEV_EDITABLE_COLUMNS:
            selected_table = "users"
        table_columns = [row["name"] for row in db.execute(
            f'PRAGMA table_info("{selected_table}")'
        ).fetchall()]
        display_columns = [name for name in table_columns if name not in {
            "password_hash", "verify_token_hash", "verify_expires_at", "midtrans_token", "evidence",
        }]
        rows = db.execute(
            f'SELECT rowid AS "__dev_rowid__", * FROM "{selected_table}" ORDER BY rowid DESC LIMIT 200'
        ).fetchall()
        ba_accounts = db.execute(
            "SELECT id,full_name,email FROM users WHERE role='ba' ORDER BY full_name COLLATE NOCASE"
        ).fetchall()
        editable_rows = []
        for row in rows:
            controls = []
            for column, (kind, rule) in DEV_EDITABLE_COLUMNS[selected_table].items():
                options = rule if kind == "select" else ()
                controls.append({
                    "name": column,
                    "label": column.replace("_", " ").title(),
                    "kind": "number" if kind.startswith("integer") else "select" if kind == "select" else "email" if kind == "email" else "text",
                    "value": "" if row[column] is None else str(row[column]),
                    "options": options,
                    "optional": kind in {"text_optional", "integer_optional"},
                })
            editable_rows.append({
                "rowid": row["__dev_rowid__"],
                "display_values": [(name, row[name]) for name in display_columns],
                "cells": {name: next((item for item in controls if item["name"] == name), None)
                          for name in display_columns},
                "controls": controls,
            })
    finally:
        db.close()
    response = app.make_response(render_template(
        "dashboard_dev.html", table_stats=table_stats, selected_table=selected_table,
        table_title=DEV_TABLE_TITLES[selected_table], rows=editable_rows, columns=display_columns,
        csrf_token=session.setdefault("csrf_token", secrets.token_urlsafe(32)),
        db_path=app.config["INTO_DATABASE"],
        ba_accounts=[dict(row) for row in ba_accounts],
    ))
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.post("/dev/database/<table>/<int:row_id>")
@dev_required
@csrf_required
def developer_update_row(table, row_id):
    if table not in DEV_EDITABLE_COLUMNS:
        abort(404)
    fields = DEV_EDITABLE_COLUMNS[table]
    if any(name not in request.form for name in fields):
        abort(400)
    try:
        values = {name: _dev_database_value(table, name, request.form[name]) for name in fields}
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("developer_dashboard", table=table))

    db = _dashboard_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(f'SELECT * FROM "{table}" WHERE rowid=?', (row_id,)).fetchone()
        if not existing:
            abort(404)
        if table == "users" and existing["role"] == "dev" and values["role"] != "dev":
            dev_count = db.execute("SELECT COUNT(*) FROM users WHERE role='dev'").fetchone()[0]
            if dev_count <= 1:
                flash("Minimal harus ada satu akun dev. Berikan role dev ke akun lain terlebih dahulu.", "error")
                return redirect(url_for("developer_dashboard", table=table))
        assignments = ",".join(f'"{column}"=?' for column in fields)
        if table == "ba_referral_codes":
            old_code = existing["code"]
            new_code = values["code"]
            collision = db.execute("SELECT 1 FROM vouchers WHERE code=? COLLATE NOCASE", (new_code,)).fetchone()
            if collision:
                raise ValueError("Kode sudah dipakai sebagai voucher. Gunakan kode lain.")
            if values["active"]:
                db.execute("UPDATE ba_referral_codes SET active=0 WHERE ba_user_id=? AND rowid<>?",
                           (existing["ba_user_id"], row_id))
            db.execute(f'UPDATE "{table}" SET {assignments} WHERE rowid=?', (*values.values(), row_id))
            if old_code.casefold() != new_code.casefold():
                # Retain the old value as inactive so past orders stay attributed to this BA.
                db.execute("INSERT INTO ba_referral_codes(code,ba_user_id,active,created_at) VALUES(?,?,0,?)",
                           (old_code, existing["ba_user_id"], existing["created_at"]))
            active_code = db.execute("""SELECT code FROM ba_referral_codes
                WHERE ba_user_id=? AND active=1 ORDER BY created_at DESC LIMIT 1""",
                (existing["ba_user_id"],)).fetchone()
            db.execute("UPDATE users SET referral_code=? WHERE id=?",
                       (active_code["code"] if active_code else None, existing["ba_user_id"]))
        elif table == "vouchers":
            old_code = existing["code"]
            new_code = values["code"]
            collision = db.execute("SELECT 1 FROM ba_referral_codes WHERE code=? COLLATE NOCASE", (new_code,)).fetchone()
            if collision:
                raise ValueError("Kode sudah dipakai sebagai kode referral BA. Gunakan kode lain.")
            db.execute(f'UPDATE "{table}" SET {assignments} WHERE rowid=?', (*values.values(), row_id))
            if old_code.casefold() != new_code.casefold():
                # Keep a disabled historical entry for orders that used the previous code.
                db.execute("""INSERT INTO vouchers(code,discount_amount,expires_at,active,referral_type,usage_limit)
                    VALUES(?,?,?,0,?,?)""",
                    (old_code, existing["discount_amount"], existing["expires_at"],
                     existing["referral_type"], existing["usage_limit"]))
        else:
            db.execute(f'UPDATE "{table}" SET {assignments} WHERE rowid=?', (*values.values(), row_id))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        flash("Perubahan gagal karena nilainya bentrok dengan data lain atau melanggar relasi database.", "error")
        return redirect(url_for("developer_dashboard", table=table))
    except ValueError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("developer_dashboard", table=table))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    flash("Perubahan database tersimpan dan langsung aktif.", "success")
    return redirect(url_for("developer_dashboard", table=table))


@app.post("/dev/users/<int:user_id>/password")
@dev_required
@csrf_required
def developer_reset_password(user_id):
    password = request.form.get("new_password", "")
    if len(password) < 5 or len(password) > 256:
        flash("Kata sandi baru harus berisi 5–256 karakter.", "error")
        return redirect(url_for("developer_dashboard", table="users"))
    password_hash = _hash_password(password)
    db = _dashboard_db()
    try:
        cursor = db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
        db.commit()
    finally:
        db.close()
    if not cursor.rowcount:
        abort(404)
    flash("Kata sandi diperbarui tanpa mengirim verifikasi email baru. Status verifikasi akun tetap sama.", "success")
    return redirect(url_for("developer_dashboard", table="users"))


@app.post("/dev/referral-codes/ba")
@dev_required
@csrf_required
def developer_create_ba_code():
    try:
        code = _dev_database_value("ba_referral_codes", "code", request.form.get("code", ""))
        ba_user_id = int(request.form.get("ba_user_id", ""))
    except (ValueError, TypeError):
        flash("Pilih akun BA yang valid dan masukkan kode 4–20 huruf atau angka.", "error")
        return redirect(url_for("developer_dashboard", table="ba_referral_codes"))
    db = _dashboard_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        ba = db.execute("SELECT id FROM users WHERE id=? AND role='ba'", (ba_user_id,)).fetchone()
        if not ba:
            raise ValueError("Akun yang dipilih bukan Brand Ambassador.")
        if (db.execute("SELECT 1 FROM ba_referral_codes WHERE code=? COLLATE NOCASE", (code,)).fetchone()
                or db.execute("SELECT 1 FROM vouchers WHERE code=? COLLATE NOCASE", (code,)).fetchone()):
            raise ValueError("Kode sudah dipakai. Pilih kode yang lain.")
        # Keep historical codes for reporting but allow one active shareable code per BA.
        db.execute("UPDATE ba_referral_codes SET active=0 WHERE ba_user_id=? AND active=1", (ba_user_id,))
        db.execute("INSERT INTO ba_referral_codes(code,ba_user_id,active,created_at) VALUES(?,?,1,?)",
                   (code, ba_user_id, datetime.now(timezone.utc).isoformat()))
        db.execute("UPDATE users SET referral_code=? WHERE id=?", (code, ba_user_id))
        db.commit()
    except ValueError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("developer_dashboard", table="ba_referral_codes"))
    except sqlite3.IntegrityError:
        db.rollback()
        flash("Kode baru saja dipakai atau bertabrakan dengan data lain. Coba kode berbeda.", "error")
        return redirect(url_for("developer_dashboard", table="ba_referral_codes"))
    finally:
        db.close()
    flash(f"Kode referral {code} aktif untuk Brand Ambassador yang dipilih.", "success")
    return redirect(url_for("developer_dashboard", table="ba_referral_codes"))


@app.post("/dev/referral-codes/sekolah")
@dev_required
@csrf_required
def developer_create_school_code():
    try:
        code = _dev_database_value("vouchers", "code", request.form.get("code", ""))
        discount_amount = _dev_database_value("vouchers", "discount_amount", request.form.get("discount_amount", ""))
        usage_limit = _dev_database_value("vouchers", "usage_limit", request.form.get("usage_limit", ""))
        expires_input = request.form.get("expires_at", "").strip()
        expires = datetime.fromisoformat(_phase_datetime_local(expires_input))
        if expires <= datetime.now(timezone.utc):
            raise ValueError("Masa berlaku harus berada di waktu mendatang.")
    except (ValueError, TypeError):
        flash("Isi kode, diskon, masa berlaku mendatang, dan kuota dengan benar.", "error")
        return redirect(url_for("developer_dashboard", table="vouchers"))
    db = _dashboard_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        if (db.execute("SELECT 1 FROM vouchers WHERE code=? COLLATE NOCASE", (code,)).fetchone()
                or db.execute("SELECT 1 FROM ba_referral_codes WHERE code=? COLLATE NOCASE", (code,)).fetchone()):
            raise ValueError("Kode sudah dipakai. Pilih kode yang lain.")
        db.execute("""INSERT INTO vouchers(code,discount_amount,expires_at,active,referral_type,usage_limit)
            VALUES(?,?,?,1,'school',?)""", (code, discount_amount, expires.isoformat(), usage_limit))
        db.commit()
    except ValueError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("developer_dashboard", table="vouchers"))
    except sqlite3.IntegrityError:
        db.rollback()
        flash("Kode baru saja dipakai atau bertabrakan dengan data lain. Coba kode berbeda.", "error")
        return redirect(url_for("developer_dashboard", table="vouchers"))
    finally:
        db.close()
    flash(f"Kode sekolah {code} berhasil ditambahkan dan langsung aktif.", "success")
    return redirect(url_for("developer_dashboard", table="vouchers"))


@app.get("/dashboardBA")
@ba_required
def dashboard_ba():
    if session.get("role") in {"admin", "dev"}:
        return redirect(url_for("dashboard_admin_ba_list"))
    return _render_ba_dashboard(session["user_id"], admin_view=False)


def _render_ba_dashboard(ba_id, admin_view):
    db = _dashboard_db()
    try:
        profile = db.execute(
            """SELECT id,full_name,school,
                (SELECT code FROM ba_referral_codes
                 WHERE ba_user_id=users.id AND active=1 ORDER BY created_at DESC LIMIT 1) AS referral_code
                FROM users WHERE id=? AND role='ba'""", (ba_id,)
        ).fetchone()
        if not profile:
            abort(404)
        code = profile["referral_code"]
        redeemed = db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM orders WHERE voucher_code IN "
            "(SELECT code FROM ba_referral_codes WHERE ba_user_id=?) AND status='PAID'", (ba_id,)
        ).fetchone()[0]
        recent_orders = db.execute(
            "SELECT id,package,amount,paid_at FROM orders WHERE voucher_code IN "
            "(SELECT code FROM ba_referral_codes WHERE ba_user_id=?) AND status='PAID' "
            "ORDER BY paid_at DESC LIMIT 8", (ba_id,)
        ).fetchall()
        missions = db.execute("""SELECT m.mission_key,m.stage,m.title,m.sort_order,
              s.id AS submission_id,s.file_name,s.mime_type,s.status,s.submitted_at,
              s.reviewed_at,s.review_note
            FROM ba_missions m LEFT JOIN ba_mission_submissions s
              ON s.mission_key=m.mission_key AND s.ba_user_id=?
            ORDER BY m.sort_order""", (ba_id,)).fetchall()
    finally:
        db.close()
    discount_percent = min(redeemed * 5, 100)
    db = _dashboard_db()
    try:
        ticket_price = ticket_pricing(db)["prices"]["Single"]
    finally:
        db.close()
    discount_amount = ticket_price if discount_percent == 100 else ticket_price * discount_percent // 100
    referral_link = f"{public_base_url()}/?ref={code}#paket" if code else ""
    return render_template(
        "dashboard_ba.html", name=profile["full_name"], school=profile["school"], code=code,
        redeemed=redeemed, progress=min(redeemed, 10), discount_percent=discount_percent,
        ticket_price=ticket_price, discount_amount=discount_amount,
        final_price=max(ticket_price - discount_amount, 0), referral_link=referral_link,
        recent_orders=recent_orders, missions=missions, admin_view=admin_view,
        ba_id=ba_id, csrf_token=session.setdefault("csrf_token", secrets.token_urlsafe(32)),
    )


@app.post("/dashboardBA/kode")
@ba_member_required
@csrf_required
def create_ba_referral_code():
    code = request.form.get("referral_code", "").strip().upper()
    if not BA_CODE_RE.fullmatch(code):
        flash("Kode harus terdiri dari 4–20 huruf atau angka, tanpa spasi atau simbol.", "error")
        return redirect(url_for("dashboard_ba"))
    db = _dashboard_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT role FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not user or user["role"] != "ba":
            abort(403)
        if db.execute("SELECT 1 FROM ba_referral_codes WHERE ba_user_id=? AND active=1 LIMIT 1",
                      (session["user_id"],)).fetchone():
            flash("Kamu sudah memiliki kode promosi. Kode yang sudah dibuat tidak dapat diganti.", "info")
            return redirect(url_for("dashboard_ba"))
        if (db.execute("SELECT 1 FROM ba_referral_codes WHERE code=?", (code,)).fetchone()
                or db.execute("SELECT 1 FROM vouchers WHERE code=?", (code,)).fetchone()):
            flash("Kode tersebut sudah digunakan. Coba pilih kode lain.", "error")
            return redirect(url_for("dashboard_ba"))
        created_at = datetime.now(timezone.utc).isoformat()
        db.execute("INSERT INTO ba_referral_codes(code,ba_user_id,active,created_at) VALUES(?,?,1,?)",
                   (code, session["user_id"], created_at))
        db.execute("UPDATE users SET referral_code=? WHERE id=?", (code, session["user_id"]))
        db.commit()
        flash(f"Kode promosi {code} berhasil dibuat. Bagikan kode dan tautan ajakanmu!", "success")
    except sqlite3.IntegrityError:
        db.rollback()
        flash("Kode tersebut baru saja dipakai orang lain. Silakan pilih kode lain.", "error")
    finally:
        db.close()
    return redirect(url_for("dashboard_ba"))


@app.get("/dashboard-admin/ba")
@admin_required
def dashboard_admin_ba_list():
    db = _dashboard_db()
    try:
        ambassadors = db.execute("""SELECT u.id,u.full_name,u.school,u.email,
            (SELECT rc_active.code FROM ba_referral_codes rc_active
             WHERE rc_active.ba_user_id=u.id AND rc_active.active=1
             ORDER BY rc_active.created_at DESC LIMIT 1) AS referral_code,
            COUNT(DISTINCT CASE WHEN o.status='PAID' THEN o.user_id END) AS paid_buyers,
              COUNT(DISTINCT CASE WHEN s.status='PENDING' THEN s.id END) AS pending_missions
            FROM users u LEFT JOIN ba_referral_codes rc ON rc.ba_user_id=u.id
            LEFT JOIN orders o ON o.voucher_code=rc.code
            LEFT JOIN ba_mission_submissions s ON s.ba_user_id=u.id
            WHERE u.role='ba' GROUP BY u.id ORDER BY u.full_name COLLATE NOCASE""").fetchall()
    finally:
        db.close()
    return render_template("dashboard_admin_ba_list.html", ambassadors=ambassadors)


@app.get("/dashboard-admin/ba/<int:ba_id>")
@admin_required
def dashboard_admin_ba_view(ba_id):
    return _render_ba_dashboard(ba_id, admin_view=True)


def _mission_action_response(message, category, endpoint, *, response_values=None, **endpoint_values):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(
            ok=category != "error",
            message=message,
            category=category,
            **(response_values or {}),
        ), (400 if category == "error" else 200)
    flash(message, category)
    return redirect(url_for(endpoint, **endpoint_values))


@app.post("/dashboardBA/misi/<mission_key>/bukti")
@ba_member_required
@csrf_required
def upload_ba_mission_evidence(mission_key):
    file = request.files.get("evidence")
    db = _dashboard_db()
    try:
        mission = db.execute("SELECT 1 FROM ba_missions WHERE mission_key=?", (mission_key,)).fetchone()
        if not mission:
            abort(404)
        existing = db.execute("SELECT status FROM ba_mission_submissions WHERE ba_user_id=? AND mission_key=?",
                              (session["user_id"], mission_key)).fetchone()
        if existing and existing["status"] == "APPROVED":
            return _mission_action_response("Misi ini sudah disetujui admin.", "info", "dashboard_ba")
        if existing and existing["status"] == "PENDING":
            return _mission_action_response("Bukti misi ini sedang menunggu pemeriksaan admin.", "info", "dashboard_ba")
        if not file or not file.filename:
            return _mission_action_response("Pilih tangkapan layar atau foto sebagai bukti misi.", "error", "dashboard_ba")
        content = file.read(5 * 1024 * 1024 + 1)
        mime_type = file.mimetype.lower()
        signatures = {
            "image/jpeg": content.startswith(b"\xff\xd8\xff"),
            "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP",
        }
        if len(content) > 5 * 1024 * 1024:
            return _mission_action_response("Ukuran gambar maksimal 5 MB.", "error", "dashboard_ba")
        if not signatures.get(mime_type, False):
            return _mission_action_response("Unggah bukti berupa gambar JPG, PNG, atau WEBP.", "error", "dashboard_ba")
        db.execute("""INSERT INTO ba_mission_submissions
            (ba_user_id,mission_key,file_name,mime_type,evidence,status,submitted_at,reviewed_by,reviewed_at,review_note)
            VALUES(?,?,?,?,?,'PENDING',?,NULL,NULL,NULL)
            ON CONFLICT(ba_user_id,mission_key) DO UPDATE SET
              file_name=excluded.file_name,mime_type=excluded.mime_type,evidence=excluded.evidence,
              status='PENDING',submitted_at=excluded.submitted_at,reviewed_by=NULL,reviewed_at=NULL,review_note=NULL""",
            (session["user_id"], mission_key, secure_filename(file.filename) or "bukti-misi", mime_type,
             sqlite3.Binary(content), datetime.now(timezone.utc).isoformat()))
        db.commit()
        submission = db.execute(
            "SELECT id FROM ba_mission_submissions WHERE ba_user_id=? AND mission_key=?",
            (session["user_id"], mission_key),
        ).fetchone()
        evidence_url = url_for("serve_ba_mission_evidence", submission_id=submission["id"])
    finally:
        db.close()
    return _mission_action_response(
        "Bukti berhasil dikirim. Misi akan ditandai selesai setelah disetujui admin.",
        "success", "dashboard_ba", response_values={"evidence_url": evidence_url},
    )


@app.post("/dashboard-admin/misi/<int:submission_id>/periksa")
@admin_required
@csrf_required
def review_ba_mission(submission_id):
    decision = request.form.get("decision", "").upper()
    if decision not in {"APPROVED", "REJECTED"}:
        abort(400)
    note = request.form.get("review_note", "").strip()[:500]
    db = _dashboard_db()
    try:
        submission = db.execute("SELECT ba_user_id,status FROM ba_mission_submissions WHERE id=?",
                                (submission_id,)).fetchone()
        if not submission:
            abort(404)
        if submission["status"] != "PENDING":
            response_message, response_category = "Bukti ini sudah pernah diperiksa.", "info"
        else:
            db.execute("""UPDATE ba_mission_submissions SET status=?,reviewed_by=?,reviewed_at=?,review_note=?
                WHERE id=? AND status='PENDING'""",
                (decision, session["user_id"], datetime.now(timezone.utc).isoformat(), note, submission_id))
            db.commit()
            response_message = "Bukti misi disetujui." if decision == "APPROVED" else "Bukti misi diminta untuk diperbaiki."
            response_category = "success" if decision == "APPROVED" else "info"
        ba_id = submission["ba_user_id"]
    finally:
        db.close()
    return _mission_action_response(response_message, response_category,
                                   "dashboard_admin_ba_view", ba_id=ba_id)


@app.get("/dashboard-misi/bukti/<int:submission_id>")
def serve_ba_mission_evidence(submission_id):
    user_id = session.get("user_id")
    if not user_id:
        abort(401)
    db = _dashboard_db()
    try:
        row = db.execute("""SELECT s.ba_user_id,s.file_name,s.mime_type,s.evidence,
              (SELECT role FROM users WHERE id=?) AS viewer_role
            FROM ba_mission_submissions s WHERE s.id=?""",
            (user_id, submission_id)).fetchone()
    finally:
        db.close()
    if not row or (row["ba_user_id"] != user_id and row["viewer_role"] not in {"admin", "dev"}):
        abort(404)
    response = send_file(BytesIO(row["evidence"]), mimetype=row["mime_type"],
                         download_name=row["file_name"], as_attachment=False, max_age=0)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/dashboard-admin")
@admin_required
def dashboard_admin():
    db = _dashboard_db()
    try:
        schools = db.execute("""SELECT p.school,COUNT(*) AS buyers FROM order_participants p
            JOIN orders o ON o.id=p.order_id WHERE o.status='PAID' AND TRIM(p.school)<>''
            GROUP BY p.school ORDER BY buyers DESC,p.school LIMIT 8""").fetchall()
        packages = db.execute("""SELECT package,COUNT(*) AS orders FROM orders
            WHERE status='PAID' GROUP BY package ORDER BY orders DESC""").fetchall()
        payment = db.execute("""SELECT status,COUNT(*) AS orders,COALESCE(SUM(amount),0) AS amount
            FROM orders GROUP BY status""").fetchall()
        referral = db.execute("""SELECT CASE
              WHEN EXISTS(SELECT 1 FROM ba_referral_codes c WHERE c.code=o.voucher_code) THEN 'Kode Brand Ambassador'
              WHEN EXISTS(SELECT 1 FROM vouchers v WHERE v.code=o.voucher_code AND v.referral_type='school') THEN 'Kode Sekolah'
              ELSE 'Kode Promosi Lain' END AS source, COUNT(*) AS orders
            FROM orders o WHERE o.status='PAID' AND o.voucher_code IS NOT NULL
            GROUP BY source ORDER BY orders DESC""").fetchall()
        total_paid = db.execute("SELECT COUNT(*) FROM orders WHERE status='PAID'").fetchone()[0]
        paid_revenue = db.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='PAID'").fetchone()[0]
        pending_amount = db.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='PENDING'").fetchone()[0]
        pending_count = db.execute("SELECT COUNT(*) FROM orders WHERE status='PENDING'").fetchone()[0]
        pricing = ticket_pricing(db)
        phase_rows = db.execute("""SELECT phase,package,price,starts_at,ends_at FROM ticket_phase_prices
            ORDER BY CASE phase WHEN 'Early Bird' THEN 1 WHEN 'Presale' THEN 2 WHEN 'Normal' THEN 3 WHEN 'Flashsale' THEN 4 ELSE 5 END,
            package""").fetchall()
        voucher_discount_rows = db.execute("""SELECT referral_type,package,discount_amount
            FROM voucher_discount_settings""").fetchall()
    finally:
        db.close()
    phase_settings = {phase: {"prices": {}, "starts_at": "", "ends_at": ""} for phase in PACKAGE_PHASES}
    voucher_discounts = {kind: {package: None for package in PACKAGES} for kind in ("school", "ba")}
    for row in voucher_discount_rows:
        voucher_discounts[row["referral_type"]][row["package"]] = row["discount_amount"]
    for row in phase_rows:
        phase_settings[row["phase"]]["prices"][row["package"]] = row["price"]
        if row["starts_at"]:
            phase_settings[row["phase"]]["starts_at"] = datetime.fromisoformat(row["starts_at"]).astimezone(
                timezone(timedelta(hours=7))).strftime("%Y-%m-%dT%H:%M")
        if row["ends_at"]:
            phase_settings[row["phase"]]["ends_at"] = datetime.fromisoformat(row["ends_at"]).astimezone(
                timezone(timedelta(hours=7))).strftime("%Y-%m-%dT%H:%M")
    return render_template(
        "dashboard_admin.html", schools=[dict(x) for x in schools],
        packages=[dict(x) for x in packages], referral=[dict(x) for x in referral],
        paid_revenue=paid_revenue, pending_amount=pending_amount, pending_count=pending_count,
        total_paid=total_paid,
        payment={row["status"]: dict(row) for row in payment},
        pricing=pricing, package_names=list(PACKAGES), phase_names=PACKAGE_PHASES,
        phase_settings=phase_settings,
        voucher_discounts=voucher_discounts,
        user_role=session.get("role", ""),
        csrf_token=session.setdefault("csrf_token", secrets.token_urlsafe(32)),
    )


@app.post("/dashboard-admin/harga")
@admin_required
@csrf_required
def save_ticket_pricing():
    mode = request.form.get("mode", "")
    manual_phase = request.form.get("manual_phase", "")
    if mode not in {"manual", "automatic"} or manual_phase not in PACKAGE_PHASES:
        flash("Pilih mode dan fase harga yang valid.", "error")
        return redirect(url_for("dashboard_admin"))
    phase_values = {}
    try:
        for phase in PACKAGE_PHASES:
            raw_prices = {}
            for package_name in PACKAGES:
                value = int(request.form.get(f"price_{phase}_{package_name}", ""))
                if value != 0 and not 1000 <= value <= 100_000_000:
                    raise ValueError
                raw_prices[package_name] = value
            starts_at = _phase_datetime_local(request.form.get(f"start_{phase}", "").strip())
            ends_at = _phase_datetime_local(request.form.get(f"end_{phase}", "").strip())
            if bool(starts_at) != bool(ends_at) or (starts_at and starts_at >= ends_at):
                raise ValueError
            phase_values[phase] = (raw_prices, starts_at, ends_at)
    except (TypeError, ValueError):
        flash("Isi harga 0 jika paket tidak tersedia, atau Rp1.000–Rp100.000.000; pastikan tanggal valid.", "error")
        return redirect(url_for("dashboard_admin"))
    if mode == "automatic":
        if any(not starts or not ends for _, starts, ends in phase_values.values()):
            flash("Mode otomatis memerlukan tanggal mulai dan selesai untuk setiap fase.", "error")
            return redirect(url_for("dashboard_admin"))
        intervals = sorted((starts, ends) for _, starts, ends in phase_values.values())
        if any(intervals[index][0] < intervals[index - 1][1] for index in range(1, len(intervals))):
            flash("Jadwal fase tidak boleh saling bertumpuk.", "error")
            return redirect(url_for("dashboard_admin"))
    db = _dashboard_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE ticket_pricing_settings SET mode=?,manual_phase=? WHERE id=1", (mode, manual_phase))
        for phase, (prices, starts, ends) in phase_values.items():
            db.executemany("""UPDATE ticket_phase_prices SET price=?,starts_at=?,ends_at=?
                WHERE phase=? AND package=?""",
                [(price, starts, ends, phase, package_name) for package_name, price in prices.items()])
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    flash("Pengaturan fase dan harga tiket berhasil disimpan.", "success")
    return redirect(url_for("dashboard_admin"))


@app.post("/dashboard-admin/diskon-voucher")
@admin_required
@csrf_required
def save_voucher_discounts():
    values = {}
    try:
        for kind in ("school", "ba"):
            for package_name in PACKAGES:
                raw = request.form.get(f"discount_{kind}_{package_name}", "").strip()
                amount = None if raw == "" else int(raw)
                if amount is not None and not 0 <= amount <= 100_000_000:
                    raise ValueError
                values[(kind, package_name)] = amount
    except (TypeError, ValueError):
        flash("Diskon harus berupa angka rupiah bulat dari 0 sampai 100.000.000; kosongkan untuk memakai diskon lama per kode.", "error")
        return redirect(url_for("dashboard_admin"))
    db = _dashboard_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.executemany("""UPDATE voucher_discount_settings SET discount_amount=?
            WHERE referral_type=? AND package=?""",
            [(amount, kind, package) for (kind, package), amount in values.items()])
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    flash("Diskon kode sekolah dan BA tersimpan. Berlaku untuk pesanan baru.", "success")
    return redirect(url_for("dashboard_admin") + "#pengaturan-diskon")


@app.get("/assets/<path:filename>")
def assets(filename):
    return send_from_directory("assets", filename, max_age=3600)


@app.post("/api/pemesanan")
@login_required
@csrf_required
def submit_order():
    requested_package = request.form.get("paket", "").strip()
    fields = {"paket": LEGACY_PACKAGE_ALIASES.get(requested_package, requested_package)}
    if fields["paket"] not in PACKAGE_SIZES:
        return jsonify(message="Paket ini belum tersedia. Pilih paket yang ada di halaman tiket."), 400
    db = _dashboard_db()
    try:
        if ticket_pricing(db)["prices"][fields["paket"]] <= 0:
            return jsonify(message="Paket ini tidak tersedia pada fase harga yang sedang aktif."), 400
    finally:
        db.close()
    peserta = []
    for nomor in range(1, PACKAGE_SIZES[fields["paket"]] + 1):
        orang = {
            "nama": request.form.get(f"peserta_{nomor}_nama", "").strip(),
            "whatsapp": request.form.get(f"peserta_{nomor}_whatsapp", "").strip(),
            "sekolah": request.form.get(f"peserta_{nomor}_sekolah", "").strip(),
        }
        if not all(orang.values()):
            return jsonify(message=f"Lengkapi data peserta {nomor} dulu, ya."), 400
        orang["pendaftar"] = nomor == 1
        peserta.append(orang)
    if request.form.get("persetujuan") != "on":
        return jsonify(message="Centang persetujuan penggunaan data untuk melanjutkan."), 400

    proof = request.files.get("bukti")
    proof_data = None
    if proof and proof.filename:
        if proof.mimetype not in ALLOWED_PROOF_TYPES:
            return jsonify(message="Unggah bukti dalam format JPG, PNG, atau WEBP."), 400
        content = proof.read(5 * 1024 * 1024 + 1)
        if len(content) > 5 * 1024 * 1024:
            return jsonify(message="Ukuran bukti maksimal 5 MB. Pilih berkas yang lebih kecil, ya."), 413
        proof_data = {
            "filename": os.path.basename(proof.filename),
            "contentType": proof.mimetype,
            "base64": base64.b64encode(content).decode("ascii"),
        }

    webhook = app.config.get("INTO_SHEETS_WEBHOOK_URL", "").strip()
    token = app.config.get("INTO_SHEETS_TOKEN", "").strip()
    if not webhook or not token:
        return jsonify(
            message=(
                "Pemesanan belum bisa dikirim saat ini. Coba lagi nanti atau hubungi panitia."
            )
        ), 503

    payload = {
        # Keep the legacy Apps Script deployment useful until its new version is published.
        "nama": peserta[0]["nama"],
        "whatsapp": peserta[0]["whatsapp"],
        "sekolah": peserta[0]["sekolah"],
        "paket": fields["paket"],
        "teman": ", ".join(person["nama"] for person in peserta[1:]),
        "peserta": peserta,
        "jumlahPeserta": len(peserta),
        "waktuKirim": datetime.now(timezone.utc).isoformat(),
        "bukti": proof_data,
        "token": token,
    }
    try:
        response = requests.post(webhook, json=payload, timeout=60, allow_redirects=True)
    except requests.RequestException:
        app.logger.exception("Google Sheets webhook request failed")
        return jsonify(message="Pemesanan belum terkirim. Periksa koneksi internetmu, lalu coba lagi."), 502

    try:
        result = response.json()
    except ValueError:
        app.logger.error(
            "Google Sheets returned non-JSON response (HTTP %s, content-type %s)",
            response.status_code,
            response.headers.get("Content-Type", "unknown"),
        )
        return jsonify(
            message=(
                "Pemesanan belum bisa diproses saat ini. Coba lagi nanti atau hubungi panitia."
            )
        ), 502

    if response.ok and isinstance(result, dict) and result.get("ok"):
        return jsonify(message="Pemesananmu sudah kami terima. Panitia akan menghubungimu lewat WhatsApp."), 201

    detail = str(result.get("message", "") if isinstance(result, dict) else "respons bukan object JSON")[:240]
    if detail.lower() == "unauthorized":
        return jsonify(message="Pemesanan belum bisa diproses saat ini. Hubungi panitia untuk bantuan."), 502
    app.logger.warning("Google Sheets rejected order (HTTP %s): %s", response.status_code, detail)
    return jsonify(message="Pemesanan belum bisa diproses saat ini. Coba lagi nanti atau hubungi panitia."), 502


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify(message="Ukuran berkas terlalu besar. Batas unggahan 5 MB."), 413


if __name__ == "__main__":
    default_port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    port = int(sys.argv[1]) if len(sys.argv) > 1 else default_port
    app.run(debug=debug, port=port)
