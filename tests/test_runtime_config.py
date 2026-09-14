from urllib.parse import parse_qs, urlsplit

from app import create_app
from app.db import get_db, get_setting, set_setting


ENVIRONMENT_NAMES = (
    "APP_NAME", "APP_VERSION", "BUILD_COMMIT", "TZ", "PORT", "DB_PATH",
    "EXTERNAL_URL", "SESSION_SECRET", "SESSION_DIR", "WEBHOOK_SECRET",
    "SYNC_INTERVAL_MINUTES", "PROXY_FIX_COUNT",
)


class FakePinResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"id": 1, "code": "CI-PIN"}


def clean_environment(monkeypatch):
    for name in ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_startup_without_environment_generates_distinct_persistent_secrets_and_database(tmp_path, monkeypatch):
    clean_environment(monkeypatch)
    data = tmp_path / "data"
    config = {
        "TESTING": True,
        "DB_PATH": str(data / "contributarr.db"),
        "SESSION_FILE_DIR": str(data / "sessions"),
    }
    first = create_app(config)
    first_session_secret = first.config["SECRET_KEY"]
    first_webhook_secret = first.config["WEBHOOK_SECRET"]

    assert first.config["APP_NAME"] == "Contributarr"
    assert first.config["APP_VERSION"] == "dev"
    assert first.config["BUILD_COMMIT"] == ""
    assert first.config["TZ"] == "Asia/Kuala_Lumpur"
    assert first.config["PORT"] == 9096
    assert first.config["SYNC_INTERVAL_MINUTES"] == 60
    assert first.config["PROXY_FIX_COUNT"] == 0
    assert first.config["EXTERNAL_URL"] == ""
    assert len(first_session_secret) == 64
    assert len(first_webhook_secret) == 64
    assert first_session_secret != first_webhook_secret
    assert (data / ".session_secret").read_text(encoding="utf-8").strip() == first_session_secret
    assert (data / ".webhook_secret").read_text(encoding="utf-8").strip() == first_webhook_secret

    with first.app_context():
        set_setting("persistence-test", "retained")
    second = create_app(config)
    assert second.config["SECRET_KEY"] == first_session_secret
    assert second.config["WEBHOOK_SECRET"] == first_webhook_secret
    with second.app_context():
        assert get_setting("persistence-test") == "retained"

    assert second.test_client().get("/health").status_code == 200


def test_existing_secret_files_are_reused(tmp_path, monkeypatch):
    clean_environment(monkeypatch)
    data = tmp_path / "data"
    data.mkdir()
    (data / ".session_secret").write_text("persisted-session\n", encoding="utf-8")
    (data / ".webhook_secret").write_text("persisted-webhook\n", encoding="utf-8")
    app = create_app({
        "TESTING": True,
        "DB_PATH": str(data / "contributarr.db"),
        "SESSION_FILE_DIR": str(data / "sessions"),
    })
    assert app.config["SECRET_KEY"] == "persisted-session"
    assert app.config["WEBHOOK_SECRET"] == "persisted-webhook"


def test_environment_secrets_override_and_replace_persisted_values(tmp_path, monkeypatch):
    clean_environment(monkeypatch)
    data = tmp_path / "data"
    data.mkdir()
    (data / ".session_secret").write_text("old-session\n", encoding="utf-8")
    (data / ".webhook_secret").write_text("old-webhook\n", encoding="utf-8")
    monkeypatch.setenv("SESSION_SECRET", "environment-session")
    monkeypatch.setenv("WEBHOOK_SECRET", "environment-webhook")
    config = {
        "TESTING": True,
        "DB_PATH": str(data / "contributarr.db"),
        "SESSION_FILE_DIR": str(data / "sessions"),
    }
    overridden = create_app(config)
    assert overridden.config["SECRET_KEY"] == "environment-session"
    assert overridden.config["WEBHOOK_SECRET"] == "environment-webhook"
    assert (data / ".session_secret").read_text(encoding="utf-8").strip() == "environment-session"
    assert (data / ".webhook_secret").read_text(encoding="utf-8").strip() == "environment-webhook"

    monkeypatch.delenv("SESSION_SECRET")
    monkeypatch.delenv("WEBHOOK_SECRET")
    migrated = create_app(config)
    assert migrated.config["SECRET_KEY"] == "environment-session"
    assert migrated.config["WEBHOOK_SECRET"] == "environment-webhook"


def test_standard_environment_values_override_application_defaults(tmp_path, monkeypatch):
    clean_environment(monkeypatch)
    data = tmp_path / "custom-data"
    monkeypatch.setenv("APP_NAME", "Custom Contributarr")
    monkeypatch.setenv("APP_VERSION", "v1.2.3-beta.1")
    monkeypatch.setenv("BUILD_COMMIT", "0123456789abcdef")
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("PORT", "9191")
    monkeypatch.setenv("DB_PATH", str(data / "custom.db"))
    monkeypatch.setenv("SESSION_DIR", str(data / "custom-sessions"))
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "15")
    monkeypatch.setenv("PROXY_FIX_COUNT", "2")
    app = create_app({"TESTING": True})
    assert app.config["APP_NAME"] == "Custom Contributarr"
    assert app.config["APP_VERSION"] == "v1.2.3-beta.1"
    assert app.config["BUILD_COMMIT"] == "0123456789abcdef"
    assert app.config["TZ"] == "UTC"
    assert app.config["PORT"] == 9191
    assert app.config["DB_PATH"] == str(data / "custom.db")
    assert app.config["SESSION_FILE_DIR"] == str(data / "custom-sessions")
    assert app.config["SYNC_INTERVAL_MINUTES"] == 15
    assert app.config["PROXY_FIX_COUNT"] == 2


def test_plex_callback_uses_request_origin_when_external_url_is_absent(tmp_path, monkeypatch):
    clean_environment(monkeypatch)
    monkeypatch.setattr("app.auth.requests.post", lambda *_args, **_kwargs: FakePinResponse())
    app = create_app({
        "TESTING": True,
        "DB_PATH": str(tmp_path / "data" / "contributarr.db"),
        "SESSION_FILE_DIR": str(tmp_path / "data" / "sessions"),
    })
    response = app.test_client().get("/auth/plex/start", base_url="http://192.168.1.50:9096")
    parameters = parse_qs(urlsplit(response.location).fragment.removeprefix("?"))
    assert parameters["forwardUrl"] == ["http://192.168.1.50:9096/auth/plex/callback"]
    assert app.config["SESSION_COOKIE_SECURE"] is False


def test_explicit_external_url_overrides_request_origin(tmp_path, monkeypatch):
    clean_environment(monkeypatch)
    monkeypatch.setenv("EXTERNAL_URL", "https://contributarr.example.test/")
    monkeypatch.setattr("app.auth.requests.post", lambda *_args, **_kwargs: FakePinResponse())
    app = create_app({
        "TESTING": True,
        "DB_PATH": str(tmp_path / "data" / "contributarr.db"),
        "SESSION_FILE_DIR": str(tmp_path / "data" / "sessions"),
    })
    response = app.test_client().get("/auth/plex/start", base_url="http://192.168.1.50:9096")
    parameters = parse_qs(urlsplit(response.location).fragment.removeprefix("?"))
    assert parameters["forwardUrl"] == ["https://contributarr.example.test/auth/plex/callback"]
    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_compose_is_self_contained_and_uses_named_volume():
    compose = open("docker-compose.yml", encoding="utf-8").read()
    assert "ghcr.io/git-hub-wasabii/contributarr:latest" in compose
    assert "contributarr-data:/data" in compose
    assert "name: contributarr-data" in compose
    assert "./data:/data" not in compose
    assert "${" not in compose
    assert "SESSION_SECRET" not in compose
    assert "WEBHOOK_SECRET" not in compose
    assert "EXTERNAL_URL" not in compose
