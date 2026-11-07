-- Core SQLite schema. init_store() applies this schema automatically on Flask startup.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  full_name TEXT NOT NULL,
  school TEXT NOT NULL,
  phone TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  is_verified INTEGER NOT NULL DEFAULT 0,
  role TEXT NOT NULL DEFAULT 'user',
  referral_code TEXT UNIQUE COLLATE NOCASE,
  verify_token_hash TEXT,
  verify_expires_at TEXT,
  created_at TEXT NOT NULL
);

-- New registrations stay here for 20 minutes and are promoted to users only
-- after the one-time email verification token is redeemed.
CREATE TABLE IF NOT EXISTS pending_users (
  id INTEGER PRIMARY KEY,
  full_name TEXT NOT NULL,
  school TEXT NOT NULL,
  phone TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  verify_token_hash TEXT UNIQUE,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vouchers (
  code TEXT PRIMARY KEY COLLATE NOCASE,
  discount_amount INTEGER NOT NULL,
  expires_at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  referral_type TEXT NOT NULL DEFAULT 'other',
  usage_limit INTEGER
);

CREATE TABLE IF NOT EXISTS ticket_pricing_settings (
  id INTEGER PRIMARY KEY CHECK(id=1),
  mode TEXT NOT NULL DEFAULT 'manual',
  manual_phase TEXT NOT NULL DEFAULT 'Normal'
);

CREATE TABLE IF NOT EXISTS ticket_phase_prices (
  phase TEXT NOT NULL,
  package TEXT NOT NULL,
  price INTEGER NOT NULL,
  starts_at TEXT,
  ends_at TEXT,
  PRIMARY KEY(phase, package)
);

CREATE TABLE IF NOT EXISTS voucher_discount_settings (
  referral_type TEXT NOT NULL CHECK(referral_type IN ('school','ba')),
  package TEXT NOT NULL,
  discount_amount INTEGER CHECK(discount_amount IS NULL OR discount_amount >= 0),
  PRIMARY KEY(referral_type, package)
);

CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  package TEXT NOT NULL,
  amount INTEGER NOT NULL,
  discount_amount INTEGER NOT NULL DEFAULT 0,
  voucher_code TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING',
  midtrans_token TEXT,
  midtrans_redirect_url TEXT,
  created_at TEXT NOT NULL,
  paid_at TEXT,
  email_sent_at TEXT,
  sheets_synced_at TEXT,
  sheets_claimed_at TEXT
);

CREATE TABLE IF NOT EXISTS ba_missions (
  mission_key TEXT PRIMARY KEY,
  stage TEXT NOT NULL,
  title TEXT NOT NULL,
  sort_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ba_referral_codes (
  code TEXT PRIMARY KEY COLLATE NOCASE,
  ba_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_migrations (
  name TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ba_mission_submissions (
  id INTEGER PRIMARY KEY,
  ba_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  mission_key TEXT NOT NULL REFERENCES ba_missions(mission_key),
  file_name TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  evidence BLOB NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING',
  submitted_at TEXT NOT NULL,
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT,
  review_note TEXT,
  UNIQUE(ba_user_id, mission_key)
);

CREATE TABLE IF NOT EXISTS order_participants (
  id INTEGER PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  full_name TEXT NOT NULL,
  school TEXT NOT NULL,
  email TEXT NOT NULL,
  phone TEXT NOT NULL,
  ticket_code TEXT UNIQUE,
  email_sent_at TEXT,
  email_claimed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_order_participants_order ON order_participants(order_id);
CREATE INDEX IF NOT EXISTS idx_pending_users_expiry ON pending_users(expires_at);

-- HOTSALESMANSA is seeded as inactive/expired by init_store().
