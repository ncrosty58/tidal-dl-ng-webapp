# tidal-dl Web Service

A self-contained, Dockerized Flask web wrapper for [tidal-dl-ng](https://github.com/exislow/tidal-dl-ng) (TIDAL downloader) that provides a simple UI and streams download progress over Server-Sent Events (SSE).

> **Important:** This project executes a bundled `tidal-dl-ng` binary and can expose system resources if deployed publicly. Read the Security section before deploying.

## Quick start (Docker)

The image bundles `tidal-dl-ng`, `ffmpeg`, and all Python dependencies — nothing to install on the host beyond Docker.

```bash
git clone https://github.com/ncrosty58/tidal-dl.git
cd tidal-dl
cp .env.example .env      # edit if you want to set DOWNLOAD_TOKEN
docker compose up -d --build
```

Open `http://<host>:5050/`.

### Logging in to TIDAL

`tidal-dl-ng`'s login/session (`token.json`) and its settings (`settings.json`) live in the named volume `tidal-dl_tidal_dl_config`, mounted at `/home/app/.config/tidal_dl_ng` in the container — so once you're logged in, you stay logged in across rebuilds/restarts.

If the volume is empty (first run, no prior login), authenticate once:

```bash
docker compose exec tidal-dl tidal-dl-ng login
```

Follow the printed device-link URL/code on any browser. After that, the container is fully self-contained — no host-side `tidal-dl-ng` install or re-login required.

### Where downloads land

`docker-compose.yml` bind-mounts a host directory to `/downloads` in the container, and `settings.json`'s `download_base_path` is set to `/downloads` to match. Edit the volume line in `docker-compose.yml` to point at wherever your music library lives:

```yaml
volumes:
  - /path/to/your/music:/downloads
```

## Configuration

Configuration is resolved in this order (highest precedence first):

1. **Environment variables**
2. **System TOML file**: `/etc/tidal-dl/config.toml`
3. **Local TOML file**: `config.toml` next to `app.py`
4. **Built-in defaults**

Built-in defaults now correctly point at `./templates` and `./static` — a config file is optional, not required (previously the fallback pointed at the app's root directory, which 404'd/500'd without a config file present; that's fixed).

| Variable | Purpose | Default |
|---|---|---|
| `TIDAL_DL_BIN` | path to the `tidal-dl-ng` binary | `tidal-dl-ng` (resolved via `PATH`) |
| `DOWNLOAD_TIMEOUT` | seconds, `0` = no timeout | `0` |
| `DOWNLOAD_TOKEN` | optional token required in `X-Download-Token` header | unset |
| `FLASK_HOST` / `FLASK_PORT` | bind address (only used when running `python app.py` directly, not under gunicorn) | `0.0.0.0` / `5050` |

See `config.example.toml` for the TOML equivalent of these settings.

## Features

- Web UI to paste TIDAL URLs and start downloads
- Real-time streaming output via SSE
- Stop running downloads
- Optional token-based access control
- TOML and environment variable configuration
- PWA-installable (manifest + service worker)

## Endpoints

- `GET /tidal-dl/` - UI page
- `POST /tidal-dl/download` - Start download (form field `url=`). If `DOWNLOAD_TOKEN` is set, include header `X-Download-Token: <token>`.
- `POST /tidal-dl/stop` - Stop running download.
- `GET /tidal-dl/stream` - SSE stream for live output.

## Vendored dependency

As of 2026-07-19, `tidal-dl-ng` is no longer published on PyPI and its upstream GitHub repo (`exislow/tidal-dl-ng`) is gone. `requirements.txt` installs it from a vendored wheel in `vendor/`, repackaged from a still-working local install (same files/hashes, unmodified — see `vendor/README.md` for details and license info). If upstream reappears, switch back to a normal PyPI/git dependency and delete `vendor/`.

## Security

- **Do not** expose this service to the open internet without a reverse proxy and access controls. The built-in UI has no login/auth of its own beyond the optional `DOWNLOAD_TOKEN` header (which the UI itself doesn't send — it's meant for API/proxy-injected auth, not browser use).
- Run behind Nginx/Caddy/Traefik with TLS if reachable outside your LAN.
- The container runs as an unprivileged user (uid 1000).
- Inputs on the `/download` endpoint are passed as `subprocess` argv (not through a shell), so there's no shell-injection surface — but the endpoint itself is unauthenticated by default. Treat it as LAN-only unless you add auth.

## Local development (without Docker)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Contributing

Feel free to open issues or PRs.

## License

MIT License - see LICENSE file. (Bundled `tidal-dl-ng` is AGPL-3.0 — see `vendor/README.md`.)
