"""SQLite storage: travels, users, bookings, and a log of every change.

The database is a single file, travels.db, created next to this module on
first run. init_db() also migrates databases created by earlier versions, so
existing rows are kept.
"""

import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from flask import g
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = Path(__file__).parent / "travels.db"

# HTTP is stateless: nobody stays "connected" to a web server. A user counts as
# online if a request carrying their session arrived within this window.
ONLINE_WINDOW = timedelta(minutes=2)

# The first account, created only when the users table is empty.
DEFAULT_ADMIN = {"username": "admin", "password": "admin123", "full_name": "Administrator"}

ROLES = ("admin", "client")

SCHEMA = """
CREATE TABLE IF NOT EXISTS travels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    summary    TEXT    NOT NULL DEFAULT '',
    duration   TEXT    NOT NULL DEFAULT '',
    price      REAL    NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    full_name     TEXT    NOT NULL DEFAULT '',
    email         TEXT    NOT NULL DEFAULT '',
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'client',   -- admin | client
    is_active     INTEGER NOT NULL DEFAULT 1,
    -- Billing details, remembered so a returning client does not retype them.
    cin           TEXT    NOT NULL DEFAULT '',
    phone         TEXT    NOT NULL DEFAULT '',
    address       TEXT    NOT NULL DEFAULT '',
    city          TEXT    NOT NULL DEFAULT '',
    postal_code   TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    last_login    TEXT,
    last_seen     TEXT,                                -- NULL once logged out
    -- Random value issued at login and checked on every request. Clearing it
    -- invalidates that user's cookie server-side, so signing out really ends
    -- the session instead of only asking the browser to forget it.
    session_token TEXT
);

CREATE TABLE IF NOT EXISTS bookings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    -- A booking outlives its travel: the name and price are copied in here so
    -- the client's history still reads correctly after an admin deletes it.
    travel_id   INTEGER          REFERENCES travels(id) ON DELETE SET NULL,
    travel_name TEXT    NOT NULL,
    unit_price  REAL    NOT NULL,
    travellers  INTEGER NOT NULL DEFAULT 1,
    total_price REAL    NOT NULL,
    notes       TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'confirmed',  -- confirmed | cancelled
    created_at  TEXT    NOT NULL,
    depart_date TEXT    NOT NULL DEFAULT '',           -- ISO yyyy-mm-dd

    -- Who is travelling and where to send the paperwork. Copied onto the
    -- booking rather than read from the user, so later profile edits do not
    -- rewrite the details a past booking was made with.
    bill_name        TEXT NOT NULL DEFAULT '',
    bill_cin         TEXT NOT NULL DEFAULT '',
    bill_phone       TEXT NOT NULL DEFAULT '',
    bill_address     TEXT NOT NULL DEFAULT '',
    bill_city        TEXT NOT NULL DEFAULT '',
    bill_postal_code TEXT NOT NULL DEFAULT '',

    -- Payment. LAB ONLY: these are stored in clear text for a university
    -- exercise with made-up data. Card networks forbid keeping a CVV at all,
    -- and keeping a full card number requires PCI DSS compliance, so never
    -- point this schema at a real card.
    payment_method TEXT NOT NULL DEFAULT 'agency',     -- card | transfer | agency
    card_holder    TEXT NOT NULL DEFAULT '',
    card_number    TEXT NOT NULL DEFAULT '',
    card_expiry    TEXT NOT NULL DEFAULT '',
    card_cvv       TEXT NOT NULL DEFAULT '',
    payment_status TEXT NOT NULL DEFAULT 'pending'     -- pending | paid
);

CREATE TABLE IF NOT EXISTS activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT    NOT NULL,
    action      TEXT    NOT NULL,  -- created|updated|deleted|booked|cancelled|login|logout
    target_kind TEXT    NOT NULL DEFAULT 'travel',  -- travel | user | booking | session
    target_id   INTEGER,
    target_name TEXT    NOT NULL,
    details     TEXT    NOT NULL DEFAULT '',
    actor_name  TEXT    NOT NULL DEFAULT '',
    actor_ip    TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS activity_id_idx   ON activity (id DESC);
CREATE INDEX IF NOT EXISTS bookings_user_idx ON bookings (user_id);
"""

SEED_TRAVELS = [
    ("Santorini, Greece", "Whitewashed villages, caldera sunsets and black-sand beaches.", "7 days / 6 nights", 1450),
    ("Istanbul, Turkey", "Two continents in one city: bazaars, imperial mosques, a Bosphorus cruise.", "5 days / 4 nights", 890),
    ("Marrakech, Morocco", "The medina, traditional riads and a night in the Agafay dunes.", "4 days / 3 nights", 650),
    ("Dubai, UAE", "Skyscrapers, a desert safari and shopping without limits.", "6 days / 5 nights", 1780),
]

TRAVEL_FIELDS = ("name", "summary", "duration", "price")
USER_FIELDS = ("username", "full_name", "email", "role", "is_active")
USER_LABELS = {
    "username": "username", "full_name": "name", "email": "email",
    "role": "role", "is_active": "active",
}


def now():
    """Local time, to the second. ISO text so it sorts correctly as a string."""
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def money(value):
    """1450.0 -> '1450', 1450.5 -> '1450.50'. Keeps the log readable."""
    amount = float(value)
    return f"{amount:.0f}" if amount == int(amount) else f"{amount:.2f}"


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # off by default in SQLite
    return conn


def get_db():
    """One connection per request, reused across calls within that request."""
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --- setup and migration -------------------------------------------------

def columns(conn, table):
    return {row["name"] for row in conn.execute("PRAGMA table_info(" + table + ")")}


def add_missing(conn, table, wanted):
    """Add any of `wanted` ({column: declaration}) that the table lacks."""
    have = columns(conn, table)
    for name, decl in wanted.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


TEXT_DEFAULT = "TEXT NOT NULL DEFAULT ''"


def migrate(conn):
    """Bring a database written by an earlier version up to the current schema."""
    if "session_token" not in columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN session_token TEXT")

    add_missing(conn, "users", {
        "cin": TEXT_DEFAULT, "phone": TEXT_DEFAULT, "address": TEXT_DEFAULT,
        "city": TEXT_DEFAULT, "postal_code": TEXT_DEFAULT,
    })

    add_missing(conn, "bookings", {
        "depart_date": TEXT_DEFAULT,
        "bill_name": TEXT_DEFAULT, "bill_cin": TEXT_DEFAULT,
        "bill_phone": TEXT_DEFAULT, "bill_address": TEXT_DEFAULT,
        "bill_city": TEXT_DEFAULT, "bill_postal_code": TEXT_DEFAULT,
        "payment_method": "TEXT NOT NULL DEFAULT 'agency'",
        "card_holder": TEXT_DEFAULT, "card_number": TEXT_DEFAULT,
        "card_expiry": TEXT_DEFAULT, "card_cvv": TEXT_DEFAULT,
        "payment_status": "TEXT NOT NULL DEFAULT 'pending'",
    })

    cols = columns(conn, "activity")
    if not cols:
        return
    # The first version logged travels only, under travel_id / travel_name,
    # and had no actor name because there were no accounts yet.
    if "travel_id" in cols and "target_id" not in cols:
        conn.execute("ALTER TABLE activity RENAME COLUMN travel_id TO target_id")
        conn.execute("ALTER TABLE activity RENAME COLUMN travel_name TO target_name")
    if "target_kind" not in cols:
        conn.execute("ALTER TABLE activity ADD COLUMN target_kind TEXT NOT NULL DEFAULT 'travel'")
    if "actor_name" not in cols:
        conn.execute("ALTER TABLE activity ADD COLUMN actor_name TEXT NOT NULL DEFAULT ''")


def init_db():
    """Create tables if missing, migrate old ones, seed travels and the admin."""
    conn = connect()
    with conn:
        conn.executescript(SCHEMA)
        migrate(conn)

        if conn.execute("SELECT COUNT(*) FROM travels").fetchone()[0] == 0:
            stamp = now()
            conn.executemany(
                "INSERT INTO travels (name, summary, duration, price, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(n, s, d, p, stamp, stamp) for n, s, d, p in SEED_TRAVELS],
            )

        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO users (username, full_name, email, password_hash, role,"
                " is_active, created_at) VALUES (?, ?, ?, ?, 'admin', 1, ?)",
                (
                    DEFAULT_ADMIN["username"], DEFAULT_ADMIN["full_name"], "",
                    generate_password_hash(DEFAULT_ADMIN["password"]), now(),
                ),
            )
    conn.close()


def using_default_admin_password():
    """True while the seeded admin still has the password printed in the docs."""
    conn = connect()
    row = conn.execute(
        "SELECT password_hash FROM users WHERE username = ?",
        (DEFAULT_ADMIN["username"],),
    ).fetchone()
    conn.close()
    if row is None:
        return False
    return check_password_hash(row["password_hash"], DEFAULT_ADMIN["password"])


# --- activity log --------------------------------------------------------

def log(action, kind, target_id, target_name, details, actor):
    """Append to the log. `actor` is the (name, ip) of whoever made the change.

    Called from inside a `with db:` block, the entry commits with the change it
    describes, so the two can never disagree. Called on its own — a sign-in, say
    — it has to commit itself, or the insert would be discarded when the
    request's connection closes.
    """
    db = get_db()
    standalone = not db.in_transaction
    name, ip = actor
    db.execute(
        "INSERT INTO activity (at, action, target_kind, target_id, target_name,"
        " details, actor_name, actor_ip) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now(), action, kind, target_id, target_name, details, name, ip),
    )
    if standalone:
        db.commit()


def list_activity(limit=200):
    return get_db().execute(
        "SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


# --- travels -------------------------------------------------------------

def list_travels():
    return get_db().execute("SELECT * FROM travels ORDER BY name COLLATE NOCASE").fetchall()


def get_travel(travel_id):
    return get_db().execute("SELECT * FROM travels WHERE id = ?", (travel_id,)).fetchone()


def create_travel(data, actor):
    db = get_db()
    stamp = now()
    with db:
        cur = db.execute(
            "INSERT INTO travels (name, summary, duration, price, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (data["name"], data["summary"], data["duration"], data["price"], stamp, stamp),
        )
        log("created", "travel", cur.lastrowid, data["name"], "", actor)
    return cur.lastrowid


def diff_travel(before, data):
    """Human-readable summary of what an edit changed."""
    changes = []
    for field in TRAVEL_FIELDS:
        old, new = before[field], data[field]
        if field == "price":
            if float(old) == float(new):
                continue
            changes.append("price: " + money(old) + " -> " + money(new))
        elif old != new:
            changes.append(f"{field}: {old} -> {new}")
    return "; ".join(changes)


def update_travel(travel_id, data, actor):
    """Returns the change summary, or None when nothing actually changed."""
    db = get_db()
    changes = diff_travel(get_travel(travel_id), data)
    if not changes:
        return None
    with db:
        db.execute(
            "UPDATE travels SET name = ?, summary = ?, duration = ?, price = ?,"
            " updated_at = ? WHERE id = ?",
            (data["name"], data["summary"], data["duration"], data["price"],
             now(), travel_id),
        )
        log("updated", "travel", travel_id, data["name"], changes, actor)
    return changes


def delete_travel(travel_id, actor):
    db = get_db()
    travel = get_travel(travel_id)
    if travel is None:
        return None
    with db:
        db.execute("DELETE FROM travels WHERE id = ?", (travel_id,))
        log("deleted", "travel", travel_id, travel["name"],
            "price was " + money(travel["price"]), actor)
    return travel["name"]


# --- users ---------------------------------------------------------------

def list_users():
    """Every account, with how many live bookings each one holds."""
    return get_db().execute(
        "SELECT u.*, (SELECT COUNT(*) FROM bookings b"
        "             WHERE b.user_id = u.id AND b.status = 'confirmed') AS booking_count"
        " FROM users u ORDER BY u.role, u.username COLLATE NOCASE"
    ).fetchall()


def get_user(user_id):
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(username):
    return get_db().execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()


def count_admins(exclude_id=None):
    """Used to refuse the change that would leave nobody able to administer."""
    sql = "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1"
    params = ()
    if exclude_id is not None:
        sql += " AND id != ?"
        params = (exclude_id,)
    return get_db().execute(sql, params).fetchone()[0]


def create_user(data, password, actor):
    db = get_db()
    with db:
        cur = db.execute(
            "INSERT INTO users (username, full_name, email, password_hash, role,"
            " is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (data["username"], data["full_name"], data["email"],
             generate_password_hash(password), data["role"],
             int(data["is_active"]), now()),
        )
        log("created", "user", cur.lastrowid, data["username"],
            "role: " + data["role"], actor)
    return cur.lastrowid


def update_user(user_id, data, password, actor):
    """Returns the change summary. The password changes only when non-empty."""
    db = get_db()
    before = get_user(user_id)
    changes = []
    for field in USER_FIELDS:
        old, new = before[field], data[field]
        if field == "is_active":
            old, new = int(old), int(new)
            if old != new:
                changes.append("activated" if new else "deactivated")
            continue
        if old != new:
            changes.append(f"{USER_LABELS[field]}: {old or 'none'} -> {new or 'none'}")
    if password:
        changes.append("password reset")

    if not changes:
        return None

    with db:
        db.execute(
            "UPDATE users SET username = ?, full_name = ?, email = ?, role = ?,"
            " is_active = ? WHERE id = ?",
            (data["username"], data["full_name"], data["email"], data["role"],
             int(data["is_active"]), user_id),
        )
        if password:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (generate_password_hash(password), user_id))
        log("updated", "user", user_id, data["username"], "; ".join(changes), actor)
    return "; ".join(changes)


def delete_user(user_id, actor):
    db = get_db()
    user = get_user(user_id)
    if user is None:
        return None
    with db:
        # ON DELETE CASCADE removes that user's bookings in the same statement.
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        log("deleted", "user", user_id, user["username"],
            "role was " + user["role"], actor)
    return user["username"]


def change_password(user_id, new_password, actor):
    db = get_db()
    user = get_user(user_id)
    with db:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                   (generate_password_hash(new_password), user_id))
        log("updated", "user", user_id, user["username"], "password changed", actor)


def verify_login(username, password):
    """Returns the user row on success, or None. Deactivated accounts fail."""
    user = get_user_by_username(username)
    if user is None or not user["is_active"]:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    return user


# --- presence ------------------------------------------------------------

def mark_login(user_id):
    """Issues a fresh session token and returns it to be put in the cookie.

    One token per account, so signing in somewhere else ends the earlier session.
    """
    token = secrets.token_urlsafe(32)
    db = get_db()
    stamp = now()
    with db:
        db.execute(
            "UPDATE users SET last_login = ?, last_seen = ?, session_token = ?"
            " WHERE id = ?",
            (stamp, stamp, token, user_id),
        )
    return token


def mark_logout(user_id):
    """Clearing the token ends the session for good; clearing last_seen reddens
    the dot immediately rather than after the online window elapses."""
    db = get_db()
    with db:
        db.execute(
            "UPDATE users SET last_seen = NULL, session_token = NULL WHERE id = ?",
            (user_id,),
        )


def session_is_valid(user, token):
    """A cookie is only good while it carries the token currently on the account."""
    stored = user["session_token"]
    if not stored or not token:
        return False
    return secrets.compare_digest(stored, token)


def touch(user_id):
    """Called on every request to keep the signed-in user's presence fresh."""
    db = get_db()
    with db:
        db.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now(), user_id))


def is_online(last_seen):
    if not last_seen:
        return False
    try:
        seen = datetime.fromisoformat(last_seen)
    except ValueError:
        return False
    return datetime.now() - seen <= ONLINE_WINDOW


# --- bookings ------------------------------------------------------------

PAYMENT_METHODS = ("card", "transfer", "agency")
PAYMENT_LABELS = {
    "card": "Card",
    "transfer": "Bank transfer",
    "agency": "Pay at the agency",
}


def mask_card(number):
    """4111111111111111 -> '**** **** **** 1111', for display in listings."""
    digits = "".join(c for c in (number or "") if c.isdigit())
    if len(digits) < 4:
        return ""
    return "**** **** **** " + digits[-4:]


def create_booking(user, travel, travellers, form, actor):
    """`form` carries the billing details and the payment block from checkout."""
    db = get_db()
    total = float(travel["price"]) * travellers
    paid = form["payment_method"] == "card"
    with db:
        cur = db.execute(
            "INSERT INTO bookings (user_id, travel_id, travel_name, unit_price,"
            " travellers, total_price, notes, status, created_at, depart_date,"
            " bill_name, bill_cin, bill_phone, bill_address, bill_city,"
            " bill_postal_code, payment_method, card_holder, card_number,"
            " card_expiry, card_cvv, payment_status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?,"
            "         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], travel["id"], travel["name"], travel["price"],
             travellers, total, form["notes"], now(), form["depart_date"],
             form["bill_name"], form["bill_cin"], form["bill_phone"],
             form["bill_address"], form["bill_city"], form["bill_postal_code"],
             form["payment_method"], form["card_holder"], form["card_number"],
             form["card_expiry"], form["card_cvv"],
             "paid" if paid else "pending"),
        )
        # The log records how it was paid, never the card details themselves.
        log("booked", "booking", cur.lastrowid, travel["name"],
            f"{travellers} traveller(s), total {money(total)}, "
            f"{PAYMENT_LABELS[form['payment_method']].lower()}", actor)
    return cur.lastrowid


def save_billing_details(user_id, form):
    """Remember the details on the account so the next checkout is prefilled."""
    db = get_db()
    with db:
        db.execute(
            "UPDATE users SET full_name = CASE WHEN full_name = '' THEN ? ELSE full_name END,"
            " cin = ?, phone = ?, address = ?, city = ?, postal_code = ?"
            " WHERE id = ?",
            (form["bill_name"], form["bill_cin"], form["bill_phone"],
             form["bill_address"], form["bill_city"], form["bill_postal_code"],
             user_id),
        )


def get_booking(booking_id):
    return get_db().execute(
        "SELECT b.*, u.username FROM bookings b JOIN users u ON u.id = b.user_id"
        " WHERE b.id = ?", (booking_id,)
    ).fetchone()


def list_bookings_for_user(user_id):
    return get_db().execute(
        "SELECT * FROM bookings WHERE user_id = ? ORDER BY id DESC", (user_id,)
    ).fetchall()


def list_all_bookings():
    return get_db().execute(
        "SELECT b.*, u.username, u.full_name FROM bookings b"
        " JOIN users u ON u.id = b.user_id ORDER BY b.id DESC"
    ).fetchall()


def cancel_booking(booking_id, actor):
    """Cancelled rather than deleted, so the history and the log stay intact."""
    db = get_db()
    booking = get_booking(booking_id)
    if booking is None or booking["status"] == "cancelled":
        return None
    with db:
        db.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
        log("cancelled", "booking", booking_id, booking["travel_name"],
            "for " + booking["username"], actor)
    return booking["travel_name"]
