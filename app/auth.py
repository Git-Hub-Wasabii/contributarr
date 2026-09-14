import time
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from .db import claim_owner, get_db, owner_record, transaction, utcnow

bp = Blueprint("auth", __name__)
PLEX_API = "https://plex.tv/api/v2"


def plex_headers(token=None):
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": "Contributarr",
        "X-Plex-Client-Identifier": current_app.config["PLEX_CLIENT_IDENTIFIER"],
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


def current_owner():
    owner = owner_record()
    return owner if owner and session.get("plex_id") == owner["plex_id"] else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("plex_id"):
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def owner_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("plex_id"):
            return redirect(url_for("auth.login", next=request.path))
        if not current_owner():
            return render_template("denied.html"), 403
        return view(*args, **kwargs)
    return wrapped


def ensure_plex_user(plex_id, username):
    """Map a Plex account by stable ID, using exact normalized username only."""
    db = get_db()
    normalized = " ".join((username or "Plex user").strip().casefold().split())
    with transaction(db, immediate=True):
        existing = db.execute("SELECT user_id FROM identity_mappings WHERE provider='plex' AND external_id=?", (str(plex_id),)).fetchone()
        if existing:
            db.execute("UPDATE identity_mappings SET username=?,normalized_username=? WHERE provider='plex' AND external_id=?", (username, normalized, str(plex_id)))
            return existing["user_id"]
        matches = db.execute("SELECT DISTINCT user_id FROM identity_mappings WHERE normalized_username=?", (normalized,)).fetchall()
        user_id = matches[0]["user_id"] if len(matches) == 1 else None
        if user_id and db.execute("SELECT 1 FROM identity_mappings WHERE user_id=? AND provider='plex'", (user_id,)).fetchone(): user_id = None
        if user_id is None:
            now = utcnow()
            user_id = db.execute("INSERT INTO users(display_name,created_at,updated_at) VALUES(?,?,?)", (username or "Plex user", now, now)).lastrowid
        db.execute("INSERT INTO identity_mappings(user_id,provider,external_id,username,normalized_username) VALUES(?,?,?,?,?)", (user_id, "plex", str(plex_id), username or "Plex user", normalized))
        return user_id


@bp.get("/login")
def login():
    if current_owner():
        return redirect(url_for("main.home"))
    if session.get("plex_id") and session.get("user_id"):
        return redirect(url_for("main.home"))
    return render_template("login.html", claimed=owner_record() is not None)


@bp.get("/auth/plex/start")
def plex_start():
    response = requests.post(f"{PLEX_API}/pins", params={"strong": "true"}, headers=plex_headers(), timeout=(5, 15))
    response.raise_for_status()
    pin = response.json()
    session["plex_pin_id"] = pin["id"]
    session["plex_pin_code"] = pin["code"]
    base_url = current_app.config["EXTERNAL_URL"] or request.url_root.rstrip("/")
    callback = base_url + url_for("auth.plex_callback")
    query = urlencode({
        "clientID": current_app.config["PLEX_CLIENT_IDENTIFIER"],
        "code": pin["code"],
        "context[device][product]": "Contributarr",
        "forwardUrl": callback,
    })
    return redirect("https://app.plex.tv/auth#?" + query)


@bp.get("/auth/plex/callback")
def plex_callback():
    pin_id = session.pop("plex_pin_id", None)
    session.pop("plex_pin_code", None)
    if not pin_id:
        return redirect(url_for("auth.login"))
    token = None
    for _ in range(8):
        response = requests.get(f"{PLEX_API}/pins/{pin_id}", headers=plex_headers(), timeout=(5, 15))
        response.raise_for_status()
        token = response.json().get("authToken")
        if token:
            break
        time.sleep(0.5)
    if not token:
        return render_template("login.html", claimed=owner_record() is not None, error="Plex authentication did not complete. Please try again."), 400
    account_response = requests.get(f"{PLEX_API}/user", headers=plex_headers(token), timeout=(5, 15))
    account_response.raise_for_status()
    account = account_response.json()
    plex_id = str(account["id"])
    claim_owner(plex_id, account.get("username") or "Plex user", account.get("email"))
    user_id = ensure_plex_user(plex_id, account.get("username") or "Plex user")
    session.clear()  # The short-lived Plex token is deliberately discarded.
    session["plex_id"] = plex_id
    session["plex_username"] = account.get("username") or "Plex user"
    session["user_id"] = user_id
    current_app.session_interface.regenerate(session)
    return redirect(url_for("main.home"))


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
