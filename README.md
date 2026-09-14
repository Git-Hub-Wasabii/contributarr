# Contributarr

Contributarr is a self-hosted dashboard that permanently tracks storage used by media requested through Seerr. Plex provides sign-in, Radarr and Sonarr provide actual file sizes, and Tautulli adds viewing statistics.

## Security warning: claim the instance first

The first successful Plex sign-in becomes the administrator. Initially deploy Contributarr on your LAN, immediately sign in and claim it, and only then expose it through a trusted reverse proxy. Anyone who reaches an unclaimed installation first could become administrator.

After the claim, other Plex users may sign in. They can access only their own statistics, request history, and remaining quota. The administrator can access the global dashboard, every user page, reconciliation controls, integration configuration, and identity mapping.

## Version

Current release: **v26.09.14**

## Publish to GHCR

The included `publish-ghcr` workflow derives its lowercase GHCR namespace from the repository running the workflow and builds Linux AMD64 and ARM64 images. The official repository publishes:

```text
ghcr.io/git-hub-wasabii/contributarr:v26.09.14
ghcr.io/git-hub-wasabii/contributarr:latest
```

GitHub packages may initially be private. Open the package on GitHub, choose **Package settings**, then change its visibility to **Public** so Docker hosts can pull without registry credentials.

## First deployment

Download the single Compose file and start it. No repository clone, `.env`, manually generated secret, host data directory, or advance knowledge of the Docker host IP is required:

```bash
mkdir contributarr && cd contributarr
curl -O https://raw.githubusercontent.com/git-hub-wasabii/contributarr/main/docker-compose.yml
docker compose pull
docker compose up -d
docker compose ps
```

You can instead download `docker-compose.yml` manually and run the final three commands. In Portainer or another stack manager, paste that file as-is. Open `http://DOCKER_HOST_IP:9096`, complete the first Plex sign-in, then visit **Settings** to add the Seerr, Radarr, Sonarr, and Tautulli URLs and API keys.

Contributarr securely generates independent session and webhook secrets on first startup. They are stored as `/data/.session_secret` and `/data/.webhook_secret` inside the persistent `contributarr-data` Docker volume and are never printed to normal logs. The administrator can copy the generated webhook URL from Settings.

The Compose file gives the volume an explicit name, so it can be inspected with:

```bash
docker volume inspect contributarr-data
```

The database, generated secrets, server-side sessions, settings, and integration credentials survive `docker compose down`, container recreation, and image upgrades. As with any Docker volume, `docker compose down -v` deliberately deletes it.

## Plex client identifier

Plex PIN sign-in requires every application installation to send a stable client identifier. It is not an API key, Plex token, server claim token, or permission grant. Contributarr generates this random identifier automatically on first startup and saves it in SQLite. You do not configure it and it provides no access to Plex by itself. The temporary token returned after a successful PIN login is used only to identify the account and is then discarded.

## Integration configuration

The administrator configures service URLs and API keys in **Admin Settings**. API keys are encrypted before storage in SQLite and are never returned to the browser after saving. Encryption is derived from the automatically persisted session secret, so it remains decryptable across upgrades and container recreation.

Do not include `/home` in the Tautulli URL. Typical base URLs are:

- Seerr: `http://HOST:5055`
- Radarr 1080p and 4K: `http://HOST:7878`
- Sonarr: `http://HOST:8989`
- Tautulli: `http://HOST:8181`

Settings shows the complete protected URL to copy into Seerr as a **Request Available** webhook.

## Users and identity mapping

Plex, Seerr, and Tautulli accounts are joined using stable provider IDs. Contributarr attempts only exact username matching after case-folding and trimming whitespace; it never performs fuzzy merges. If usernames differ or a match is ambiguous, the administrator can correct the mapping on the Users page.

A Plex user who signs in before reconciliation receives an internal account immediately. A later exact Seerr match is attached to that account. Authenticated users cannot browse other users, the global dashboard, admin settings, or administrative API endpoints.

## Ownership recovery

```bash
docker compose exec contributarr flask owner show
docker compose exec contributarr flask owner clear
docker compose exec contributarr flask owner clear --yes
```

Clearing ownership changes only the administrator claim. It preserves users, mappings, quotas, requests, adjustments, and ledger entries. The next successful Plex login claims administration.

## Reconciliation and charging

The single Gunicorn worker reconciles every `SYNC_INTERVAL_MINUTES` (60 by default) and in response to the Seerr webhook. The in-process scheduler and lock are suitable for this one-worker deployment; do not add workers without moving scheduling and locking to an external service.

Each request is isolated so one bad item does not stop the remainder. Movies check both Radarr instances by TMDB ID. TV requests resolve TVDB identity and sum only requested Sonarr seasons. The stored charge is always:

```text
max(previous_charge, currently_observed_size)
```

Deleting only a media file or replacing it with a smaller encode never lowers its charge. When a previously tracked request is removed from a successfully retrieved complete Seerr request list, Contributarr moves it to **Deleted requests**, records the previous charge as refunded, and returns those bytes to the user's quota. The original immutable usage ledger remains available for audit. Storage uses decimal GB (`1 GB = 1,000,000,000 bytes`). A Tautulli outage produces a warning and never blocks quotas or reconciliation.

The Settings page saves all integration URLs and encrypted API keys in one submission. It also provides an application-wide display-currency selector; changing the code changes presentation only and does not convert saved contribution amounts.

## Reverse proxy

For a normal LAN connection, Contributarr derives Plex callback and webhook URLs from the incoming request. It does not trust `X-Forwarded-*` headers by default.

Advanced users can add environment entries to the Compose service. `EXTERNAL_URL` forces a canonical public origin and enables Secure cookies when it starts with `https://`. `PROXY_FIX_COUNT` must be the exact number of trusted proxies that overwrite `X-Forwarded-*` headers—usually `1`; keep the default `0` for direct connections. Other optional overrides are `APP_NAME`, `TZ`, `PORT`, `DB_PATH`, `SYNC_INTERVAL_MINUTES`, `SESSION_DIR`, `SESSION_SECRET`, and `WEBHOOK_SECRET`.

Secret precedence is explicit environment value, then its persisted file, then secure generation. On the first upgraded start with an environment-provided secret, Contributarr copies that same value into the volume. You can subsequently remove the override without invalidating sessions or losing access to encrypted integration keys. Deliberately changing `SESSION_SECRET` rotates the encryption key, invalidates sessions, and requires integration API keys to be entered again.

## Database, backups, and existing installations

SQLite is stored at `/data/contributarr.db` in `contributarr-data`, with numbered migrations applied automatically during startup. A portable volume backup can be created from the directory containing the Compose file:

```bash
docker run --rm -v contributarr-data:/data:ro -v "$PWD:/backup" alpine \
  tar czf /backup/contributarr-backup-$(date +%Y%m%d-%H%M%S).tar.gz -C /data .
```

Older Contributarr releases took secrets from `.env` and used `./data:/data`. Upgrade the image once while the old Compose file and `.env` are still active. This lets the new startup code persist those exact existing secrets beside the database before migration:

```bash
docker compose pull
docker compose up -d
docker compose exec contributarr sh -c \
  'test -s /data/.session_secret && test -s /data/.webhook_secret'
```

After that check succeeds, stop the stack and copy the complete bind-mounted directory—including its new dotfiles—into the named volume:

```bash
docker compose down
docker volume create contributarr-data
docker run --rm -v "$PWD/data:/from:ro" -v contributarr-data:/to alpine \
  sh -c 'cp -a /from/. /to/'
```

Then install the new `docker-compose.yml` and run `docker compose up -d`. Keep the old `.env` and `./data` backup until the upgraded application and saved integration keys are verified. If the old image can no longer be started, temporarily supply the previous `SESSION_SECRET` and `WEBHOOK_SECRET` as service environment entries on the first new-image start; they will be persisted and can then be removed.

To import a compatible older tracker database without modifying the source, place it in the volume and run:

```bash
docker compose exec contributarr flask legacy import --source /data/tracker.db
```

The importer de-duplicates by Seerr request ID, preserves historical charges, and never removes source or existing records.

## Updating from GHCR

```bash
cd /path/to/contributarr-stack
docker compose pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 contributarr
```

Normal updates preserve `contributarr-data` and do not require cloning, `git pull`, `.env`, or `docker compose down`. The supplied Compose file follows `latest`; advanced users can edit its image reference to pin a version tag.

## Tests

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

Timestamps are stored in UTC and displayed in `Asia/Kuala_Lumpur`. The public health endpoint exposes no integration details or secrets.
