# MyFitnessPal MCP Server

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that enables AI assistants
to interact with your MyFitnessPal data — food diary, body measurements, food search, and nutrition info.

## Quick Start

```bash
# 1. Clone & install
git clone https://github.com/RafalB82/myfitnesspal-mcp-python.git
cd myfitnesspal-mcp-python
cp .env.example .env   # fill in your MFP credentials

# 2. Build
docker build -t mfp-mcp .

# 3. Run (mount local .mfp_mcp for cookies & cache persistence)
docker run -d --name mfp-mcp \
  --restart unless-stopped \
  -p 8000:8000 -p 5900:5900 \
  -v ~/.mfp_mcp:/home/mcp/.mfp_mcp \
  -e MFP_USERNAME="$(grep MFP_USERNAME .env | cut -d= -f2)" \
  -e MFP_PASSWORD="$(grep MFP_PASSWORD .env | cut -d= -f2)" \
  -e MFP_SYNC_DAYS=90 \
  mfp-mcp

# Or with docker compose (for Traefik reverse proxy):
# docker compose up -d --build

# 4. First login — VNC (port 5900, password: mfpvnc)
#    Open VNC client to <host>:5900, complete reCAPTCHA if shown.
#    Timeout: 5 minutes. Once logged in, cookies are saved to ~/.mfp_mcp/cookies.json
#    and the background sync starts automatically.

# 5. Verify
curl -s http://localhost:8000/health
# → {"status":"ok"}

# 6. Trigger initial sync (optional — already runs on startup)
docker exec mfp-mcp python -m mfp_mcp.sync --days 90 --force
```

## Authentication

MyFitnessPal uses **reCAPTCHA** and **Cloudflare Bot Management**, making automated credential login unreliable.
The server uses **Camoufox** (stealth-hardened Firefox fork) for auth:

1. **Persistent browser profile** — Firefox session saved to Docker volume `mfp_browser_profile`.
   On first run, connect via VNC (port 5900, password `mfpvnc`) to complete reCAPTCHA manually.
2. **Cookies fallback** — `~/.mfp_mcp/cookies.json` saves the session token. If Camoufox fails,
   the server falls back to stored cookies.
3. **mfp_quick.py** — standalone cookie-based reader (~3s, no browser).

### First-time VNC login

On first run (or when cookies expire after ~30 days):

1. Container starts, Camoufox Firefox opens on `:99` display
2. Server detects no valid session → waits **5 minutes** for manual login
3. Connect VNC to `<host>:5900` (password: `mfpvnc`)
4. Complete login + reCAPTCHA in the Firefox window
5. Server auto-detects successful login → saves cookies to `~/.mfp_mcp/cookies.json`
6. Background sync starts automatically (90 days by default)

```bash
# After VNC login, cookies are persisted on host:
ls -la ~/.mfp_mcp/cookies.json   # 13 cookies, valid ~30 days
ls -la ~/.mfp_mcp/mfp_cache.db   # SQLite database with cached data
```

## Tools

### Read

| Tool | Description |
|------|-------------|
| `mfp_get_diary` | Get food diary for any date — cache-first (sub-ms), fallback to live MFP |
| `mfp_search_food` | Search the MFP food database (live only) |
| `mfp_get_food_details` | Detailed nutrition for a food item (live only) |
| `mfp_get_measurements` | Body measurement history — cache-first (sub-ms), fallback to live MFP |
| `mfp_get_water` | Get water intake for a date (live) |
| `mfp_get_goals` | Get daily nutrition goals (live) |
| `mfp_get_exercises` | Get logged exercises (live) |
| `mfp_get_report` | Get a diary report for a date range (live) |
| `mfp_list_own_foods` | List the user's custom foods (live) |

### Write / Create (merged from upstream, v2 JSON API)

| Tool | Description |
|------|-------------|
| `mfp_add_food_to_diary` | Add a food to the diary (POST /v2/diary) — returns the new entry UUID |
| `mfp_create_custom_food` | Create a private custom food (BFF /api/services/foods, CSRF) |
| `mfp_delete_custom_food` | Delete a custom food by id (204) |
| `mfp_remove_food_from_diary` | Remove a diary entry by entry id |
| `mfp_set_water` | Set a day's water total (POST /v2/diary/water, replace-semantics; cups→ml) |
| `mfp_set_measurement` | Log/upsert a body measurement (POST /v2/measurements) |
| `mfp_set_goals` | Update daily nutrition goals (client.set_new_goal) |

> **Write support** — reads were originally cache-first and read-only. The write/create
> tools were merged from upstream (`AdamWalt/myfitnesspal-mcp-python`) and rewritten on MFP's
> **v2 JSON API** + web BFF, which replaced the old form-based endpoints (those return 404
> now). `mfp_set_water` and `mfp_set_measurement` were additionally rewritten on v2 because
> the upstream versions still hit the removed legacy HTML forms.
>
> All write tools authenticate through the same Camoufox session as the read path and return
> the sync wrapper's client, so no extra login flow is needed.

> **Background sync** — the SQLite cache is refreshed on a cron schedule; changes made
> directly in the MFP app are picked up within ~8 hours.

## Fast Reader — `mfp_quick.py`

Standalone script that reads the diary via cached cookies (~3s) without spawning a browser.

```bash
# From the project directory
python3 mfp_quick.py              # today
python3 mfp_quick.py 2026-05-01   # specific date
```

Uses `~/.mfp_mcp/cookies.json` — falls back to MCP server (via Camoufox, ~23s) if cookies are expired.

## Architecture

```
┌─────────────────────────┐     streamable-http      ┌────────────────────────────────────┐
│  AI Assistant (Claude,  │ ◄──────────────────────► │  MFP MCP Server                    │
│  Perplexity, OpenClaw)  │      POST /mcp           │  port 8000                         │
└─────────────────────────┘                          │                                    │
                                                     │  ┌────────────────┐  ┌───────────┐  │
              ┌──────────────────────────────────────┤  │  SQLite Cache  │  │ Camoufox  │  │
              │                                      │  │  (mfp_cache.db)│  │ (auth)    │  │
              ▼                                      │  └───────┬────────┘  └─────┬─────┘  │
     ┌───────────────┐                               └─────────┼────────────────────┼────────┘
     │ mfp_quick.py  │  cookies-first (~3s)                    │                    │
     │ (host side)   │─────────────────────────────────────────┤                    │
     └───────────────┘                                          ▼                    ▼
                                                         ┌──────────────┐   ┌──────────────┐
                                                         │ myfitnesspal │   │  Background  │
                                                         │   .com API   │   │  Sync (cron) │
                                                         └──────────────┘   └──────┬───────┘
                                                                                   │
                                                                                   ▼
                                                                           ┌──────────────┐
                                                                           │  SQLite Cache│
                                                                           │  (same db)   │
                                                                           └──────────────┘
```

### Read path (fast path)

```
AI Agent → mfp_get_diary / mfp_get_measurements
         → SQLite cache (sub‑ms response)
         → fallback: live MFP via Camoufox (only if date is not synced)
```

### Data freshness (background sync)

A cron job inside the container syncs MFP data into SQLite 3 times per day
(default schedule: 06:00, 14:00, 22:00). An initial sync runs on container start.

Data changes made through the MFP app/website are picked up by the next sync run.

This means:
- **AI agents get answers in milliseconds** — no 20‑second Camoufox delay per query
- **Data is never older than ~8 hours** at worst (between sync runs)
- **No change to the authentication flow** — sync uses the same cookies/Camoufox chain

### Transports

| Transport | Config | Use case |
|-----------|--------|----------|
| `streamable-http` | `MCP_TRANSPORT=streamable-http` (default) | Remote clients, Perplexity, AI agents |
| `sse` | `MCP_TRANSPORT=sse` | Legacy SSE clients |
| `stdio` | `MCP_TRANSPORT=stdio` | Claude Desktop (local) |

## Docker Deployment

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MFP_USERNAME` | — | MFP email (for Camoufox login) |
| `MFP_PASSWORD` | — | MFP password (for Camoufox login) |
| `MFP_HOST` | `0.0.0.0` | Bind address |
| `MFP_PORT` | `8000` | Bind port |
| `MFP_TRANSPORT` | `streamable-http` | Transport type |
| `DOMAIN` | — | Public domain for Traefik |
| `CERT_RESOLVER` | `letsencrypt` | Traefik certresolver |
| `TRAEFIK_NETWORK` | `traefik` | Docker network for Traefik |
| `MFP_SYNC_SCHEDULE` | `0 6,14,22 * * *` | Cron expression for background sync |
| `MFP_SYNC_DAYS` | `30` | Number of days to sync each run |

### Volumes

| Volume | Mount | Purpose |
|--------|-------|---------|
| `mfp_cookies` | `/home/mcp/.mfp_mcp` | Cookies.json persistence |
| `mfp_browser_profile` | `/home/mcp/.mfp_mcp/browser_profile` | Full Firefox profile (persistent login) |
| `mfp_cache` | `/home/mcp/.mfp_mcp` | SQLite cache database (mfp_cache.db) |

### Traefik reverse proxy

```bash
docker network create traefik  # if not exists
docker compose up -d --build
# Server available at https://<DOMAIN>/mcp
```

## Perplexity Remote Connector

1. Settings → MCP Connectors → + Add Custom Connector
2. Name: `MyFitnessPal`, Transport: `Streamable HTTP`, URL: `https://<domain>/mcp`

## Claude Desktop (local)

```json
{
  "mcpServers": {
    "myfitnesspal": {
      "command": "/path/to/venv/bin/python",
      "args": ["-m", "mfp_mcp.server"],
      "env": {
        "MFP_USERNAME": "email",
        "MFP_PASSWORD": "password",
        "MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

## Project Structure

```
myfitnesspal-mcp-python/
├── .env.example               # Environment template
├── docker-compose.yml         # Traefik, VNC, volumes, sync config
├── Dockerfile                 # python:3.12-slim + Camoufox + Xvfb + cron
├── entrypoint.sh              # Xvfb → openbox → x11vnc → cron → MFP server
├── pyproject.toml
├── README.md
├── mfp_quick.py               # Fast cookie-based reader (~3s)
├── test_client.py             # MCP SSE test client
└── src/
    └── mfp_mcp/
        ├── __init__.py
        ├── server.py          # Main MCP server (FastMCP)
        ├── cache.py           # SQLite cache — MFPCache class
        └── sync.py            # Background sync script (python -m mfp_mcp.sync)
```

## Manual sync

```bash
# Inside the running container
docker exec mfp-mcp python -m mfp_mcp.sync --days 7

# Options:
#   --days N          Number of days to sync (default: 14)
#   --end-date YYYY-MM-DD  End date (default: today)
#   --force           Re-sync already synced dates
#
# Examples:
#   python -m mfp_mcp.sync --days 30
#   python -m mfp_mcp.sync --days 90 --end-date 2025-12-31 --force
```

The sync script authenticates via stored cookies first (sub‑second),
falling back to Camoufox only if cookies are expired.

## Troubleshooting

### `Permission denied: /home/mcp/.mfp_mcp/cookies.json`
Fix: `docker exec -u root mfp-mcp chown -R mcp:mcp /home/mcp/.mfp_mcp/`

### `Stored cookies are invalid`
Session token expired (~30 days). Log in via VNC to refresh, or re-inject cookies manually:
```bash
docker cp ~/.mfp_mcp/cookies.json mfp-mcp:/home/mcp/.mfp_mcp/cookies.json
docker exec -u root mfp-mcp chown mcp:mcp /home/mcp/.mfp_mcp/cookies.json
```

### `All authentication methods failed`
VNC to port 5900 (password: `mfpvnc`), check if logged into myfitnesspal.com.
Relogin if needed.

### Data is stale / missing recent days
The cron sync runs 3x/day. You can trigger an immediate sync:
```bash
docker exec mfp-mcp python -m mfp_mcp.sync --days 7 --force
```

## License

MIT — see [LICENSE](LICENSE)
