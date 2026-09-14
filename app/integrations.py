import requests


class IntegrationError(RuntimeError):
    pass


class BaseClient:
    def __init__(self, base_url, api_key, session=None, timeout=(5, 30)):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout

    def get(self, path, *, params=None, header_key="X-Api-Key"):
        params = dict(params or {})
        headers = {header_key: self.api_key} if header_key else {}
        try:
            response = self.session.get(self.base_url + path, params=params, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise IntegrationError(f"{self.__class__.__name__}: {exc}") from exc
        return data


class SeerrClient(BaseClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._media_cache = {}

    def paged(self, path, key="results"):
        skip, take, items = 0, 100, []
        while True:
            data = self.get(path, params={"take": take, "skip": skip})
            page = data.get(key, [])
            items.extend(page)
            if len(page) < take:
                return items
            skip += take

    def users(self): return self.paged("/api/v1/user")
    def requests(self): return self.paged("/api/v1/request")
    def media_details(self, media_type, tmdb_id):
        key = (media_type, int(tmdb_id))
        if key not in self._media_cache:
            if media_type not in {"movie", "tv"}:
                raise IntegrationError(f"Unsupported Seerr media type: {media_type}")
            self._media_cache[key] = self.get(f"/api/v1/{media_type}/{int(tmdb_id)}")
        return self._media_cache[key]

    def movie_details(self, tmdb_id): return self.media_details("movie", tmdb_id)
    def tv_details(self, tmdb_id): return self.media_details("tv", tmdb_id)


class RadarrClient(BaseClient):
    def movies(self): return self.get("/api/v3/movie")

    def size_for_tmdb(self, tmdb_id):
        for movie in self.movies():
            if int(movie.get("tmdbId") or 0) == int(tmdb_id or 0):
                return int((movie.get("movieFile") or {}).get("size") or movie.get("sizeOnDisk") or 0)
        return 0


class SonarrClient(BaseClient):
    def series(self): return self.get("/api/v3/series", params={"includeSeasonImages": "false"})

    def series_for(self, tvdb_id):
        return next((s for s in self.series() if int(s.get("tvdbId") or 0) == int(tvdb_id or 0)), None)

    def size_for_seasons(self, tvdb_id, seasons):
        series = self.series_for(tvdb_id)
        if not series:
            return 0
        wanted = {int(n) for n in seasons}
        stats = series.get("statistics", {}).get("seasonStatistics") or []
        if not stats:
            stats = [{"seasonNumber": s.get("seasonNumber"), "sizeOnDisk": (s.get("statistics") or {}).get("sizeOnDisk", 0)} for s in series.get("seasons", [])]
        return sum(int(s.get("sizeOnDisk") or 0) for s in stats if not wanted or int(s.get("seasonNumber") or 0) in wanted)


class TautulliClient(BaseClient):
    def command(self, command, **params):
        params.update({"apikey": self.api_key, "cmd": command})
        data = self.get("/api/v2", params=params, header_key=None)
        response = data.get("response") if isinstance(data, dict) else None
        if not response or response.get("result") != "success":
            raise IntegrationError(f"Tautulli returned an invalid response for {command}")
        return response.get("data")

    def users(self): return self.command("get_users") or []
    def activity(self): return (self.command("get_activity") or {}).get("sessions", [])
    def history(self, user_id=None, length=50):
        params = {"length": length}
        if user_id is not None: params["user_id"] = user_id
        return (self.command("get_history", **params) or {}).get("data", [])
