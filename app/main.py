"""Unraid OpenAPI bridge — read-only REST/OpenAPI facade over the official Unraid GraphQL API.

Built for Open WebUI's "OpenAPI" tool-server connection type: it introspects
/openapi.json and exposes each GET endpoint as a tool. All endpoints are
read-only; the underlying Unraid API key is VIEWER-role and lives server-side
only (it is never exposed to the model).

Config via environment:
  UNRAID_API_URL   — full URL of the Unraid GraphQL endpoint (default below)
  UNRAID_API_KEY   — Unraid API key (VIEWER role recommended); REQUIRED
  BRIDGE_API_KEY   — token clients must present as `Authorization: Bearer <key>`
                     or `X-API-Key: <key>`. If unset, the bridge is unauthenticated
                     (LAN use only).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

UNRAID_API_URL = os.environ.get("UNRAID_API_URL", "http://192.168.8.186:8080/graphql")
UNRAID_API_KEY = os.environ.get("UNRAID_API_KEY", "")
BRIDGE_API_KEY = os.environ.get("BRIDGE_API_KEY", "")

BRIDGE_VERSION = "1.0.0"
_HTTP_TIMEOUT = 20.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    yield
    await app.state.http.aclose()


app = FastAPI(
    title="Unraid Bridge",
    description=(
        "Read-only REST facade over the official Unraid GraphQL API "
        "(system, array/disks, Docker containers, notifications, shares, metrics). "
        "Presented as an OpenAPI endpoint for Open WebUI tool servers."
    ),
    version=BRIDGE_VERSION,
    lifespan=lifespan,
)


# ---------------------------------------------------------------- auth


async def require_key(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> None:
    """Require the bridge token when BRIDGE_API_KEY is configured."""
    if not BRIDGE_API_KEY:
        return
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    elif x_api_key:
        supplied = x_api_key.strip()
    if supplied != BRIDGE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing bridge API key")


# ---------------------------------------------------------------- graphql client


class UpstreamError(HTTPException):
    def __init__(self, errors: list[dict[str, Any]]):
        msgs = "; ".join(e.get("message", "unknown error") for e in errors)
        super().__init__(status_code=502, detail=f"Unraid API error: {msgs}")


async def gql(query: str) -> dict[str, Any]:
    """POST a read-only GraphQL query and return the `data` payload."""
    resp = await app.state.http.post(
        UNRAID_API_URL,
        json={"query": query},
        headers={"x-api-key": UNRAID_API_KEY},
    )
    if resp.status_code in (401, 403):
        raise HTTPException(status_code=502, detail=f"Unraid API rejected the key (HTTP {resp.status_code})")
    if resp.status_code >= 400:
        body = resp.text[:500]
        raise HTTPException(status_code=502, detail=f"Unraid API returned HTTP {resp.status_code}: {body}")
    payload = resp.json()
    if payload.get("errors"):
        raise UpstreamError(payload["errors"])
    return payload.get("data") or {}


# ---------------------------------------------------------------- helpers


def _num(value: Any) -> Optional[float]:
    """Coerce GraphQL BigInt (int or str) into a number."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _gib(value: Any) -> Optional[str]:
    """Bytes (or raw GB string from ArrayCapacity) to a human string."""
    n = _num(value)
    if n is None:
        return value if isinstance(value, str) else None
    # ArrayCapacity already reports GiB strings; byte values need /1024^3.
    # Heuristic: ArrayCapacity strings are small (<10^4); raw bytes are huge.
    if n < 10_000 and isinstance(value, str):
        return f"{n:g} GiB"
    return f"{n / 1024**3:.1f} GiB"


def _disk(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": d.get("name"),
        "device": d.get("device"),
        "size_gib": _gib(_num(d.get("size"))),
        "status": d.get("status"),
        "temp_c": d.get("temp"),
        "fs_free_gib": _gib(_num(d.get("fsFree"))),
        "fs_used_gib": _gib(_num(d.get("fsUsed"))),
        "is_spinning": d.get("isSpinning"),
        "num_reads": d.get("numReads"),
        "num_writes": d.get("numWrites"),
        "num_errors": d.get("numErrors"),
    }


def _interfaces(nics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for n in nics or []:
        out.append(
            {
                "name": n.get("name"),
                "description": n.get("description"),
                "mac": n.get("macAddress"),
                "operstate": n.get("operstate"),
                "type": n.get("type"),
                "ip": n.get("ipAddress"),
                "netmask": n.get("netmask"),
                "gateway": n.get("gateway"),
            }
        )
    return out


# ---------------------------------------------------------------- response models

# Kept deliberately simple dicts; the OpenAPI spec exposes them as free-form
# objects, which Open WebUI presents to the model verbatim.


# ---------------------------------------------------------------- endpoints


@app.get("/health", tags=["bridge"], summary="Bridge liveness and upstream reachability")
async def health() -> dict[str, Any]:
    upstream_ok = False
    detail = ""
    try:
        await gql("query { systemTime { currentTime } }")
        upstream_ok = True
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
    return {
        "status": "ok",
        "version": BRIDGE_VERSION,
        "upstream_reachable": upstream_ok,
        "upstream_error": detail or None,
        "auth_required": bool(BRIDGE_API_KEY),
    }


@app.get(
    "/system",
    tags=["system"],
    summary="Full system summary (OS, CPU, RAM, Unraid version, uptime)",
)
async def system(_: None = Depends(require_key)) -> dict[str, Any]:
    data = await gql(
        "query { info { os { distro release kernel arch hostname uptime uefi } "
        "cpu { manufacturer brand cores threads speed speedmax socket } "
        "memory { layout { size formFactor type } } "
        "system { manufacturer model virtual } "
        "versions { core { unraid api kernel } packages { docker nginx php node openssl git } } } }"
    )
    info = data.get("info") or {}
    mem_modules = [m for m in (info.get("memory") or {}).get("layout") or [] if _num(m.get("size"))]
    return {
        "os": info.get("os"),
        "cpu": info.get("cpu"),
        "memory_modules": mem_modules,
        "system": info.get("system"),
        "versions": info.get("versions"),
    }


@app.get("/system/cpu", tags=["system"], summary="CPU details and current utilization %")
async def cpu(_: None = Depends(require_key)) -> dict[str, Any]:
    data = await gql(
        "query { info { cpu { manufacturer brand cores threads speed speedmax socket } } "
        "metrics { cpu { percentTotal } } }"
    )
    return {
        "cpu": (data.get("info") or {}).get("cpu"),
        "utilization_percent": ((data.get("metrics") or {}).get("cpu") or {}).get("percentTotal"),
    }


@app.get("/system/memory", tags=["system"], summary="RAM and swap utilization")
async def memory(_: None = Depends(require_key)) -> dict[str, Any]:
    data = await gql(
        "query { metrics { memory { total used free available buffcache percentTotal "
        "swapTotal swapUsed swapFree percentSwapTotal } } }"
    )
    mem = ((data.get("metrics") or {}).get("memory")) or {}
    out = dict(mem)
    for key in ("total", "used", "free", "available", "buffcache", "swapTotal", "swapUsed", "swapFree"):
        out[key] = _gib(mem.get(key))
    return out


@app.get("/system/temps", tags=["system"], summary="Temperature sensors and summary")
async def temps(_: None = Depends(require_key)) -> dict[str, Any]:
    data = await gql(
        "query { metrics { temperature { summary { average warningCount criticalCount "
        "hottest { name current { value } } } sensors { name type location "
        "current { value unit status } warning critical } } } }"
    )
    tm = ((data.get("metrics") or {}).get("temperature")) or {}
    summary = tm.get("summary") or {}
    if isinstance(summary.get("hottest"), dict):
        hottest = summary["hottest"]
        cur = hottest.get("current") or {}
        hottest = {"name": hottest.get("name"), "current_c": cur.get("value")}
    return {
        "summary": summary,
        "sensors": tm.get("sensors") or [],
    }


@app.get("/system/network", tags=["system"], summary="Network interfaces and throughput")
async def network(_: None = Depends(require_key)) -> dict[str, Any]:
    data = await gql(
        "query { networkInterfaces { name description macAddress mtu operstate type "
        "ipAddress netmask gateway useDhcp } "
        "metrics { network { name operstate rxSec txSec utilizationPercent } } }"
    )
    nics = _interfaces(data.get("networkInterfaces") or [])
    rates = {n["name"]: n for n in ((data.get("metrics") or {}).get("network")) or []}
    for nic in nics:
        r = rates.get(nic["name"])
        if r:
            nic["rx_mib_per_sec"] = round((r.get("rxSec") or 0) / 1024**2, 2)
            nic["tx_mib_per_sec"] = round((r.get("txSec") or 0) / 1024**2, 2)
    return {"interfaces": nics}


@app.get("/array", tags=["array"], summary="Array state, capacity, and disk health (incl. cache)")
async def array(_: None = Depends(require_key)) -> dict[str, Any]:
    data = await gql(
        "query { array { state capacity { kilobytes { free used total } disks { free used total } } "
        "parityCheckStatus { status running progress paused errors correcting speed date } "
        "disks { name device size status temp numReads numWrites numErrors fsSize fsFree fsUsed isSpinning type } "
        "caches { name device size status temp numReads numWrites numErrors fsSize fsFree fsUsed isSpinning type } } }"
    )
    arr = data.get("array") or {}
    cap = arr.get("capacity") or {}
    return {
        "state": arr.get("state"),
        "capacity": {
            "data_disks": cap.get("disks") or {},
            "kilobytes": cap.get("kilobytes") or {},
        },
        "parity_check": arr.get("parityCheckStatus"),
        "disks": [_disk(d) for d in arr.get("disks") or []],
        "cache_disks": [_disk(d) for d in arr.get("caches") or []],
    }


@app.get("/docker", tags=["docker"], summary="Docker containers and networks (inventory only)")
async def docker(_: None = Depends(require_key)) -> dict[str, Any]:
    data = await gql(
        "query { docker { containers { id names image state status autoStart isUpdateAvailable "
        "lanIpPorts webUiUrl iconUrl } networks { name driver scope internal } } }"
    )
    d = data.get("docker") or {}
    containers = [
        {
            "id": c.get("id"),
            "name": (c.get("names") or [""])[0].lstrip("/"),
            "image": c.get("image"),
            "state": c.get("state"),
            "status": c.get("status"),
            "auto_start": c.get("autoStart"),
            "update_available": c.get("isUpdateAvailable"),
            "lan_ports": c.get("lanIpPorts"),
            "webui_url": c.get("webUiUrl"),
        }
        for c in d.get("containers") or []
    ]
    return {"containers": containers, "networks": d.get("networks") or []}


@app.get(
    "/docker/container/{container_id}",
    tags=["docker"],
    summary="Detailed view of one container (mounts, labels, restart policy)",
    description="Pass the full container `id` from the /docker listing.",
)
async def docker_container(container_id: str, _: None = Depends(require_key)) -> dict[str, Any]:
    data = await gql(
        'query { docker { container(id: "%s") { id names image command created state status autoStart '
        "isUpdateAvailable lanIpPorts webUiUrl mounts labels "
        "hostConfig { networkMode } } } }" % container_id.replace('"', "")
    )
    c = ((data.get("docker") or {}).get("container")) or {}
    return {
        "id": c.get("id"),
        "name": (c.get("names") or [""])[0].lstrip("/"),
        "image": c.get("image"),
        "state": c.get("state"),
        "status": c.get("status"),
        "auto_start": c.get("autoStart"),
        "update_available": c.get("isUpdateAvailable"),
        "created": c.get("created"),
        "mounts": c.get("mounts"),
        "labels": c.get("labels"),
        "network_mode": ((c.get("hostConfig") or {}).get("networkMode")),
    }


@app.get(
    "/notifications",
    tags=["notifications"],
    summary="Unraid notifications (counts + list)",
    description="box: unread (default) or archive. limit: max items returned.",
)
async def notifications(
    box: Literal["unread", "archive"] = Query("unread"),
    limit: int = Query(25, ge=1, le=100),
    _: None = Depends(require_key),
) -> dict[str, Any]:
    type_enum = "UNREAD" if box == "unread" else "ARCHIVE"
    data = await gql(
        "query { notifications { overview { unread { info warning alert total } "
        "archive { info warning alert total } } "
        f"list(filter: {{type: {type_enum}, offset: 0, limit: {limit}}}) "
        "{ title subject description importance type formattedTimestamp } } }"
    )
    n = data.get("notifications") or {}
    return {"overview": n.get("overview"), "items": n.get("list") or []}


@app.get("/shares", tags=["shares"], summary="User shares with usage")
async def shares(_: None = Depends(require_key)) -> dict[str, Any]:
    data = await gql("query { shares { name free used size cache comment } }")
    out = []
    for s in data.get("shares") or []:
        out.append(
            {
                "name": s.get("name"),
                "free": _gib(s.get("free")),
                "used": _gib(s.get("used")),
                "size": _gib(s.get("size")) or "unlimited",
                "cache": s.get("cache"),
                "comment": s.get("comment"),
            }
        )
    return {"shares": out}


@app.get("/time", tags=["system"], summary="Server time and timezone")
async def time_(_: None = Depends(require_key)) -> dict[str, Any]:
    data = await gql("query { systemTime { currentTime timeZone useNtp ntpServers } }")
    return data.get("systemTime") or {}
