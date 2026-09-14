import os
import secrets
from pathlib import Path


class Config:
    APP_NAME = "Contributarr"
    APP_VERSION = "dev"
    BUILD_COMMIT = ""
    TZ = "Asia/Kuala_Lumpur"
    PORT = 9096
    DB_PATH = "/data/contributarr.db"
    EXTERNAL_URL = ""
    SECRET_KEY = ""
    SESSION_TYPE = "cachelib"
    SESSION_FILE_DIR = "/data/sessions"
    SESSION_PERMANENT = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    WTF_CSRF_TIME_LIMIT = None
    PROXY_FIX_COUNT = 0
    SYNC_INTERVAL_MINUTES = 60

    SEERR_URL = ""
    SEERR_API_KEY = ""
    RADARR_1080_URL = ""
    RADARR_1080_API_KEY = ""
    RADARR_4K_URL = ""
    RADARR_4K_API_KEY = ""
    SONARR_URL = ""
    SONARR_API_KEY = ""
    TAUTULLI_URL = ""
    TAUTULLI_API_KEY = ""
    PLEX_CLIENT_IDENTIFIER = ""
    WEBHOOK_SECRET = ""

    @staticmethod
    def environment():
        """Read environment overrides at application creation time."""
        mapping = {
            "APP_NAME": "APP_NAME",
            "APP_VERSION": "APP_VERSION",
            "BUILD_COMMIT": "BUILD_COMMIT",
            "TZ": "TZ",
            "PORT": "PORT",
            "DB_PATH": "DB_PATH",
            "EXTERNAL_URL": "EXTERNAL_URL",
            "SESSION_SECRET": "SECRET_KEY",
            "SESSION_DIR": "SESSION_FILE_DIR",
            "PROXY_FIX_COUNT": "PROXY_FIX_COUNT",
            "SYNC_INTERVAL_MINUTES": "SYNC_INTERVAL_MINUTES",
            "SEERR_URL": "SEERR_URL",
            "SEERR_API_KEY": "SEERR_API_KEY",
            "RADARR_1080_URL": "RADARR_1080_URL",
            "RADARR_1080_API_KEY": "RADARR_1080_API_KEY",
            "RADARR_4K_URL": "RADARR_4K_URL",
            "RADARR_4K_API_KEY": "RADARR_4K_API_KEY",
            "SONARR_URL": "SONARR_URL",
            "SONARR_API_KEY": "SONARR_API_KEY",
            "TAUTULLI_URL": "TAUTULLI_URL",
            "TAUTULLI_API_KEY": "TAUTULLI_API_KEY",
            "PLEX_CLIENT_IDENTIFIER": "PLEX_CLIENT_IDENTIFIER",
            "WEBHOOK_SECRET": "WEBHOOK_SECRET",
        }
        integers = {"PORT", "PROXY_FIX_COUNT", "SYNC_INTERVAL_MINUTES"}
        values = {}
        for environment_name, config_name in mapping.items():
            if environment_name not in os.environ:
                continue
            value = os.environ[environment_name]
            values[config_name] = int(value) if config_name in integers else value
        return values

    @staticmethod
    def _write_secret(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(value + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        finally:
            if temporary.exists():
                temporary.unlink()

    @classmethod
    def _persistent_secret(cls, configured, path):
        if configured:
            # Persist explicit overrides so an existing installation can later
            # remove its .env without reverting to an older generated secret.
            current = path.read_text(encoding="utf-8").strip() if path.exists() else ""
            if current != configured:
                cls._write_secret(path, configured)
            return configured
        if path.exists():
            persisted = path.read_text(encoding="utf-8").strip()
            if persisted:
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
                return persisted
        generated = secrets.token_hex(32)
        cls._write_secret(path, generated)
        return generated

    @classmethod
    def prepare(cls, config):
        data_directory = Path(config["DB_PATH"]).expanduser().parent
        data_directory.mkdir(parents=True, exist_ok=True)
        configured_placeholders = [
            name for name in ("SECRET_KEY", "WEBHOOK_SECRET")
            if "REPLACE" in config.get(name, "")
        ]
        if configured_placeholders:
            raise RuntimeError(
                "Configured secrets still contain placeholders: "
                + ", ".join(configured_placeholders)
            )
        config["SECRET_KEY"] = cls._persistent_secret(
            config.get("SECRET_KEY", ""), data_directory / ".session_secret"
        )
        config["WEBHOOK_SECRET"] = cls._persistent_secret(
            config.get("WEBHOOK_SECRET", ""), data_directory / ".webhook_secret"
        )
        config["EXTERNAL_URL"] = (config.get("EXTERNAL_URL") or "").rstrip("/")
        config["SESSION_COOKIE_SECURE"] = config["EXTERNAL_URL"].startswith("https://")
        Path(config["SESSION_FILE_DIR"]).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate(config, testing=False):
        placeholders = [
            name for name in ("SECRET_KEY", "WEBHOOK_SECRET")
            if "REPLACE" in config.get(name, "")
        ]
        if placeholders:
            raise RuntimeError("Configured secrets still contain placeholders: " + ", ".join(placeholders))
        if int(config["PORT"]) < 1 or int(config["PORT"]) > 65535:
            raise RuntimeError("PORT must be between 1 and 65535.")
        if int(config["PROXY_FIX_COUNT"]) < 0:
            raise RuntimeError("PROXY_FIX_COUNT cannot be negative.")
