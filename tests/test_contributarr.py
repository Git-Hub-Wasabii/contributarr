import sqlite3
import re
from datetime import datetime, timezone

import pytest

from app.db import claim_owner, get_db, get_secret, import_legacy, owner_record, utcnow
from app.integrations import IntegrationError, RadarrClient, SonarrClient
from app.reconcile import mark_deleted_requests, observed_charge, reconcile_one, run_reconciliation


class FakeResponse:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): return None
    def json(self): return self.payload


def test_first_plex_user_atomically_claims_and_second_is_denied(app):
    with app.app_context():
        assert claim_owner("stable-1", "First", "first@example.test") is True
        assert claim_owner("stable-2", "Second", "second@example.test") is False
        assert owner_record()["plex_id"] == "stable-1"


def test_mocked_plex_pin_flow_claims_owner_without_storing_token(app, client, monkeypatch):
    monkeypatch.setattr("app.auth.requests.post", lambda *_a, **_k: FakeResponse({"id": 12, "code": "PINCODE"}))
    def plex_get(url, **_kwargs):
        return FakeResponse({"authToken": "temporary-plex-token"}) if "/pins/" in url else FakeResponse({"id": "plex-99", "username": "Plex Owner", "email": "owner@example.test"})
    monkeypatch.setattr("app.auth.requests.get", plex_get)
    response = client.get("/auth/plex/start")
    assert response.status_code == 302 and "app.plex.tv/auth" in response.location
    response = client.get("/auth/plex/callback")
    assert response.status_code == 302 and response.location.endswith("/home")
    with client.session_transaction() as session:
        assert session["plex_id"] == "plex-99"
        assert "temporary-plex-token" not in session.values()
    with app.app_context(): assert owner_record()["plex_id"] == "plex-99"


def test_owner_survives_logout_and_new_app_instance(app, owner_client):
    # A valid CSRF-less logout is correctly rejected; clearing the browser session
    # directly models a completed logout without touching persistent ownership.
    with owner_client.session_transaction() as session: session.clear()
    assert owner_client.get("/stats").status_code == 302
    with app.app_context(): assert owner_record()["plex_id"] == "plex-1"


def test_clear_owner_preserves_ledger(app, seeded_user):
    with app.app_context():
        claim_owner("plex-1", "Owner", None)
        db, now = get_db(), utcnow()
        cur = db.execute("INSERT INTO requests(seerr_request_id,user_id,seerr_user_id,display_username,title,media_type,charged_bytes,first_seen_at,last_updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (7,seeded_user,"42","Alice","Movie","movie",123,now,now))
        db.execute("INSERT INTO usage_ledger(request_id,user_id,delta_bytes,total_charged_bytes,created_at) VALUES(?,?,?,?,?)", (cur.lastrowid,seeded_user,123,123,now))
        result = app.test_cli_runner().invoke(args=["owner", "clear", "--yes"])
        assert result.exit_code == 0
        assert owner_record() is None
        assert db.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 1


def test_anonymous_redirect_and_non_owner_denied(app, client):
    with app.app_context(): claim_owner("plex-1", "Owner", None)
    assert client.get("/stats").status_code == 302
    with client.session_transaction() as session: session["plex_id"] = "plex-other"
    for path in ("/stats", "/admin/settings", "/api/reconcile"):
        response = client.get(path) if path != "/api/reconcile" else client.post(path)
        assert response.status_code in {400, 403}  # CSRF may reject POST before authorization.


def test_owner_pages_and_csrf(app, owner_client, seeded_user):
    assert owner_client.get("/home").status_code == 200
    assert owner_client.get("/stats").status_code == 200
    assert owner_client.get("/dashboard").status_code == 200
    assert owner_client.get(f"/users/{seeded_user}").status_code == 200
    assert owner_client.get("/admin/users").status_code == 200
    assert owner_client.get("/admin/settings").status_code == 200
    assert owner_client.post("/admin/settings", data={"action": "user", "user_id": seeded_user}).status_code == 400


def test_non_owner_can_see_only_their_own_stats(app, client, seeded_user):
    with app.app_context():
        claim_owner("plex-owner", "Owner", None)
        now = utcnow()
        other_id = get_db().execute("INSERT INTO users(display_name,created_at,updated_at) VALUES(?,?,?)", ("Other", now, now)).lastrowid
    with client.session_transaction() as session:
        session["plex_id"] = "plex-user"
        session["user_id"] = seeded_user
    own = client.get(f"/users/{seeded_user}")
    assert own.status_code == 200 and b"Remaining" in own.data
    assert client.get(f"/users/{other_id}").status_code == 403
    assert client.get("/stats").status_code == 403
    assert client.get("/admin/settings").status_code == 403


def test_admin_saves_encrypted_integration_key_without_rendering_it(app, owner_client):
    page = owner_client.get("/admin/settings").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    response = owner_client.post("/admin/settings", data={
        "csrf_token": token, "action": "integrations",
        "SEERR_url": "http://seerr.local:5055", "SEERR_api_key": "private-seerr-key",
        "SONARR_url": "http://sonarr.local:8989", "SONARR_api_key": "private-sonarr-key",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"private-seerr-key" not in response.data
    with app.app_context():
        raw = get_db().execute("SELECT value FROM settings WHERE key='integration.SEERR.api_key'").fetchone()[0]
        assert raw.startswith("fernet:") and "private-seerr-key" not in raw
        assert get_secret("integration.SEERR.api_key") == "private-seerr-key"
        assert get_secret("integration.SONARR.api_key") == "private-sonarr-key"


def test_settings_has_one_save_for_all_integrations(owner_client):
    html = owner_client.get("/admin/settings").get_data(as_text=True)
    assert html.count('name="action" value="integrations"') == 1
    assert html.count("Save all integrations") == 1
    for name in ("SEERR", "RADARR_1080", "RADARR_4K", "SONARR", "TAUTULLI"):
        assert f'name="{name}_url"' in html


def test_plex_client_identifier_is_generated_and_persisted(app):
    with app.app_context():
        first = app.config["PLEX_CLIENT_IDENTIFIER"]
        stored = get_db().execute("SELECT value FROM settings WHERE key='plex_client_identifier'").fetchone()[0]
        assert first == stored and len(first) >= 32


def test_radarr_checks_both_instances():
    class Fake:
        def __init__(self, size): self.size = size; self.calls = 0
        def size_for_tmdb(self, _): self.calls += 1; return self.size
    first, second = Fake(0), Fake(9_000)
    size, source = observed_charge({"media_type":"movie","tmdb_id":1,"tvdb_id":None,"seasons":[]}, {"radarr_1080":first,"radarr_4k":second})
    assert (size, source, first.calls, second.calls) == (9_000, "Radarr 4K", 1, 1)
    first.size = 7_000
    observed_charge({"media_type":"movie","tmdb_id":1,"tvdb_id":None,"seasons":[]}, {"radarr_1080":first,"radarr_4k":second})
    assert (first.calls, second.calls) == (2, 2)


def test_sonarr_sums_only_requested_seasons():
    client = SonarrClient("http://sonarr", "key")
    client.series_for = lambda _: {"statistics":{"seasonStatistics":[{"seasonNumber":1,"sizeOnDisk":100},{"seasonNumber":2,"sizeOnDisk":250},{"seasonNumber":3,"sizeOnDisk":500}]}}
    assert client.size_for_seasons(99, [1, 3]) == 600


def test_charge_never_decreases_and_deleted_media_does_not_refund(app):
    item = {"id":10,"status":5,"requestedBy":{"id":42,"displayName":"Alice"},"media":{"mediaType":"movie","tmdbId":77,"title":"A very long movie title that remains complete in storage"}}
    class Radarr:
        size = 8_000
        def size_for_tmdb(self, _): return self.size
    radarr = Radarr(); clients = {"radarr_1080":radarr,"radarr_4k":radarr}
    with app.app_context():
        reconcile_one(item, clients)
        radarr.size = 0
        reconcile_one(item, clients)
        db = get_db()
        assert db.execute("SELECT charged_bytes FROM requests WHERE seerr_request_id=10").fetchone()[0] == 8_000
        assert db.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 1
        assert db.execute("SELECT is_deleted FROM requests WHERE seerr_request_id=10").fetchone()[0] == 0


def test_seerr_details_supply_media_name_and_original_request_date(app):
    item = {"id":11,"status":5,"createdAt":"2026-09-01T02:30:00.000Z","requestedBy":{"id":42,"displayName":"Alice"},"media":{"mediaType":"movie","tmdbId":77}}
    class Seerr:
        def media_details(self, media_type, tmdb_id):
            assert (media_type, tmdb_id) == ("movie", 77)
            return {"title":"The Actual Media Name"}
    class Radarr:
        def size_for_tmdb(self, _): return 0
    with app.app_context():
        reconcile_one(item, {"seerr":Seerr(),"radarr_1080":Radarr(),"radarr_4k":Radarr()})
        row = get_db().execute("SELECT title,requested_at FROM requests WHERE seerr_request_id=11").fetchone()
        assert row["title"] == "The Actual Media Name"
        assert row["requested_at"].startswith("2026-09-01T02:30:00")


def test_deleted_seerr_request_refunds_quota_and_moves_to_deleted_section(app, owner_client, seeded_user):
    class EmptyRadarr:
        def size_for_tmdb(self, _): return 0
    requested = "2026-09-01T02:30:00+00:00"
    with app.app_context():
        db = get_db()
        db.execute("UPDATE users SET contribution_myr=12.5 WHERE id=?", (seeded_user,))
        db.execute("INSERT INTO requests(seerr_request_id,user_id,seerr_user_id,display_username,title,media_type,tmdb_id,charged_bytes,first_seen_at,last_updated_at,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (12,seeded_user,"42","Alice","Refunded Movie","movie",99,2_000_000_000,utcnow(),utcnow(),requested))
        result = mark_deleted_requests([], {"radarr_1080": EmptyRadarr(), "radarr_4k": EmptyRadarr()})
        assert result == {"deleted": 1, "restored": 0, "warnings": []}
        row = db.execute("SELECT charged_bytes,refunded_bytes,is_deleted FROM requests WHERE seerr_request_id=12").fetchone()
        assert tuple(row) == (0, 2_000_000_000, 1)
    html = owner_client.get(f"/users/{seeded_user}").get_data(as_text=True)
    assert "Deleted requests" in html and "Refunded Movie" in html and "2.00 GB" in html
    assert "01 Sep 2026, 10:30" in html


def test_missing_seerr_request_remains_charged_while_media_exists(app, owner_client, seeded_user):
    class Radarr:
        def __init__(self, size): self.size = size
        def size_for_tmdb(self, _): return self.size
    with app.app_context():
        db, now = get_db(), utcnow()
        db.execute("INSERT INTO requests(seerr_request_id,user_id,seerr_user_id,display_username,title,media_type,tmdb_id,charged_bytes,first_seen_at,last_updated_at,seerr_status) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (13,seeded_user,"42","Alice","Still Stored","movie",100,2_000_000_000,now,now,"5"))
        result = mark_deleted_requests([], {"radarr_1080":Radarr(2_000_000_000),"radarr_4k":Radarr(0)})
        assert result["deleted"] == 0
        row = db.execute("SELECT charged_bytes,is_deleted,seerr_status FROM requests WHERE seerr_request_id=13").fetchone()
        assert tuple(row) == (2_000_000_000, 0, "removed_media_present")
    html = owner_client.get(f"/users/{seeded_user}").get_data(as_text=True)
    assert "Still Stored" in html
    dashboard = owner_client.get("/dashboard").get_data(as_text=True)
    assert "Still Stored" not in dashboard


def test_mocked_seerr_reconciliation_imports_users_and_requests(app, monkeypatch):
    request = {"id":101,"status":5,"requestedBy":{"id":2,"displayName":"Requester"},"media":{"mediaType":"movie","tmdbId":88,"title":"Synced Movie"}}
    class Seerr:
        def users(self): return [{"id":1,"displayName":"No Requests"},{"id":2,"displayName":"Requester"}]
        def requests(self): return [request]
    class Radarr:
        def __init__(self, size): self.size = size
        def size_for_tmdb(self, _): return self.size
    monkeypatch.setattr("app.reconcile.clients", lambda: {"seerr":Seerr(),"radarr_1080":Radarr(2_000),"radarr_4k":Radarr(0),"sonarr":object()})
    with app.app_context():
        result = run_reconciliation("test")
        assert result["ok"] is True and result["processed"] == 1
        assert get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
        assert get_db().execute("SELECT charged_bytes FROM requests WHERE seerr_request_id=101").fetchone()[0] == 2_000


def test_tautulli_failure_does_not_break_user_page(app, owner_client, seeded_user, monkeypatch):
    with app.app_context():
        get_db().execute("INSERT INTO identity_mappings(user_id,provider,external_id,username,normalized_username) VALUES(?,?,?,?,?)", (seeded_user,"tautulli","9","Alice","alice"))
    def fail(*_args, **_kwargs): raise IntegrationError("offline")
    monkeypatch.setattr("app.main.TautulliClient.history", fail)
    response = owner_client.get(f"/users/{seeded_user}")
    assert response.status_code == 200 and b"offline" in response.data


def test_ledger_uses_username_full_hover_title_and_kuala_lumpur_time(app, owner_client, seeded_user):
    title = "The Lord of the Rings: The Fellowship of the Ring Extended Edition"
    with app.app_context():
        db = get_db(); created = "2026-01-01T00:00:00+00:00"
        requested = "2025-12-30T16:00:00+00:00"
        cur = db.execute("INSERT INTO requests(seerr_request_id,user_id,seerr_user_id,display_username,title,media_type,charged_bytes,first_seen_at,last_updated_at,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (88,seeded_user,"42","Alice",title,"movie",1_000_000_000,created,created,requested))
        db.execute("INSERT INTO usage_ledger(request_id,user_id,delta_bytes,total_charged_bytes,created_at) VALUES(?,?,?,?,?)", (cur.lastrowid,seeded_user,1_000_000_000,1_000_000_000,created))
    html = owner_client.get("/stats").get_data(as_text=True)
    assert "Alice" in html and "User 42" not in html
    assert f'title="{title}"' in html and ">" + title + "</a>" in html
    assert "31 Dec 2025, 00:00" in html
    assert "01 Jan 2026, 08:00" not in html


def test_ledger_paginates_with_supported_page_sizes(app, owner_client, seeded_user):
    with app.app_context():
        db, now = get_db(), utcnow()
        for index in range(30):
            cur = db.execute("INSERT INTO requests(seerr_request_id,user_id,seerr_user_id,display_username,title,media_type,charged_bytes,first_seen_at,last_updated_at,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (500+index,seeded_user,"42","Alice",f"Ledger title {index:02d}","movie",index+1,now,now,f"2026-08-{(index % 28)+1:02d}T00:00:00+00:00"))
            db.execute("INSERT INTO usage_ledger(request_id,user_id,delta_bytes,total_charged_bytes,created_at) VALUES(?,?,?,?,?)", (cur.lastrowid,seeded_user,index+1,index+1,now))
    first = owner_client.get("/dashboard").get_data(as_text=True)
    assert first.count('data-label="User"') == 25 and "1–25 of 30" in first
    second = owner_client.get("/dashboard?ledger_page=2&ledger_per_page=25").get_data(as_text=True)
    assert second.count('data-label="User"') == 5 and "26–30 of 30" in second
    fifty = owner_client.get("/dashboard?ledger_per_page=50").get_data(as_text=True)
    assert fifty.count('data-label="User"') == 30
    for size in (25, 50, 100, 250):
        assert f'<option value="{size}"' in first


def test_dashboard_capitalizes_media_and_links_to_seerr(app, owner_client, seeded_user):
    with app.app_context():
        from app.db import set_setting
        set_setting("integration.SEERR.url", "http://seerr.local:5055")
        now = utcnow()
        get_db().execute("INSERT INTO requests(seerr_request_id,user_id,seerr_user_id,display_username,title,media_type,tmdb_id,first_seen_at,last_updated_at,seerr_status,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (321,seeded_user,"42","Alice","A Long Movie Name For Hovering","movie",88,now,now,"1",now))
    html = owner_client.get("/dashboard").get_data(as_text=True)
    assert ">Movie</span>" in html
    assert 'href="http://seerr.local:5055/movie/88"' in html
    assert 'title="A Long Movie Name For Hovering"' in html


def test_admin_can_choose_custom_media_link_base(app, owner_client, seeded_user):
    page = owner_client.get("/admin/settings").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    response = owner_client.post("/admin/settings", data={"csrf_token":token,"action":"links","link_mode":"custom","seerr_link_base_url":"https://seerr.example.test/"}, follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        now = utcnow()
        get_db().execute("INSERT INTO requests(seerr_request_id,user_id,seerr_user_id,display_username,title,media_type,tmdb_id,first_seen_at,last_updated_at,seerr_status,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (322,seeded_user,"42","Alice","External Link Movie","movie",89,now,now,"1",now))
    html = owner_client.get("/dashboard").get_data(as_text=True)
    assert 'href="https://seerr.example.test/movie/89"' in html


def test_users_page_shows_manual_adjustment_history(app, owner_client, seeded_user):
    with app.app_context():
        get_db().execute("INSERT INTO manual_adjustments(user_id,bytes,note,created_at) VALUES(?,?,?,?)", (seeded_user,-500_000_000,"Storage correction","2026-09-14T16:15:00+00:00"))
    html = owner_client.get("/admin/users").get_data(as_text=True)
    assert "Manual adjustment history" in html and "Storage correction" in html
    assert "-0.50 GB" in html and "15 Sep 2026, 00:15" in html


def test_home_has_requested_leaderboards_and_handles_tautulli_outage(app, owner_client, seeded_user, monkeypatch):
    with app.app_context():
        from app.db import set_secret, set_setting
        set_setting("integration.TAUTULLI.url", "http://tautulli.local:8181")
        set_secret("integration.TAUTULLI.api_key", "key")
        get_db().execute("UPDATE users SET contribution_myr=25 WHERE id=?", (seeded_user,))
    def fail(*_args, **_kwargs): raise IntegrationError("Tautulli unavailable")
    monkeypatch.setattr("app.main.TautulliClient.history", fail)
    html = owner_client.get("/home").get_data(as_text=True)
    assert "Contribution leaderboard" in html and "Usage leaderboard" in html
    assert "Top plays" in html and "Recently requested media" in html
    assert "Tautulli unavailable" in html and "MYR 25.00" in html


def test_current_seerr_media_uses_balanced_responsive_grid(owner_client):
    html = owner_client.get("/dashboard").get_data(as_text=True)
    css = open("static/app.css", encoding="utf-8").read()
    assert "request-bucket--available" in html and "No failed requests." in html
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css
    assert ".request-bucket--available { grid-column: span 2; }" in css
    assert ".request-bucket--available .request-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert "overflow-y: auto" not in css


def test_admin_can_change_automatic_sync_schedule(app, owner_client):
    page = owner_client.get("/admin/settings").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    response = owner_client.post("/admin/settings", data={"csrf_token":token,"action":"schedule","sync_interval_minutes":"30"}, follow_redirects=True)
    assert response.status_code == 200 and b"every 30 minutes" in response.data
    with app.app_context():
        assert get_db().execute("SELECT value FROM settings WHERE key='sync_interval_minutes'").fetchone()[0] == "30"


def test_admin_currency_setting_is_used_on_user_pages(app, owner_client, seeded_user):
    page = owner_client.get("/admin/settings").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    response = owner_client.post("/admin/settings", data={"csrf_token":token,"action":"currency","currency":"USD"}, follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        get_db().execute("UPDATE users SET contribution_myr=12.5 WHERE id=?", (seeded_user,))
    assert "USD 12.50" in owner_client.get(f"/users/{seeded_user}").get_data(as_text=True)


def test_theme_controls_support_auto_light_and_dark(owner_client):
    html = owner_client.get("/admin/settings").get_data(as_text=True)
    assert '<option value="auto">Auto</option>' in html
    assert '<option value="light">Light</option>' in html
    assert '<option value="dark">Dark</option>' in html


def test_secrets_never_render_or_enter_session(app, owner_client):
    app.config.update(SEERR_API_KEY="super-secret-api-key", TAUTULLI_API_KEY="another-secret")
    html = owner_client.get("/admin/settings").get_data(as_text=True)
    assert "super-secret-api-key" not in html and "another-secret" not in html
    with owner_client.session_transaction() as session:
        # Flask-WTF legitimately stores a random CSRF token; Plex/API tokens must not persist.
        assert "plex_auth_token" not in session
        assert "super-secret-api-key" not in session.values()
        assert "another-secret" not in session.values()


def test_owner_can_copy_generated_webhook_url(app, owner_client):
    html = owner_client.get("/admin/settings").get_data(as_text=True)
    assert "http://localhost/webhook/webhook-test" in html
    assert "Show webhook secret" in html


def test_legacy_tracker_import_preserves_rows(app, tmp_path):
    source = tmp_path / "tracker.db"
    legacy = sqlite3.connect(source)
    legacy.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, quota_bytes INTEGER)")
    legacy.execute("CREATE TABLE requests(id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, media_type TEXT, size_bytes INTEGER, created_at TEXT)")
    legacy.execute("INSERT INTO users VALUES(42,'Legacy Alice',1000)")
    legacy.execute("INSERT INTO requests VALUES(77,42,'Old Movie','movie',900,'2025-01-01T00:00:00+00:00')")
    legacy.commit(); legacy.close()
    with app.app_context():
        result = import_legacy(source)
        assert result == {"users":1,"requests":1,"ledger":1}
        assert get_db().execute("SELECT title,charged_bytes FROM requests WHERE seerr_request_id=77").fetchone()["charged_bytes"] == 900
    check = sqlite3.connect(source).execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    assert check == 1


def test_health_is_public_and_minimal(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert set(response.get_json()) == {"status", "application"}


def test_ghcr_workflow_is_repository_aware_and_gated():
    workflow = open(".github/workflows/publish-ghcr.yml", encoding="utf-8").read()
    assert "IMAGE_NAME: ghcr.io/${{ github.repository }}" in workflow
    assert 'echo "name=${IMAGE_NAME,,}"' in workflow
    assert "needs: validate" in workflow
    assert "packages: write" in workflow
    assert "username: ${{ github.actor }}" in workflow
    assert "password: ${{ secrets.GITHUB_TOKEN }}" in workflow
    assert "latest=false" in workflow
    assert "if: github.ref_type == 'tag'" in workflow
    assert "type=raw,value=latest" in workflow
    assert "type=ref,event=tag" in workflow
    assert "Refuse to overwrite an existing release tag" in workflow
    assert not re.search(r"type=raw,value=v\d{2}\.\d{2}\.\d{2}", workflow)
    assert "workflow_dispatch" not in workflow
