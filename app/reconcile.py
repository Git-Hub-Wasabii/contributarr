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
    assets = observed_assets(req, c)
    return asset_total(assets), source_summary(assets)


def observed_assets(req, c):
    """Return stable logical assets with exact observed bytes and optional physical identity."""
    if req["media_type"] == "movie" and req.get("tmdb_id"):
        assets = []
        for client_key, source in (("radarr_1080", "Radarr 1080p"), ("radarr_4k", "Radarr 4K")):
            client = c[client_key]
            if hasattr(client, "observation_for_tmdb"):
                observation = client.observation_for_tmdb(req["tmdb_id"])
            else:
                observation = {"bytes": client.size_for_tmdb(req["tmdb_id"]), "physical_id": None}
            assets.append({
                "key": f"movie:tmdb:{int(req['tmdb_id'])}:{client_key}",
                "bytes": int(observation.get("bytes") or 0),
                "source": source,
                "physical_id": observation.get("physical_id"),
            })
        return assets
    if req["media_type"] == "tv" and req.get("tvdb_id"):
        client = c["sonarr"]
        if hasattr(client, "season_observations"):
            observations = client.season_observations(req["tvdb_id"], req["seasons"])
            if observations:
                return [{
                    "key": f"tv:tvdb:{int(req['tvdb_id'])}:season:{int(item['season'])}",
                    "bytes": int(item.get("bytes") or 0),
                    "source": f"Sonarr season {int(item['season'])}",
                    "physical_id": None,
                } for item in observations]
        size = int(client.size_for_seasons(req["tvdb_id"], req["seasons"]) or 0)
        season_key = ",".join(str(int(value)) for value in sorted(set(req["seasons"]))) or "all"
        return [{
            "key": f"tv:tvdb:{int(req['tvdb_id'])}:seasons:{season_key}",
            "bytes": size, "source": f"Sonarr seasons {season_key}", "physical_id": None,
        }]
    return []


def asset_total(assets):
    """Sum logical assets, collapsing assets that identify the same physical file."""
    groups = {}
    for asset in assets:
        group = asset.get("physical_id") or asset["key"]
        groups[group] = max(groups.get(group, 0), int(asset.get("bytes") or 0))
    return sum(groups.values())


def source_summary(assets):
    sources = list(dict.fromkeys(asset["source"] for asset in assets if int(asset.get("bytes") or 0) > 0))
    seasons = [source.removeprefix("Sonarr season ") for source in sources if source.startswith("Sonarr season ")]
    if sources and len(seasons) == len(sources):
        return "Sonarr seasons " + ", ".join(seasons)
    return " + ".join(sources) or None


def decode_asset_map(value):
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def mapped_assets(asset_map):
    return [{"key": key, **value} for key, value in asset_map.items()]


def highwater_map():
    result = {}
    for row in get_db().execute("SELECT accounting_assets FROM requests WHERE is_deleted=0"):
        for key, asset in decode_asset_map(row["accounting_assets"]).items():
            if int(asset.get("bytes") or 0) > int(result.get(key, {}).get("bytes") or 0):
                result[key] = dict(asset)
    return result


def reserved_asset_claims():
    claims = {}
    rows = get_db().execute(
        "SELECT seerr_request_id,accounting_assets FROM requests "
        "WHERE is_deleted=0 AND seerr_status='removed_media_present' ORDER BY COALESCE(requested_at,first_seen_at),id"
    ).fetchall()
    for row in rows:
        for key in decode_asset_map(row["accounting_assets"]):
            claims.setdefault(key, row["seerr_request_id"])
    return claims


def reconcile_one(item, c, claimed_assets=None, global_highwaters=None):
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
    observations = observed_assets(req, c)
    observed = asset_total(observations)
    previous = int(existing["charged_bytes"]) if existing else 0
    previous_assets = decode_asset_map(existing["accounting_assets"]) if existing else {}
    claimed_assets = claimed_assets if claimed_assets is not None else {}
    global_highwaters = global_highwaters if global_highwaters is not None else {}
    assigned, duplicate_owners = {}, set()
    for observation in observations:
        key = observation["key"]
        owner = claimed_assets.get(key)
        if owner is not None and owner != req["id"]:
            duplicate_owners.add(owner)
            continue
        claimed_assets[key] = req["id"]
        prior = previous_assets.get(key, {})
        transferable = global_highwaters.get(key, {}) if observation["bytes"] > 0 else {}
        highwater = max(int(observation["bytes"]), int(prior.get("bytes") or 0), int(transferable.get("bytes") or 0))
        assigned[key] = {
            "bytes": highwater, "source": observation["source"],
            "physical_id": observation.get("physical_id") or prior.get("physical_id") or transferable.get("physical_id"),
        }
    charged = asset_total(mapped_assets(assigned))
    if existing and not previous_assets and previous > charged and assigned:
        first_key = next(iter(assigned))
        assigned[first_key]["bytes"] += previous - charged
        charged = asset_total(mapped_assets(assigned))
    assigned_current = [{**asset, "bytes": next((item["bytes"] for item in observations if item["key"] == key), 0), "key": key} for key, asset in assigned.items()]
    deduplicated = max(0, observed - asset_total(assigned_current))
    source = source_summary(mapped_assets(assigned))
    if duplicate_owners:
        duplicate_note = "Duplicate of Seerr request " + ", ".join(f"#{value}" for value in sorted(duplicate_owners))
        source = f"{source}; {duplicate_note}" if source else duplicate_note
    for key, asset in assigned.items():
        if int(asset["bytes"]) > int(global_highwaters.get(key, {}).get("bytes") or 0):
            global_highwaters[key] = dict(asset)
    now = utcnow()
    requested_at = normalized_utc_timestamp(req["requested_at"], existing["requested_at"] if existing and existing["requested_at"] else existing["first_seen_at"] if existing else now)
    values = (user_id, req["seerr_user_id"], req["username"], req["title"], req["media_type"], req["tmdb_id"], req["tvdb_id"], json.dumps(req["seasons"]), source or (existing["servarr_source"] if existing else None), charged, now, req["status"], json.dumps(req["raw"], separators=(",", ":")), requested_at, json.dumps(assigned, separators=(",", ":")), observed, deduplicated)
    if existing:
        db.execute("UPDATE requests SET user_id=?,seerr_user_id=?,display_username=?,title=?,media_type=?,tmdb_id=?,tvdb_id=?,requested_seasons=?,servarr_source=?,charged_bytes=?,last_updated_at=?,seerr_status=?,raw_metadata=?,requested_at=?,accounting_assets=?,observed_bytes=?,deduplicated_bytes=?,is_deleted=0,deleted_at=NULL,refunded_bytes=0 WHERE seerr_request_id=?", values + (req["id"],))
        request_id = existing["id"]
    else:
        cur = db.execute("INSERT INTO requests(user_id,seerr_user_id,display_username,title,media_type,tmdb_id,tvdb_id,requested_seasons,servarr_source,charged_bytes,last_updated_at,seerr_status,raw_metadata,requested_at,accounting_assets,observed_bytes,deduplicated_bytes,seerr_request_id,first_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values + (req["id"], now))
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
        if row["is_deleted"]:
            continue
        if (row["media_type"] == "movie" and not row["tmdb_id"]) or (row["media_type"] == "tv" and not row["tvdb_id"]):
            warnings.append(f"Could not verify deleted request {row['seerr_request_id']}: stable media ID is missing.")
            continue
        request_data = {
            "media_type": row["media_type"], "tmdb_id": row["tmdb_id"],
            "tvdb_id": row["tvdb_id"], "seasons": json.loads(row["requested_seasons"] or "[]"),
        }
        try:
            observations = observed_assets(request_data, integration_clients)
            observed, source = asset_total(observations), source_summary(observations)
        except Exception as exc:
            warnings.append(f"Could not verify deleted request {row['seerr_request_id']}: {exc}")
            continue
        historical = int(row["charged_bytes"] or 0)
        if observed > 0:
            asset_map = decode_asset_map(row["accounting_assets"])
            if asset_map:
                current = {item["key"]: item for item in observations}
                for key, asset in asset_map.items():
                    if key in current:
                        asset["bytes"] = max(int(asset.get("bytes") or 0), int(current[key]["bytes"]))
                        asset["physical_id"] = current[key].get("physical_id") or asset.get("physical_id")
            elif not int(row["deduplicated_bytes"] or 0):
                asset_map = {item["key"]: {"bytes": item["bytes"], "source": item["source"], "physical_id": item.get("physical_id")} for item in observations}
            retained = asset_total(mapped_assets(asset_map))
            if asset_map and historical > retained:
                first_key = next(iter(asset_map))
                asset_map[first_key]["bytes"] += historical - retained
                retained = asset_total(mapped_assets(asset_map))
            charged = max(historical, retained)
            db.execute(
                "UPDATE requests SET last_updated_at=?,servarr_source=COALESCE(?,servarr_source),seerr_status='removed_media_present',observed_bytes=?,charged_bytes=?,accounting_assets=? WHERE id=?",
                (now, source, observed, charged, json.dumps(asset_map, separators=(",", ":")), row["id"]),
            )
            if charged > historical:
                db.execute(
                    "INSERT INTO usage_ledger(request_id,user_id,delta_bytes,total_charged_bytes,source,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], row["user_id"], charged - historical, charged, source, now),
                )
        else:
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
        with transaction(db):
            deletion_result = mark_deleted_requests((item["id"] for item in request_items), c)
        claimed_assets = reserved_asset_claims()
        global_highwaters = highwater_map()
        ordered_items = sorted(
            request_items,
            key=lambda item: (str(item.get("createdAt") or item.get("created_at") or item.get("createdDate") or ""), int(item["id"])),
        )
        for item in ordered_items:
            try:
                with transaction(db): reconcile_one(item, c, claimed_assets, global_highwaters)
                processed += 1
            except Exception as exc:
                errors += 1
                messages.append(f"Request {item.get('id', '?')}: {exc}")
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
