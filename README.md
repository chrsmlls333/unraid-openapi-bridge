# Unraid OpenAPI Bridge

A tiny, **read-only** REST/OpenAPI facade over the official
[Unraid GraphQL API](https://docs.unraid.net/API/how-to-use-the-api/), designed
to be consumed as an **Open WebUI "OpenAPI" tool-server connection**.

The Unraid API is GraphQL-only and serves no OpenAPI spec, so this bridge
exposes a curated set of REST endpoints (with `/openapi.json`), translates
them to GraphQL calls, and injects the server-side API key — the model never
sees the key and can only reach read-only data.

## What it exposes

| Endpoint | Description |
|---|---|
| `GET /health` | Bridge liveness + upstream reachability (no auth) |
| `GET /system` | OS, CPU, RAM modules, Unraid/kernel versions, uptime |
| `GET /system/cpu` | CPU details + current utilization % |
| `GET /system/memory` | RAM/swap utilization (human-readable) |
| `GET /system/temps` | Temperature sensors + summary |
| `GET /system/network` | Interfaces + rx/tx rates |
| `GET /array` | Array state, capacity, disk/cache health incl. temps |
| `GET /docker` | Container inventory (state, updates, ports, WebUI links) + networks |
| `GET /docker/container/{id}` | One container in detail (mounts, restart policy) |
| `GET /notifications?box=unread&limit=25` | Notification counts + list |
| `GET /shares` | User shares with usage |
| `GET /time` | Server time and timezone |

All endpoints except `/health` require the bridge token if `BRIDGE_API_KEY`
is set (via `Authorization: Bearer <token>` or `X-API-Key: <token>`).

**No write actions are exposed** — no container start/stop, no array
start/stop. Widen the scope deliberately later if you want them.

## Deploy on the Unraid box (Docker)

The bridge is meant to run on the Unraid server itself, next to the API.

```bash
# 1. Get the repo onto the server (or clone with git)
cd /boot/config/plugins            # persists on the flash drive
git clone <this repo> unraid-openapi-bridge
cd unraid-openapi-bridge

# 2. Configure
cp .env.example .env
nano .env   # set UNRAID_API_KEY (VIEWER role) and BRIDGE_API_KEY (random string)

# 3. Build and run (compose is bundled with Unraid 7.x)
docker compose up -d --build
# or classic:
docker build -t unraid-openapi-bridge:latest .
docker run -d --name unraid-openapi-bridge --restart unless-stopped \
  --env-file .env -p 8085:8080 unraid-openapi-bridge:latest

# 4. Verify
curl http://192.168.8.186:8085/health
```

### Creating the Unraid API key (VIEWER = read-only)

On the server, as root:

```bash
unraid-api apikey --create --name "OpenWebUI Bridge" \
  --description "Open WebUI read-only bridge" --roles VIEWER --json
```

Store the key in `.env` as `UNRAID_API_KEY=...`. Manage/rotate it later in
the Unraid WebGUI: **Settings → Management Access → API Keys**.

## Register in Open WebUI

1. **Admin Panel → Settings → External Tools → Add Connection** (or
   Tools/Integrations depending on version).
2. **Type:** `OpenAPI`
3. **URL:** `http://<unraid-lan-ip>:8085`
4. **Auth:** Bearer — paste the value of `BRIDGE_API_KEY`.
5. Save, then confirm the tools appear; test with a chat prompt like
   *"What's the array state and hottest disk on the Unraid server?"*

Access control: keep the connection limited to Admin (default) unless you
intend other users to have it.

## Security notes

- The Unraid API key is VIEWER-role and never leaves the bridge process.
- The bridge talks to the Unraid API over HTTP to `127.0.0.1`/LAN — keep it
  LAN-only. Don't port-forward 8085 to the internet.
- The bridge itself is additionally protected by its own Bearer token.
- Introspection/sandbox on the Unraid API can stay disabled; none of this
  requires it in normal operation.

## License

MIT
