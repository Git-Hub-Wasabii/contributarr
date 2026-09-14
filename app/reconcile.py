import json
import threading
from datetime import datetime, timezone

from flask import current_app

from .db import get_db, integration_config, transaction, utcnow
from .integrations import RadarrClient, SeerrClient, SonarrClient, TautulliClient

sync_lock = threading.Lock()


def normalize(value):
    return " ".join((value or "").strip().casefold().split())


def clients():
    configured = {name: integration_config(name) for name in ("SEERR", "RADARR_1080", "RADARR_4K", "SONARR", "TAUTULLI")}
    result = {
        "seerr": SeerrClient(configured["SEERR"]["url"], configured["SEERR"]["api_key"]),
        "radarr_1080": RadarrClient(configured["RADARR_1080"]["url"], configured["RADARR_1080"]["api_key"]),
        "radarr_4k": RadarrClient(configured["RADARR_4K"]["url"], configured["RADARR_4K"]["api_key"]),
        "sonarr": SonarrClient(configured["SONARR"]["url"], configured["SONARR"]["api_key"]),
    }
    if configured["TAUTULLI"]["url"] and configured["TAUTULLI"]["api_key"]:
        result["tautulli"] = TautulliClient(configured["TAUTULLI"]["url"], configured["TAUTULLI"]["api_key"])
    return result


def import_seerr_user(external_id, username):
    db = get_db()
    row = db.execute("SELECT user_id FROM identity_mappings WHERE provider='seerr' AND external_id=?", (str(external_id),)).fetchone()
    if row:
        db.execute("UPDATE identity_mappings SET username=?,normalized_username=? WHERE provider='seerr' AND external_id=?", (username, normalize(username), str(external_id)))
        db.execute("UPDATE users SET display_name=?,updated_at=? WHERE id=?", (username, utcnow(), row["user_id"]))
        return row["user_id"]
    # Exact normalized matching only; ambiguity is deliberately left for manual mapping.
    matches = db.execute("SELECT DISTINCT user_id FROM identity_mappings WHERE normalized_username=?", (normalize(username),)).fetchall()
    user_id = matches[0]["user_id"] if len(matches) == 1 else None
    if user_id is None:
        now = utcnow()
        cur = db.execute("INSERT INTO users(display_name,created_at,updated_at) VALUES(?,?,?)", (username, now, now))
        user_id = cur.lastrowid
    db.execute("INSERT INTO identity_mappings(user_id,provider,external_id,username,normalized_username) VALUES(?,?,?,?,?)", (user_id, "seerr", str(external_id), username, normalize(username)))
    return user_id


def match_existing_identity(provider, external_id, username):
    """Attach only an unambiguous exact-normalized account; never fuzzy merge."""
    db = get_db()
    if db.execute("SELECT 1 FROM identity_mappings WHERE provider=? AND external_id=?", (provider, str(external_id))).fetchone(): return
    matches = db.execute("SELECT id FROM users WHERE lower(trim(display_name))=?", (normalize(username),)).fetchall()
    if len(matches) != 1: return
    if db.execute("SELECT 1 FROM identity_mappings WHERE user_id=? AND provider=?", (matches[0]["id"], provider)).fetchone(): return
    db.execute("INSERT INTO identity_mappings(user_id,provider,external_id,username,normalized_username) VALUES(?,?,?,?,?)", (matches[0]["id"], provider, str(external_id), username, normalize(username)))


def extract_request(item):
    media = item.get("media") or {}
    user = item.get("requestedBy") or item.get("requestedByUser") or {}
    seasons = [int(s.get("seasonNumber", s)) if isinstance(s, dict) else int(s) for s in item.get("seasons", [])]
    title = media.get("title") or media.get("name") or item.get("title") or ""
    return {
        "id": int(item["id"]), "seerr_user_id": str(user.get("id", "unknown")),
        "username": user.get("displayName") or user.get("username") or user.get("email") or "Unknown",
        "title": title, "media_type": media.get("mediaType") or item.get("type") or "unknown",
        "tmdb_id": media.get("tmdbId"), "tvdb_id": media.get("tvdbId"), "seasons": seasons,
        "status": str(item.get("status", media.get("status", "unknown"))),
        "requested_at": item.get("createdAt") or item.get("created_at") or item.get("createdDate"), "raw": item,
    }


def normalized_utc_timestamp(value, fallback):
    if not value: return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return fallback


def observed_charge(req, c):
    if req["media_type"] == "movie":
        size_1080 = c["radarr_1080"].size_for_tmdb(req["tmdb_id"])
        size_4k = c["radarr_4k"].size_for_tmdb(req["tmdb_id"])
        if size_1080: return size_1080, "Radarr 1080p"
        return size_4k, "Radarr 4K" if size_4k else None
    if req["media_type"] == "tv" and req["tvdb_id"]:
        return c["sonarr"].size_for_seasons(req["tvdb_id"], req["seasons"]), "Sonarr"
    return 0, None


def reconcile_one(item, c):
    req = extract_request(item)
    needs_details = not req["title"] or (req["media_type"] == "tv" and not req["tvdb_id"])
    if needs_details and req["tmdb_id"] and req["media_type"] in {"movie", "tv"}:
        details = c["seerr"].media_details(req["media_type"], req["tmdb_id"])
        req["title"] = req["title"] or details.get("title") or details.get("name") or details.get("originalTitle") or details.get("originalName") or ""
        if req["media_type"] == "tv" and not req["tvdb_id"]:
            req["tvdb_id"] = (details.get("externalIds") or {}).get("tvdbId") or details.get("tvdbId")
    req["title"] = req["title"] or f"Unknown {req['media_type'].title()} (TMDB {req['tmdb_id'] or 'unavailable'})"
    db = get_db()
    user_id = import_seerr_user(req["seerr_user_id"], req["username"])
    existing = db.execute("SELECT * FROM requests WHERE seerr_request_id=?", (req["id"],)).fetchone()
    observed, source = observed_charge(req, c)
    previous = int(existing["charged_bytes"]) if existing else 0
    charged = max(previous, observed)
    now = utcnow()
    requested_at = normalized_utc_timestamp(req["requested_at"], existing["requested_at"] if existing and existing["requested_at"] else existing["first_seen_at"] if existing else now)
    values = (user_id, req["seerr_user_id"], req["username"], req["title"], req["media_type"], req["tmdb_id"], req["tvdb_id"], json.dumps(req["seasons"]), source or (existing["servarr_source"] if existing else None), charged, now, req["status"], json.dumps(req["raw"], separators=(",", ":")), requested_at)
    if existing:
        db.execute("UPDATE requests SET user_id=?,seerr_user_id=?,display_username=?,title=?,media_type=?,tmdb_id=?,tvdb_id=?,requested_seasons=?,servarr_source=?,charged_bytes=?,last_updated_at=?,seerr_status=?,raw_metadata=?,requested_at=?,is_deleted=0,deleted_at=NULL,refunded_bytes=0 WHERE seerr_request_id=?", values + (req["id"],))
        request_id = existing["id"]
    else:
        cur = db.execute("INSERT INTO requests(user_id,seerr_user_id,display_username,title,media_type,tmdb_id,tvdb_id,requested_seasons,servarr_source,charged_bytes,last_updated_at,seerr_status,raw_metadata,requested_at,seerr_request_id,first_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values + (req["id"], now))
        request_id = cur.lastrowid
    if charged > previous:
        db.execute("INSERT INTO usage_ledger(request_id,user_id,delta_bytes,total_charged_bytes,source,created_at) VALUES(?,?,?,?,?,?)", (request_id, user_id, charged - previous, charged, source, now))


def mark_deleted_requests(seen_request_ids, integration_clients):
    """Classify missing Seerr requests only after checking their Servarr size."""
    db, now = get_db(), utcnow()
    seen = {int(value) for value in seen_request_ids}
    rows = db.execute("SELECT * FROM requests").fetchall()
    deleted = restored = 0
    warnings = []
    for row in rows:
        if row["seerr_request_id"] in seen:
            continue
        if (row["media_type"] == "movie" and not row["tmdb_id"]) or (row["media_type"] == "tv" and not row["tvdb_id"]):
            warnings.append(f"Could not verify deleted request {row['seerr_request_id']}: stable media ID is missing.")
            continue
        request_data = {
            "media_type": row["media_type"], "tmdb_id": row["tmdb_id"],
            "tvdb_id": row["tvdb_id"], "seasons": json.loads(row["requested_seasons"] or "[]"),
        }
        try:
            observed, source = observed_charge(request_data, integration_clients)
        except Exception as exc:
            warnings.append(f"Could not verify deleted request {row['seerr_request_id']}: {exc}")
            continue
        historical = max(int(row["charged_bytes"] or 0), int(row["refunded_bytes"] or 0))
        if observed > 0:
            restored_charge = max(historical, observed)
            if row["is_deleted"] or restored_charge != row["charged_bytes"] or row["seerr_status"] != "removed_media_present":
                db.execute(
                    "UPDATE requests SET charged_bytes=?,refunded_bytes=0,is_deleted=0,deleted_at=NULL,last_updated_at=?,servarr_source=COALESCE(?,servarr_source),seerr_status='removed_media_present' WHERE id=?",
                    (restored_charge, now, source, row["id"]),
                )
                if restored_charge > historical:
                    db.execute(
                        "INSERT INTO usage_ledger(request_id,user_id,delta_bytes,total_charged_bytes,source,created_at) VALUES(?,?,?,?,?,?)",
                        (row["id"], row["user_id"], restored_charge - historical, restored_charge, source, now),
                    )
                if row["is_deleted"]:
                    restored += 1
        elif not row["is_deleted"]:
            db.execute(
                "UPDATE requests SET refunded_bytes=charged_bytes,charged_bytes=0,is_deleted=1,deleted_at=?,last_updated_at=?,seerr_status='deleted' WHERE id=?",
                (now, now, row["id"]),
            )
            deleted += 1
    return {"deleted": deleted, "restored": restored, "warnings": warnings}


def run_reconciliation(trigger="manual"):
    if not sync_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "message": "A reconciliation is already running."}
    db = get_db()
    start = utcnow()
    run_id = db.execute("INSERT INTO sync_runs(started_at,message) VALUES(?,?)", (start, trigger)).lastrowid
    processed = errors = 0
    messages = []
    try:
        c = clients()
        # Import the complete Seerr directory, including people without requests.
        for account in c["seerr"].users():
            try:
                username = account.get("displayName") or account.get("username") or account.get("email") or f"Seerr user {account.get('id')}"
                with transaction(db): import_seerr_user(account["id"], username)
            except Exception as exc:
                errors += 1
                messages.append(f"User {account.get('id', '?')}: {exc}")
        # Identity discovery is best-effort; Tautulli can never block quota sync.
        if "tautulli" in c:
            try:
                for account in c["tautulli"].users():
                    username = account.get("friendly_name") or account.get("username") or account.get("user") or "Unknown"
                    match_existing_identity("tautulli", account.get("user_id") or account.get("id"), username)
            except Exception as exc:
                messages.append(f"Tautulli warning: {exc}")
        request_items = c["seerr"].requests()
        for item in request_items:
            try:
                with transaction(db): reconcile_one(item, c)
                processed += 1
            except Exception as exc:
                errors += 1
                messages.append(f"Request {item.get('id', '?')}: {exc}")
        with transaction(db):
            deletion_result = mark_deleted_requests((item["id"] for item in request_items), c)
        if deletion_result["deleted"]:
            messages.append(f"Refunded {deletion_result['deleted']} deleted Seerr request(s) with no remaining Servarr storage.")
        if deletion_result["restored"]:
            messages.append(f"Restored {deletion_result['restored']} request(s) whose media still exists in Servarr.")
        messages.extend(deletion_result["warnings"])
        success = errors == 0
        db.execute("UPDATE sync_runs SET completed_at=?,success=?,processed=?,errors=?,message=? WHERE id=?", (utcnow(), int(success), processed, errors, "; ".join(messages)[:2000] or trigger, run_id))
        return {"ok": success, "processed": processed, "errors": errors, "message": messages[0] if messages else "Reconciliation completed."}
    except Exception as exc:
        db.execute("UPDATE sync_runs SET completed_at=?,success=0,processed=?,errors=?,message=? WHERE id=?", (utcnow(), processed, errors + 1, str(exc)[:2000], run_id))
        return {"ok": False, "processed": processed, "errors": errors + 1, "message": str(exc)}
    finally:
        sync_lock.release()
