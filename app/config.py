import os
from pathlib import Path


class Config:
    APP_NAME = os.getenv("APP_NAME", "Contributarr")
    APP_VERSION = "v26.09.14"
    TZ = os.getenv("TZ", "Asia/Kuala_Lumpur")
    PORT = int(os.getenv("PORT", "9096"))
    DB_PATH = os.getenv("DB_PATH", "/data/contributarr.db")
    EXTERNAL_URL = os.getenv("EXTERNAL_URL", "http://localhost:9096").rstrip("/")
    SECRET_KEY = os.getenv("SESSION_SECRET", "")
    SESSION_TYPE = "cachelib"
    SESSION_FILE_DIR = os.getenv("SESSION_DIR", "/data/sessions")
    SESSION_PERMANENT = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = EXTERNAL_URL.startswith("https://")
    WTF_CSRF_TIME_LIMIT = None
    PROXY_FIX_COUNT = int(os.getenv("PROXY_FIX_COUNT", "0"))
    SYNC_INTERVAL_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "60"))

    SEERR_URL = os.getenv("SEERR_URL", "").rstrip("/")
    SEERR_API_KEY = os.getenv("SEERR_API_KEY", "")
    RADARR_1080_URL = os.getenv("RADARR_1080_URL", "").rstrip("/")
    RADARR_1080_API_KEY = os.getenv("RADARR_1080_API_KEY", "")
    RADARR_4K_URL = os.getenv("RADARR_4K_URL", "").rstrip("/")
    RADARR_4K_API_KEY = os.getenv("RADARR_4K_API_KEY", "")
    SONARR_URL = os.getenv("SONARR_URL", "").rstrip("/")
    SONARR_API_KEY = os.getenv("SONARR_API_KEY", "")
    TAUTULLI_URL = os.getenv("TAUTULLI_URL", "").rstrip("/")
    TAUTULLI_API_KEY = os.getenv("TAUTULLI_API_KEY", "")
    PLEX_CLIENT_IDENTIFIER = os.getenv("PLEX_CLIENT_IDENTIFIER", "")  # Optional legacy override; generated automatically.
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

    @classmethod
    def validate(cls, testing=False):
        if testing:
            return
        missing = [name for name in ("SECRET_KEY", "WEBHOOK_SECRET") if not getattr(cls, name)]
        placeholders = [name for name in ("SECRET_KEY", "WEBHOOK_SECRET") if "REPLACE" in getattr(cls, name, "")]
        if missing or placeholders:
            raise RuntimeError("Required secrets are missing or still placeholders: " + ", ".join(missing + placeholders))
        Path(cls.SESSION_FILE_DIR).mkdir(parents=True, exist_ok=True)
