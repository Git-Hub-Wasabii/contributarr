import click
from flask import current_app

from .db import get_db, import_legacy, migrate, owner_record


def register(app):
    @app.cli.group("owner")
    def owner_group():
        """Inspect or clear the instance owner."""

    @owner_group.command("show")
    def owner_show():
        owner = owner_record()
        if not owner: click.echo("This Contributarr instance is unclaimed.")
        else: click.echo(f"Plex ID: {owner['plex_id']}\nUsername: {owner['username']}\nEmail: {owner['email'] or '—'}\nClaimed: {owner['claimed_at']}")

    @owner_group.command("clear")
    @click.option("--yes", is_flag=True, help="Skip confirmation.")
    def owner_clear(yes):
        if not owner_record():
            click.echo("This Contributarr instance is already unclaimed.")
            return
        if not yes and not click.confirm("Clear ownership? All existing data will remain"):
            raise click.Abort()
        get_db().execute("DELETE FROM owner WHERE singleton=1")
        click.echo("Ownership cleared. Users, quotas, requests and ledger data were preserved.")

    @app.cli.group("db")
    def db_group(): """Database maintenance."""

    @db_group.command("upgrade")
    def db_upgrade():
        migrate()
        click.echo("Database is up to date.")

    @app.cli.group("legacy")
    def legacy_group(): """Legacy tracker migration."""

    @legacy_group.command("import")
    @click.option("--source", default="/data/tracker.db", show_default=True)
    def legacy_import(source):
        result = import_legacy(source)
        click.echo(f"Imported {result['users']} users, {result['requests']} requests, and {result['ledger']} ledger entries. The source was not modified.")
