import pytest

from app import create_app
from app.db import claim_owner, get_db, utcnow


@pytest.fixture
def app(tmp_path):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DB_PATH": str(tmp_path / "contributarr.db"),
        "SESSION_TYPE": "cachelib",
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "WTF_CSRF_ENABLED": True,
        "WEBHOOK_SECRET": "webhook-test",
        "TAUTULLI_URL": "",
        "TAUTULLI_API_KEY": "",
    })
    yield app


@pytest.fixture
def client(app): return app.test_client()


@pytest.fixture
def owner_client(app, client):
    with app.app_context(): claim_owner("plex-1", "Owner", "owner@example.test")
    with client.session_transaction() as session:
        session["plex_id"] = "plex-1"
        session["plex_username"] = "Owner"
    return client


@pytest.fixture
def seeded_user(app):
    with app.app_context():
        db, now = get_db(), utcnow()
        cur = db.execute("INSERT INTO users(display_name,quota_bytes,created_at,updated_at) VALUES(?,?,?,?)", ("Alice", 50_000_000_000, now, now))
        user_id = cur.lastrowid
        db.execute("INSERT INTO identity_mappings(user_id,provider,external_id,username,normalized_username) VALUES(?,?,?,?,?)", (user_id, "seerr", "42", "Alice", "alice"))
        return user_id
