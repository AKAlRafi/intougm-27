# InTO UGM 2027

Landing page and ticket checkout built on Flask, SQLite, SMTP email, and Midtrans Snap. The SQLite
schema is initialized automatically when Flask starts; `schema.sql` is included as a reference.

## Setup

1. Use Python 3.10 or newer and install the packages:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   Copy-Item .env.example .env
   ```

2. Set `FLASK_SECRET_KEY` to a private random value. Configure `PUBLIC_BASE_URL` to the public HTTPS
   site address when deployed.
3. Configure SMTP (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM`) for
   verification and ticket email. For Gmail, use `smtp.gmail.com:587`, enable 2-Step Verification,
   and put a Google App Password in `SMTP_PASSWORD` (not the normal account password).
4. Set matching Midtrans Snap server and client keys. Start with sandbox credentials and
   `MIDTRANS_IS_PRODUCTION=false`.
5. Set `WHATSAPP_GROUP_URL` to the real group invite link before checkout is opened; order creation
   is blocked without it so paid ticket emails always contain the official group link. Contact names
   default to Miftah & Steven.
6. Run `python app.py`. The database is created at `instance/into.sqlite3` unless `INTO_DATABASE`
   overrides it.

Never commit `.env` or deploy sandbox credentials to a production Midtrans account. On production,
set `COOKIE_SECURE=true` and serve the site over HTTPS.

## Accounts and authentication

New registrations are staged in the SQLite `pending_users` table at `instance/into.sqlite3` (or the
path configured by `INTO_DATABASE`) for 20 minutes. The table stores an Argon2id password hash and
only a hash of the one-time verification token; raw passwords and raw tokens are never stored. A
valid `GET /api/verify-email?token=...` link atomically moves the account into `users` with
`is_verified=1` and removes the pending row. The verification page then asks the user to log in.
Expired pending rows are rejected and cleaned during startup, registration, resend, and verification
requests. Configure SMTP before registration so verification links can be delivered. Failed email
delivery leaves only a time-limited pending registration, which can be retried with **Kirim ulang
verifikasi** before it expires.

## Midtrans callback and paid-order spreadsheet

Set `MIDTRANS_SERVER_KEY` and `MIDTRANS_CLIENT_KEY` from the same Midtrans environment (sandbox or
production). With sandbox keys, keep `MIDTRANS_IS_PRODUCTION=false`. Register
`https://<your-domain>/api/midtrans/notification` as the Midtrans payment notification URL. The
endpoint verifies Midtrans' SHA-512 signature, checks the order amount, and only accepts settlement
or accepted capture as `PAID`.

After verified payment, the server creates participant ticket codes and sends ticket email, then
posts the paid order to the Apps Script webhook using `INTO_SHEETS_WEBHOOK_URL` and
`INTO_SHEETS_TOKEN`. Apps Script writes one row per participant in the `Pembayaran` tab, including
order/payment details and ticket codes. The order ID makes delivery idempotent; failed delivery can
be retried on Midtrans callback retries and while the buyer checks the order status. The database
records `sheets_synced_at` only after Apps Script confirms success. The frontend's status polling
does not determine whether payment succeeded; the verified Midtrans callback remains authoritative.

Snap presents QRIS, bank transfer/Virtual Account, and enabled e-wallet methods according to the
merchant account configuration. Turn on the needed payment methods in Midtrans and verify the
complete flow with sandbox test accounts before production.

## Vouchers

`HOTSALESMANSA` is seeded as inactive and expired. Add voucher codes with `INTO_VOUCHERS_JSON` in
the server environment. Optional `usage_limit` caps paid and unexpired pending orders; concurrent
checkouts are serialized in SQLite. Example:

```json
{
  "INTOHEMAT": {
    "discount_amount": 10000,
    "expires_at": "2027-01-01T00:00:00+00:00",
    "active": true,
    "referral_type": "school",
    "usage_limit": 250
  }
}
```

Discount values are fixed rupiah amounts, capped at the package price. Restart Flask after changing
voucher environment configuration.

## Dashboard roles

The home page exposes a Brand Ambassador dashboard link for `ba` accounts. Admins can open each
Brand Ambassador's dashboard from the Brand Ambassador directory. The admin dashboard remains at
`/dashboard-admin`; the developer console is at `/dev`. New and existing accounts default to `user`.
For a one-time initial admin or BA role assignment, update an account after verifying it:

```sql
UPDATE users SET role = 'admin' WHERE email = 'admin@example.org';
UPDATE users SET role = 'ba' WHERE email = 'ambassador@example.org';
```

Admins manage Single, Child-Free/Duo, Keluarga Berencana/Trio, and Keluarga Besar/Circle prices
for Early Bird, Presale, Normal, Flashsale, and Sosialisasi from `/dashboard-admin`. Prices can be
switched manually or scheduled by WIB start/end times. Scheduled windows must not overlap; when no
automatic window is active, the selected manual phase is used as the fallback. Set a phase price to
0 when a package is unavailable. New checkouts use the current server-side price, while existing
orders keep their original amount.

The developer console is available at `/dev` to verified accounts with role `dev`. It can update
selected operational SQLite fields, account roles, and account passwords; password changes are
stored as Argon2id hashes. Payment/order tables and authentication hashes or tokens are not editable
in the web console. To grant the first developer account on the deployed database, create and verify
the account, then run this command from the PythonAnywhere project console (with its virtualenv
active):

```bash
flask --app app grant-dev owner@example.com
```

Use the owner's verified email in place of the example. The command uses the database configured by
the deployed app; it does not require downloading or re-uploading `into.sqlite3`.

On their first visit to `/dashboardBA`, Brand Ambassadors choose their own unique 4–20 character
code using letters and numbers. A code can only be created once. Mission proofs (JPG, PNG, or WEBP,
up to 5 MB) are stored in SQLite. Brand Ambassadors can submit a proof;
only admins can approve or reject it, and rejected missions can be resubmitted. Share/school voucher
codes can be marked with `"referral_type": "school"` in `INTO_VOUCHERS_JSON` so the report
separates school codes from Brand Ambassador codes. Only orders in `PAID` status count as successful redemptions.

## Google Sheets setup

Configure the Apps Script Web App and its script properties using
[SHEETS_SETUP.md](SHEETS_SETUP.md). The paid checkout writes to `Pembayaran`. The legacy
`/api/pemesanan` endpoint remains available and writes to `Pemesanan`; both endpoints use the same
private webhook URL and token. Keep the token only in local `.env` and Apps Script Script
Properties, and rotate it if it has been shared.
