# Contributarr

Contributarr is a self-hosted dashboard that permanently tracks storage used by media requested through Seerr. Plex provides sign-in, Radarr and Sonarr provide actual file sizes, and Tautulli adds viewing statistics.

## Security warning: claim the instance first

The first successful Plex sign-in becomes the administrator. Initially deploy Contributarr on your LAN, immediately sign in and claim it, and only then expose it through a trusted reverse proxy. Anyone who reaches an unclaimed installation first could become administrator.

After the claim, other Plex users may sign in. They can access only their own statistics, request history, and remaining quota. The administrator can access the global dashboard, every user page, reconciliation controls, integration configuration, and identity mapping.

## Version

Current release: **v26.09.14**

## Publish to GHCR

Push this repository to [`Git-Hub-Wasabii/Contributarr`](https://github.com/Git-Hub-Wasabii/Contributarr). The included `publish-ghcr` workflow publishes exclusively to the `git-hub-wasabii` GHCR namespace, lowercases the complete image name, and builds Linux AMD64 and ARM64 images:

```text
ghcr.io/git-hub-wasabii/contributarr:v26.09.14
ghcr.io/git-hub-wasabii/contributarr:latest
```

GitHub packages may initially be private. Open the package on GitHub, choose **Package settings**, then change its visibility to **Public** so Docker hosts can pull without registry credentials.

## First deployment

Copy `docker-compose.yml` and `.env.example` to a directory on the Docker host once. Rename `.env.example` to `.env`, then use `ghcr.io/git-hub-wasabii/contributarr:latest` for pull-only updates without editing Compose or `ghcr.io/git-hub-wasabii/contributarr:v26.09.14` to pin this release:

```bash
cp .env.example .env
openssl rand -hex 32  # use as SESSION_SECRET
openssl rand -hex 32  # use as WEBHOOK_SECRET
```

Edit `.env`, replacing the two secret placeholders and setting `EXTERNAL_URL` to the address users open. Then start the stack:

```bash
docker compose pull
docker compose up -d
docker compose logs --tail=100 contributarr
```

Open `http://DOCKER_HOST_IP:9096`, complete the first Plex sign-in, then visit **Settings** to add the Seerr, Radarr, Sonarr, and Tautulli URLs and API keys. All examples and container mappings use port `9096`.

In Portainer or another Compose stack manager, paste `docker-compose.yml` once and provide the variables from `.env.example`. Future application updates pull directly from GHCR; the stack does not need to clone the source again.

## Plex client identifier

Plex PIN sign-in requires every application installation to send a stable client identifier. It is not an API key, Plex token, server claim token, or permission grant. Contributarr generates this random identifier automatically on first startup and saves it in SQLite. You do not configure it and it provides no access to Plex by itself. The temporary token returned after a successful PIN login is used only to identify the account and is then discarded.

## Integration configuration

Only bootstrap and security values live in `.env`. The administrator configures service URLs and API keys in **Admin Settings**. API keys are encrypted before storage in SQLite and are never returned to the browser after saving. Encryption is derived from `SESSION_SECRET`, so retain the same session secret across upgrades and container recreation.

Do not include `/home` in the Tautulli URL. Typical base URLs are:

- Seerr: `http://HOST:5055`
- Radarr 1080p and 4K: `http://HOST:7878`
- Sonarr: `http://HOST:8989`
- Tautulli: `http://HOST:8181`

Configure a Seerr **Request Available** webhook as:

```text
http://CONTRIBUTARR_HOST:9096/webhook/YOUR_WEBHOOK_SECRET
```

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

Set `EXTERNAL_URL` to the public HTTPS origin to enable Secure cookies. Set `PROXY_FIX_COUNT` to the exact number of trusted proxies that overwrite `X-Forwarded-*` headers—usually `1`. Leave it at `0` for direct connections. Do not accept forwarded headers directly from untrusted clients.

## Database and legacy import

SQLite is stored at `/data/contributarr.db`, with numbered migrations applied automatically during startup. Back up first, then import a compatible older tracker without modifying the source:

```bash
docker compose exec contributarr flask legacy import --source /data/tracker.db
```

The importer de-duplicates by Seerr request ID, preserves historical charges, and never removes source or existing records.

## Updating from GHCR

```bash
cd /path/to/contributarr-stack
cp data/contributarr.db data/contributarr-backup-$(date +%Y%m%d-%H%M%S).db
docker compose pull
docker compose up -d
docker compose logs --tail=100 contributarr
```

Normal updates do not require cloning, `git pull`, or `docker compose down`. To follow stable releases, update `CONTRIBUTARR_IMAGE` to the new version tag before pulling. To follow every main-branch publication automatically, use the `latest` tag.

## Tests

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

Timestamps are stored in UTC and displayed in `Asia/Kuala_Lumpur`. The public health endpoint exposes no integration details or secrets.
