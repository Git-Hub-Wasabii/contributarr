import json
import math
from datetime import datetime
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from .auth import current_owner, login_required, owner_required
from .db import get_db, get_setting, integration_config, owner_record, set_secret, set_setting, transaction, utcnow
from .integrations import IntegrationError, TautulliClient
from .reconcile import normalize, run_reconciliation

bp = Blueprint("main", __name__)
INTEGRATIONS = ("SEERR", "RADARR_1080", "RADARR_4K", "SONARR", "TAUTULLI")
CURRENCIES = ("MYR", "USD", "SGD", "EUR", "GBP", "AUD", "CAD", "JPY", "CNY", "INR")
PAGE_SIZES = (10, 25, 50, 100, 250)


def page_values(page_name, per_page_name, default=10):
    try:
        per_page = int(request.args.get(per_page_name, default))
    except (TypeError, ValueError):
        per_page = default
    if per_page not in PAGE_SIZES:
        per_page = default
    try:
        page = max(1, int(request.args.get(page_name, 1)))
    except (TypeError, ValueError):
        page = 1
    return page, per_page


def bytes_gb(value):
    return f"{int(value or 0) / 1_000_000_000:,.2f} GB"


def local_time(value):
    if not value:
        return "—"
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(current_app.config["TZ"])).strftime("%d %b %Y, %H:%M")


def media_label(value):
    return "TV" if (value or "").lower() == "tv" else (value or "Unknown").capitalize()


def currency_code():
    value = get_setting("currency", "MYR").upper()
    return value if value in CURRENCIES else "MYR"


def money(value):
    return f"{currency_code()} {float(value or 0):,.2f}"


def media_link_base():
    custom = get_setting("links.seerr_base_url", "").rstrip("/")
    if get_setting("links.mode", "internal") == "custom" and custom:
        return custom
    return integration_config("SEERR")["url"]


def request_buckets(rows):
    definitions = [
        ("pending", "Pending approval", {"1", "pending"}),
        ("processing", "Approved or processing", {"2", "approved", "processing"}),
        ("partial", "Partially available", {"3", "partial", "partially available"}),
        ("available", "Available", {"4", "5", "available"}),
        ("failed", "Failed", {"failed", "declined"}),
    ]
    buckets = [{"key": key, "label": label, "items": []} for key, label, _ in definitions]
    for row in rows:
        status = (row["seerr_status"] or "").lower()
        target = next((index for index, (_, _, statuses) in enumerate(definitions) if status in statuses), 1)
        buckets[target]["items"].append(row)
    return buckets


@bp.get("/")
@login_required
def index():
    return redirect(url_for("main.home"))


@bp.get("/home")
@login_required
def home():
    db = get_db()
    seerr_base = media_link_base()
    leaders = db.execute("SELECT * FROM users WHERE enabled=1 ORDER BY display_name COLLATE NOCASE").fetchall()
    contribution_leaders = sorted(leaders, key=lambda row: (-float(row["contribution_myr"]), row["display_name"].casefold()))
    recent_requests = db.execute(
        "SELECT r.*,u.display_name FROM requests r LEFT JOIN users u ON u.id=r.user_id "
        "WHERE r.is_deleted=0 AND (r.seerr_status IS NULL OR r.seerr_status!='removed_media_present') "
        "ORDER BY COALESCE(r.requested_at,r.first_seen_at) DESC LIMIT 9"
    ).fetchall()
    most_active_users, top_plays, tautulli_warning = [], [], None
    try:
        configured = integration_config("TAUTULLI")
        if configured["url"] and configured["api_key"]:
            history = TautulliClient(configured["url"], configured["api_key"]).history(length=250)
            mappings = db.execute(
                "SELECT m.external_id,m.normalized_username,u.id user_id,u.display_name "
                "FROM identity_mappings m JOIN users u ON u.id=m.user_id "
                "WHERE m.provider='tautulli' AND u.enabled=1"
            ).fetchall()
            mapping_by_id = {row["external_id"]: row for row in mappings}
            mapping_by_name = {row["normalized_username"]: row for row in mappings}
            active, grouped = {}, {}
            for item in history:
                external_id = str(item.get("user_id") or "")
                username = item.get("user") or item.get("friendly_name") or "Unknown"
                mapping = mapping_by_id.get(external_id) or mapping_by_name.get(normalize(username))
                user_key = f"internal:{mapping['user_id']}" if mapping else f"tautulli:{external_id or normalize(username)}"
                user_stats = active.setdefault(user_key, {
                    "user_id": mapping["user_id"] if mapping else None,
                    "display_name": mapping["display_name"] if mapping else username,
                    "plays": 0, "duration": 0,
                })
                user_stats["plays"] += 1
                user_stats["duration"] += int(item.get("duration") or 0)
                title = item.get("full_title") or item.get("title") or "Unknown"
                media_key = normalize(title)
                media_stats = grouped.setdefault(media_key, {"title": title, "plays": 0})
                media_stats["plays"] += 1
            most_active_users = sorted(active.values(), key=lambda item: (-item["plays"], -item["duration"], item["display_name"].casefold()))[:5]
            request_links = {}
            if seerr_base:
                for media in db.execute(
                    "SELECT title,media_type,tmdb_id FROM requests WHERE is_deleted=0 "
                    "AND (seerr_status IS NULL OR seerr_status!='removed_media_present') AND tmdb_id IS NOT NULL ORDER BY id"
                ):
                    request_links.setdefault(normalize(media["title"]), f"{seerr_base}/{media['media_type']}/{media['tmdb_id']}")
            top_plays = sorted(grouped.values(), key=lambda item: (-item["plays"], item["title"].casefold()))[:5]
            for media in top_plays:
                media["url"] = request_links.get(normalize(media["title"]))
                if not media["url"] and seerr_base:
                    media["url"] = f"{seerr_base}/search?query={quote(media['title'], safe='')}"
        else:
            tautulli_warning = "Tautulli is not configured."
    except IntegrationError as exc:
        tautulli_warning = str(exc)
    return render_template(
        "home.html", contribution_leaders=contribution_leaders[:5],
        most_active_users=most_active_users, recent_requests=recent_requests,
        top_plays=top_plays, tautulli_warning=tautulli_warning,
        currency=currency_code(), seerr_url=seerr_base,
    )


@bp.get("/dashboard")
@bp.get("/stats")
@owner_required
def dashboard():
    db = get_db()
    ledger_page, ledger_per_page = page_values("ledger_page", "ledger_per_page")
    totals = db.execute("SELECT COUNT(*) users,COALESCE(SUM(quota_bytes),0) quota FROM users WHERE enabled=1").fetchone()
    charged = db.execute("SELECT COALESCE(SUM(charged_bytes),0) n FROM requests r JOIN users u ON u.id=r.user_id WHERE u.enabled=1").fetchone()["n"]
    adjustments = db.execute("SELECT COALESCE(SUM(bytes),0) n FROM manual_adjustments a JOIN users u ON u.id=a.user_id WHERE u.enabled=1").fetchone()["n"]
    media = db.execute("SELECT media_type,COUNT(*) n FROM requests WHERE is_deleted=0 GROUP BY media_type").fetchall()
    requests_rows = db.execute(
        "SELECT r.*,u.display_name FROM requests r LEFT JOIN users u ON u.id=r.user_id "
        "WHERE r.is_deleted=0 AND (r.seerr_status IS NULL OR r.seerr_status!='removed_media_present') "
        "ORDER BY requested_at DESC,last_updated_at DESC"
    ).fetchall()
    ledger_total = db.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0]
    ledger_pages = max(1, math.ceil(ledger_total / ledger_per_page))
    ledger_page = min(ledger_page, ledger_pages)
    ledger = db.execute(
        "SELECT l.*,r.title,r.media_type,r.tmdb_id,r.seerr_request_id,r.requested_at,r.first_seen_at,"
        "COALESCE(u.display_name,r.display_username) username,r.servarr_source "
        "FROM usage_ledger l JOIN requests r ON r.id=l.request_id LEFT JOIN users u ON u.id=l.user_id "
        "ORDER BY COALESCE(r.requested_at,r.first_seen_at) DESC,l.id DESC LIMIT ? OFFSET ?",
        (ledger_per_page, (ledger_page - 1) * ledger_per_page),
    ).fetchall()
    last_sync = db.execute("SELECT * FROM sync_runs WHERE success=1 ORDER BY id DESC LIMIT 1").fetchone()
    activity, tautulli_error = [], None
    try:
        tautulli = integration_config("TAUTULLI")
        if tautulli["url"] and tautulli["api_key"]:
            activity = TautulliClient(tautulli["url"], tautulli["api_key"]).activity()
        else:
            tautulli_error = "Tautulli is not configured."
    except IntegrationError as exc:
        tautulli_error = str(exc)
    counts = {row["media_type"]: row["n"] for row in media}
    seerr_url = media_link_base()
    return render_template(
        "stats.html", totals=totals, charged=charged + adjustments,
        remaining=max(0, totals["quota"] - charged - adjustments), counts=counts,
        request_groups=request_buckets(requests_rows), ledger=ledger, last_sync=last_sync,
        activity=activity, tautulli_error=tautulli_error, seerr_url=seerr_url,
        ledger_page=ledger_page, ledger_pages=ledger_pages,
        ledger_per_page=ledger_per_page, ledger_page_sizes=PAGE_SIZES,
        ledger_total=ledger_total,
    )


@bp.get("/users/<int:user_id>")
@login_required
def user_page(user_id):
    if not current_owner() and int(user_id) != int(session.get("user_id", -1)):
        abort(403)
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        abort(404)
    identities = db.execute("SELECT * FROM identity_mappings WHERE user_id=? ORDER BY provider", (user_id,)).fetchall()
    all_requests = db.execute("SELECT * FROM requests WHERE user_id=? ORDER BY requested_at DESC,first_seen_at DESC", (user_id,)).fetchall()
    requests_rows = [row for row in all_requests if not row["is_deleted"]]
    deleted_requests = [row for row in all_requests if row["is_deleted"]]
    adjustments = db.execute("SELECT COALESCE(SUM(bytes),0) FROM manual_adjustments WHERE user_id=?", (user_id,)).fetchone()[0]
    charged = sum(row["charged_bytes"] for row in all_requests) + adjustments
    media_counts = {
        "movies": sum(1 for row in requests_rows if row["media_type"] == "movie"),
        "tv_requests": sum(1 for row in requests_rows if row["media_type"] == "tv"),
        "tv_seasons": sum(len(json.loads(row["requested_seasons"] or "[]")) for row in requests_rows if row["media_type"] == "tv"),
    }
    tautulli = {"plays": 0, "watch_time": 0, "history": [], "most_watched": []}
    warning = None
    mapping = next((row for row in identities if row["provider"] == "tautulli"), None)
    if mapping:
        try:
            configured = integration_config("TAUTULLI")
            history = TautulliClient(configured["url"], configured["api_key"]).history(mapping["external_id"])
            tautulli["history"] = history
            tautulli["plays"] = len(history)
            tautulli["watch_time"] = sum(int(item.get("duration") or 0) for item in history)
            grouped = {}
            for item in history:
                title = item.get("full_title") or item.get("title") or "Unknown"
                grouped[title] = grouped.get(title, 0) + 1
            tautulli["most_watched"] = sorted(grouped.items(), key=lambda item: item[1], reverse=True)[:5]
        except IntegrationError as exc:
            warning = str(exc)
    return render_template("user.html", user=user, identities=identities, requests=requests_rows, deleted_requests=deleted_requests, charged=charged, media_counts=media_counts, tautulli=tautulli, warning=warning, seerr_url=media_link_base())


@bp.route("/admin/users", methods=["GET", "POST"])
@owner_required
def users_admin():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "user":
            user_id = int(request.form["user_id"])
            db.execute("UPDATE users SET contribution_myr=?,quota_bytes=?,enabled=?,updated_at=? WHERE id=?", (float(request.form.get("contribution", 0)), int(float(request.form.get("quota_gb", 0)) * 1_000_000_000), int(request.form.get("enabled") == "on"), utcnow(), user_id))
        elif action == "adjustment":
            db.execute("INSERT INTO manual_adjustments(user_id,bytes,note,created_at) VALUES(?,?,?,?)", (int(request.form["user_id"]), int(float(request.form["gb"]) * 1_000_000_000), request.form["note"].strip(), utcnow()))
        elif action == "undo_adjustment":
            try:
                adjustment_id = int(request.form["adjustment_id"])
            except (KeyError, TypeError, ValueError):
                abort(400)
            with transaction(db, immediate=True):
                original = db.execute(
                    "SELECT * FROM manual_adjustments WHERE id=? AND reversed_at IS NULL AND reversal_of_adjustment_id IS NULL",
                    (adjustment_id,),
                ).fetchone()
                if not original:
                    abort(409)
                now = utcnow()
                reversal_id = db.execute(
                    "INSERT INTO manual_adjustments(user_id,bytes,note,created_at,reversal_of_adjustment_id) VALUES(?,?,?,?,?)",
                    (original["user_id"], -original["bytes"], f"Undo adjustment #{original['id']}: {original['note']}", now, original["id"]),
                ).lastrowid
                db.execute(
                    "UPDATE manual_adjustments SET reversed_at=?,reversed_by_adjustment_id=? WHERE id=?",
                    (now, reversal_id, original["id"]),
                )
        elif action == "mapping":
            user_id, provider = int(request.form["user_id"]), request.form["provider"]
            if provider not in {"seerr", "plex", "tautulli"}:
                abort(400)
            external_id, username = request.form["external_id"].strip(), request.form["username"].strip()
            db.execute("DELETE FROM identity_mappings WHERE provider=? AND external_id=?", (provider, external_id))
            db.execute("DELETE FROM identity_mappings WHERE user_id=? AND provider=?", (user_id, provider))
            db.execute("INSERT INTO identity_mappings(user_id,provider,external_id,username,normalized_username) VALUES(?,?,?,?,?)", (user_id, provider, external_id, username, normalize(username)))
        else:
            abort(400)
        flash("User settings saved.", "success")
        return redirect(url_for("main.users_admin"))
    users_page, users_per_page = page_values("users_page", "users_per_page")
    users_total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    users_pages = max(1, math.ceil(users_total / users_per_page))
    users_page = min(users_page, users_pages)
    user_query = "SELECT u.*,COALESCE(SUM(r.charged_bytes),0) charged_bytes FROM users u LEFT JOIN requests r ON r.user_id=u.id GROUP BY u.id ORDER BY u.display_name COLLATE NOCASE"
    all_users = db.execute(user_query).fetchall()
    users = db.execute(user_query + " LIMIT ? OFFSET ?", (users_per_page, (users_page - 1) * users_per_page)).fetchall()
    identities = db.execute("SELECT * FROM identity_mappings ORDER BY provider,username").fetchall()
    adjustment_page, adjustment_per_page = page_values("adjustment_page", "adjustment_per_page")
    adjustment_total = db.execute("SELECT COUNT(*) FROM manual_adjustments").fetchone()[0]
    adjustment_pages = max(1, math.ceil(adjustment_total / adjustment_per_page))
    adjustment_page = min(adjustment_page, adjustment_pages)
    adjustment_history = db.execute(
        "SELECT a.*,u.display_name FROM manual_adjustments a JOIN users u ON u.id=a.user_id "
        "ORDER BY a.created_at DESC,a.id DESC LIMIT ? OFFSET ?",
        (adjustment_per_page, (adjustment_page - 1) * adjustment_per_page),
    ).fetchall()
    return render_template(
        "users.html", users=users, all_users=all_users, identities=identities,
        adjustment_history=adjustment_history, currency=currency_code(), page_sizes=PAGE_SIZES,
        users_page=users_page, users_pages=users_pages, users_per_page=users_per_page, users_total=users_total,
        adjustment_page=adjustment_page, adjustment_pages=adjustment_pages,
        adjustment_per_page=adjustment_per_page, adjustment_total=adjustment_total,
    )


@bp.route("/admin/settings", methods=["GET", "POST"])
@owner_required
def settings():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "integrations":
            pending = []
            for name in INTEGRATIONS:
                url = request.form.get(f"{name}_url", "").strip().rstrip("/")
                parsed = urlparse(url)
                if url and (parsed.scheme not in {"http", "https"} or not parsed.netloc): abort(400)
                if name == "TAUTULLI" and parsed.path.rstrip("/").endswith("/home"): abort(400)
                pending.append((name, url, request.form.get(f"{name}_api_key", "").strip(), request.form.get(f"{name}_clear") == "on"))
            with transaction(get_db()):
                for name, url, api_key, clear_key in pending:
                    set_setting(f"integration.{name}.url", url)
                    if api_key: set_secret(f"integration.{name}.api_key", api_key)
                    if clear_key: set_setting(f"integration.{name}.api_key", "")
        elif action == "schedule":
            try:
                minutes = int(request.form.get("sync_interval_minutes", 60))
            except (TypeError, ValueError):
                abort(400)
            if minutes < 5 or minutes > 10080:
                abort(400)
            set_setting("sync_interval_minutes", str(minutes))
            current_app.config["SYNC_INTERVAL_MINUTES"] = minutes
            from . import scheduler
            if scheduler.get_job("reconcile"):
                scheduler.reschedule_job("reconcile", trigger="interval", minutes=minutes)
        elif action == "currency":
            currency = request.form.get("currency", "").upper()
            if currency not in CURRENCIES: abort(400)
            set_setting("currency", currency)
        elif action == "links":
            mode = request.form.get("link_mode", "internal")
            custom_url = request.form.get("seerr_link_base_url", "").strip().rstrip("/")
            if mode not in {"internal", "custom"}:
                abort(400)
            parsed = urlparse(custom_url)
            if custom_url and (parsed.scheme not in {"http", "https"} or not parsed.netloc):
                abort(400)
            if mode == "custom" and not custom_url:
                abort(400)
            with transaction(get_db()):
                set_setting("links.mode", mode)
                set_setting("links.seerr_base_url", custom_url)
        elif action == "sync":
            result = run_reconciliation("manual")
            flash(result["message"], "success" if result.get("ok") else "warning")
            return redirect(url_for("main.settings"))
        else:
            abort(400)
        flash("Contributarr settings saved.", "success")
        return redirect(url_for("main.settings"))
    integrations = {}
    for name in INTEGRATIONS:
        configured = integration_config(name)
        integrations[name] = {"url": configured["url"], "configured": bool(configured["url"] and configured["api_key"])}
    interval = int(get_setting("sync_interval_minutes", current_app.config["SYNC_INTERVAL_MINUTES"]))
    last_run = get_db().execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    public_base = current_app.config["EXTERNAL_URL"] or request.url_root.rstrip("/")
    webhook_secret = current_app.config["WEBHOOK_SECRET"]
    link_mode = get_setting("links.mode", "internal")
    seerr_link_base_url = get_setting("links.seerr_base_url", "")
    return render_template(
        "settings.html", owner=owner_record(), integrations=integrations,
        sync_interval=interval, last_run=last_run, currency=currency_code(),
        currencies=CURRENCIES, webhook_secret=webhook_secret,
        webhook_url=f"{public_base}/webhook/{webhook_secret}",
        link_mode=link_mode, seerr_link_base_url=seerr_link_base_url,
    )


@bp.post("/api/reconcile")
@owner_required
def api_reconcile():
    result = run_reconciliation("manual")
    return jsonify(result), (409 if result.get("busy") else 200 if result["ok"] else 207)


@bp.post("/webhook/<secret>")
def webhook(secret):
    if not current_app.config["WEBHOOK_SECRET"] or secret != current_app.config["WEBHOOK_SECRET"]:
        abort(404)
    result = run_reconciliation("seerr-webhook")
    return jsonify(result), (202 if result.get("busy") else 200 if result["ok"] else 207)
