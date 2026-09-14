import logging
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from cachelib.file import FileSystemCache
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_session import Session
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .db import close_db, get_setting, initialize_instance_identity, migrate

csrf = CSRFProtect()
scheduler = BackgroundScheduler(daemon=True)


def create_app(test_config=None):
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.from_object(Config)
    if test_config: app.config.update(test_config)
    Config.validate(app.config.get("TESTING", False))
    Path(app.config["SESSION_FILE_DIR"]).mkdir(parents=True, exist_ok=True)
    app.config["SESSION_CACHELIB"] = FileSystemCache(cache_dir=app.config["SESSION_FILE_DIR"], threshold=500)
    count = app.config["PROXY_FIX_COUNT"]
    if count: app.wsgi_app = ProxyFix(app.wsgi_app, x_for=count, x_proto=count, x_host=count, x_port=count, x_prefix=count)
    Session(app)
    app.teardown_appcontext(close_db)

    from . import auth, cli, main
    app.register_blueprint(auth.bp)
    app.register_blueprint(main.bp)
    app.jinja_env.globals["current_owner"] = auth.current_owner
    public_endpoints = {"auth.login", "auth.plex_start", "auth.plex_callback", "health", "static", "main.webhook", "auth.logout"}

    @app.before_request
    def default_deny_authorization():
        if request.endpoint in public_endpoints:
            return None
        if not session.get("plex_id"):
            return redirect(url_for("auth.login", next=request.path))
        return None

    csrf.init_app(app)
    csrf.exempt(main.webhook)
    cli.register(app)

    @app.get("/health")
    def health(): return jsonify(status="ok", application=app.config["APP_NAME"])

    @app.errorhandler(400)
    def bad_request(error):
        if "csrf" in str(error).lower(): return "Invalid or missing CSRF token.", 400
        return "Bad request.", 400

    @app.template_filter("gb")
    def gb(value): return main.bytes_gb(value)

    @app.template_filter("localtime")
    def localtime(value): return main.local_time(value)

    @app.template_filter("media")
    def media(value): return main.media_label(value)

    @app.template_filter("money")
    def money(value): return main.money(value)

    with app.app_context():
        migrate()
        initialize_instance_identity()
        configured_sync_interval = int(get_setting("sync_interval_minutes", app.config["SYNC_INTERVAL_MINUTES"]))

    if not app.config.get("TESTING") and not scheduler.running:
        def scheduled_sync():
            with app.app_context():
                from .reconcile import run_reconciliation
                run_reconciliation("scheduled")
        scheduler.add_job(scheduled_sync, "interval", minutes=configured_sync_interval, id="reconcile", replace_existing=True, max_instances=1)
        scheduler.start()
    return app
