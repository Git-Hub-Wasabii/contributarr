import sqlite3
import base64
import hashlib
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g
from cryptography.fernet import Fernet, InvalidToken


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    if "db" not in g:
        path = current_app.config["DB_PATH"]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=30000")
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def transaction(db=None, immediate=False):
    db = db or get_db()
    db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()


def migrate():
    db = get_db()
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    migrations = Path(current_app.root_path).parent / "migrations"
    applied = {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
    for path in sorted(migrations.glob("*.sql")):
        version = path.stem.split("_", 1)[0]
        if version in applied:
            continue
        script = path.read_text(encoding="utf-8")
        db.executescript("BEGIN IMMEDIATE;\n" + script + f"\nINSERT INTO schema_migrations(version, applied_at) VALUES('{version}', '{utcnow()}');\nCOMMIT;")


def import_legacy(source):
    """Import compatible legacy tracker rows without altering the source."""
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(source)
    legacy = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    legacy.row_factory = sqlite3.Row
    tables = {r[0] for r in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db, counts, now = get_db(), {"users": 0, "requests": 0, "ledger": 0}, utcnow()
    with transaction(db, immediate=True):
        if "users" in tables:
            for row in legacy.execute("SELECT * FROM users"):
                cols = set(row.keys())
                external = str(row["seerr_user_id"] if "seerr_user_id" in cols else row["id"])
                username = str(row["username"] if "username" in cols else row["display_name"] if "display_name" in cols else f"User {external}")
                if db.execute("SELECT 1 FROM identity_mappings WHERE provider='seerr' AND external_id=?", (external,)).fetchone(): continue
                cur = db.execute("INSERT INTO users(display_name,contribution_myr,quota_bytes,created_at,updated_at) VALUES(?,?,?,?,?)", (username, float(row["contribution_myr"] if "contribution_myr" in cols else row["contribution"] if "contribution" in cols else 0), int(row["quota_bytes"] if "quota_bytes" in cols else 0), now, now))
                db.execute("INSERT INTO identity_mappings(user_id,provider,external_id,username,normalized_username) VALUES(?,?,?,?,?)", (cur.lastrowid, "seerr", external, username, " ".join(username.strip().casefold().split())))
                counts["users"] += 1
        if "requests" in tables:
            for row in legacy.execute("SELECT * FROM requests"):
                cols = set(row.keys())
                rid = int(row["seerr_request_id"] if "seerr_request_id" in cols else row["request_id"] if "request_id" in cols else row["id"])
                if db.execute("SELECT 1 FROM requests WHERE seerr_request_id=?", (rid,)).fetchone(): continue
                external = str(row["seerr_user_id"] if "seerr_user_id" in cols else row["user_id"] if "user_id" in cols else "unknown")
                mapping = db.execute("SELECT user_id,username FROM identity_mappings WHERE provider='seerr' AND external_id=?", (external,)).fetchone()
                username = mapping["username"] if mapping else str(row["username"] if "username" in cols else row["display_username"] if "display_username" in cols else f"User {external}")
                if mapping: user_id = mapping["user_id"]
                else:
                    cur = db.execute("INSERT INTO users(display_name,created_at,updated_at) VALUES(?,?,?)", (username, now, now)); user_id = cur.lastrowid
                    db.execute("INSERT INTO identity_mappings(user_id,provider,external_id,username,normalized_username) VALUES(?,?,?,?,?)", (user_id, "seerr", external, username, " ".join(username.strip().casefold().split())))
                charged = int(row["charged_bytes"] if "charged_bytes" in cols else row["size_bytes"] if "size_bytes" in cols else 0)
                title = str(row["title"] if "title" in cols else f"Legacy request {rid}")
                media_type = str(row["media_type"] if "media_type" in cols else "unknown")
                created = str(row["first_seen_at"] if "first_seen_at" in cols else row["created_at"] if "created_at" in cols else now)
                cur = db.execute("INSERT INTO requests(seerr_request_id,user_id,seerr_user_id,display_username,title,media_type,charged_bytes,first_seen_at,last_updated_at,seerr_status,raw_metadata,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (rid,user_id,external,username,title,media_type,charged,created,now,"legacy","{\"legacy_import\":true}",created))
                if charged > 0:
                    db.execute("INSERT INTO usage_ledger(request_id,user_id,delta_bytes,total_charged_bytes,source,created_at) VALUES(?,?,?,?,?,?)", (cur.lastrowid,user_id,charged,charged,"Legacy tracker",created)); counts["ledger"] += 1
                counts["requests"] += 1
    legacy.close()
    return counts


def claim_owner(plex_id, username, email):
    db = get_db()
    with transaction(db, immediate=True):
        owner = db.execute("SELECT * FROM owner WHERE singleton=1").fetchone()
        if owner is None:
            db.execute(
                "INSERT INTO owner(singleton,plex_id,username,email,claimed_at) VALUES(1,?,?,?,?)",
                (str(plex_id), username, email, utcnow()),
            )
            return True
        if owner["plex_id"] == str(plex_id):
            db.execute("UPDATE owner SET username=?, email=? WHERE singleton=1", (username, email))
            return True
        return False


def owner_record():
    return get_db().execute("SELECT * FROM owner WHERE singleton=1").fetchone()


def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    get_db().execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _fernet():
    digest = hashlib.sha256(current_app.config["SECRET_KEY"].encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def set_secret(key, value):
    set_setting(key, "fernet:" + _fernet().encrypt(value.encode("utf-8")).decode("ascii"))


def get_secret(key, default=""):
    value = get_setting(key)
    if not value: return default
    if not value.startswith("fernet:"): return value  # Upgrade path for pre-encryption development data.
    try: return _fernet().decrypt(value[7:].encode("ascii")).decode("utf-8")
    except InvalidToken: raise RuntimeError("A stored integration secret cannot be decrypted. Restore the original SESSION_SECRET.")


def integration_config(name):
    """Return database-managed integration configuration with env fallback for upgrades."""
    name = name.upper()
    return {
        "url": get_setting(f"integration.{name}.url", current_app.config.get(f"{name}_URL", "")),
        "api_key": get_secret(f"integration.{name}.api_key", current_app.config.get(f"{name}_API_KEY", "")),
    }


def initialize_instance_identity():
    stored = get_setting("plex_client_identifier")
    identifier = stored or current_app.config.get("PLEX_CLIENT_IDENTIFIER")
    if not identifier:
        identifier = uuid.uuid4().hex
    if not stored:
        set_setting("plex_client_identifier", identifier)
    current_app.config["PLEX_CLIENT_IDENTIFIER"] = identifier
    return identifier
