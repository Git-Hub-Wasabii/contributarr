import json
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from .auth import current_owner, login_required, owner_required
from .db import get_db, get_setting, integration_config, owner_record, set_secret, set_setting, transaction, utcnow
from .integrations import IntegrationError, TautulliClient
from .reconcile import normalize, run_reconciliation

bp = Blueprint("main", __name__)
INTEGRATIONS = ("SEERR", "RADARR_1080", "RADARR_4K", "SONARR", "TAUTULLI")
CURRENCIES = ("MYR", "USD", "SGD", "EUR", "GBP", "AUD", "CAD", "JPY", "CNY", "INR")


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


def request_buckets(rows):
    definitions = [
        ("Pending approval", {"1", "pending"}),
        ("Approved or processing", {"2", "approved", "processing"}),
        ("Partially available", {"3", "partial", "partially available"}),
        ("Available", {"4", "5", "available"}),
        ("Failed", {"failed", "declined"}),
    ]
    buckets = [{"label": label, "items": []} for label, _ in definitions]
    for row in rows:
        status = (row["seerr_status"] or "").lower()
        target = next((index for index, (_, statuses) in enumerate(definitions) if status in statuses), 1)
        buckets[target]["items"].append(row)
    return buckets


@bp.get("/")
@login_required
def index():
    destination = url_for("main.dashboard") if current_owner() else url_for("main.user_page", user_id=session["user_id"])
    return redirect(destination)


@bp.get("/dashboard")
@bp.get("/stats")
@owner_required
def dashboard():
    db = get_db()
    totals = db.execute("SELECT COUNT(*) users,COALESCE(SUM(quota_bytes),0) quota FROM users WHERE enabled=1").fetchone()
    charged = db.execute("SELECT COALESCE(SUM(charged_bytes),0) n FROM requests r JOIN users u ON u.id=r.user_id WHERE u.enabled=1").fetchone()["n"]
    adjustments = db.execute("SELECT COALESCE(SUM(bytes),0) n FROM manual_adjustments a JOIN users u ON u.id=a.user_id WHERE u.enabled=1").fetchone()["n"]
    media = db.execute("SELECT media_type,COUNT(*) n FROM requests WHERE is_deleted=0 GROUP BY media_type").fetchall()
    requests_rows = db.execute("SELECT r.*,u.display_name FROM requests r LEFT JOIN users u ON u.id=r.user_id WHERE r.is_deleted=0 ORDER BY requested_at DESC,last_updated_at DESC").fetchall()
    ledger = db.execute("SELECT l.*,r.title,r.media_type,r.tmdb_id,r.seerr_request_id,COALESCE(u.display_name,r.display_username) username,r.servarr_source FROM usage_ledger l JOIN requests r ON r.id=l.request_id LEFT JOIN users u ON u.id=l.user_id ORDER BY l.created_at DESC LIMIT 100").fetchall()
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
    seerr_url = integration_config("SEERR")["url"]
    return render_template(
        "stats.html", totals=totals, charged=charged + adjustments,
        remaining=max(0, totals["quota"] - charged - adjustments), counts=counts,
        request_groups=request_buckets(requests_rows), ledger=ledger, last_sync=last_sync,
        activity=activity, tautulli_error=tautulli_error, seerr_url=seerr_url,
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
    return render_template("user.html", user=user, identities=identities, requests=requests_rows, deleted_requests=deleted_requests, charged=charged, media_counts=media_counts, tautulli=tautulli, warning=warning, seerr_url=integration_config("SEERR")["url"])


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
    users = db.execute("SELECT u.*,COALESCE(SUM(r.charged_bytes),0) charged_bytes FROM users u LEFT JOIN requests r ON r.user_id=u.id GROUP BY u.id ORDER BY u.display_name COLLATE NOCASE").fetchall()
    identities = db.execute("SELECT * FROM identity_mappings ORDER BY provider,username").fetchall()
    return render_template("users.html", users=users, identities=identities, currency=currency_code())


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
    return render_template(
        "settings.html", owner=owner_record(), integrations=integrations,
        sync_interval=interval, last_run=last_run, currency=currency_code(),
        currencies=CURRENCIES, webhook_secret=webhook_secret,
        webhook_url=f"{public_base}/webhook/{webhook_secret}",
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
