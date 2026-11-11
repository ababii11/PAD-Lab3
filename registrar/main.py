"""Registrar: service discovery for the load balancer.

Runs on the load-balancer host next to the proxy.
  * DW nodes POST /register every few seconds (heartbeat) with their name and URL.
  * Nodes whose heartbeats stop for NODE_TTL seconds are dropped.
  * GET /nodes returns the live nodes. The Python smart proxy reads it every second
    to know where to send traffic; nodes read it to find their sync peers.

Nobody hard-codes the list of servers: a new node only needs the registrar's URL.

Optional (Linux alternative): when HAPROXY_API is set (e.g. 127.0.0.1:9999), the
registrar also adds/removes the nodes in a running HAProxy through its Runtime API.

[RO] PE SCURT: „cartea de telefon” a sistemului (port 9100).
    - fiecare server trimite POST /register la 3 s („sunt node2, la http://127.0.0.1:8002”)
    - sweeper() scoate din listă serverele care n-au mai trimis nimic de 10 s
    - GET /nodes = lista serverelor vii; o citesc proxy-ul (unde trimit cererile?) și
      serverele (de la cine copiez datele?)
Astfel poți adăuga un server nou (`run.py add-node 4`) fără să modifici nimic în cod.
Toată partea cu HAProxy e opțională (doar pe Linux) și pe Windows nu se folosește.
"""
import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

HAPROXY_API = os.environ.get("HAPROXY_API", "")   # e.g. 127.0.0.1:9999; empty = no HAProxy
BACKEND = os.environ.get("HAPROXY_BACKEND", "app")
TTL = float(os.environ.get("NODE_TTL", "10"))          # seconds without heartbeat -> removed
SWEEP_INTERVAL = float(os.environ.get("SWEEP_INTERVAL", "2"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s registrar %(levelname)s %(message)s")
log = logging.getLogger("registrar")

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
nodes: dict[str, dict] = {}            # name -> {url, host, port, weight, last_seen}
lock = asyncio.Lock()


# ---------------- HAProxy Runtime API ----------------
async def haproxy(cmd: str) -> str:
    host, port = HAPROXY_API.rsplit(":", 1)
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 2)
    try:
        writer.write((cmd + "\n").encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 3)   # HAProxy closes after replying
    finally:
        writer.close()
    return data.decode().strip()


async def haproxy_servers() -> dict[str, tuple[str, int]]:
    """Servers currently in the backend: name -> (address, port)."""
    out = await haproxy(f"show servers state {BACKEND}")
    servers = {}
    for line in out.splitlines():
        if not line or line.startswith("#") or line == "1":
            continue
        cols = line.split()
        if len(cols) > 18 and cols[1] == BACKEND:
            servers[cols[3]] = (cols[4], int(cols[18]))
    return servers


async def add_to_haproxy(name: str, node: dict) -> None:
    out = await haproxy(
        f"add server {BACKEND}/{name} {node['host']}:{node['port']} "
        f"weight {node['weight']} check inter 2s fall 2 rise 2")
    if "New server registered" not in out:
        log.warning("add server %s failed: %s", name, out)
        return
    await haproxy(f"enable health {BACKEND}/{name}")
    await haproxy(f"enable server {BACKEND}/{name}")   # dynamic servers start in maintenance
    log.info("HAProxy: added %s -> %s:%s", name, node["host"], node["port"])


async def remove_from_haproxy(name: str) -> None:
    await haproxy(f"set server {BACKEND}/{name} state maint")
    await haproxy(f"shutdown sessions server {BACKEND}/{name}")
    out = await haproxy(f"del server {BACKEND}/{name}")
    if "Server deleted" in out:
        log.info("HAProxy: removed %s", name)
    else:
        log.warning("del server %s: %s (will retry)", name, out)


async def sync_haproxy() -> None:
    """Make HAProxy's backend match the list of live nodes (only if HAProxy is used)."""
    if not HAPROXY_API:
        return
    current = await haproxy_servers()
    for name, node in nodes.items():
        want = (node["host"], node["port"])
        if name in current and current[name] != want:
            await remove_from_haproxy(name)          # node came back on another address
            current.pop(name)
        if name not in current:
            await add_to_haproxy(name, node)
    for name in current.keys() - nodes.keys():
        await remove_from_haproxy(name)


async def sweeper() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL)
        async with lock:
            now = time.time()
            for name in [n for n, v in nodes.items() if now - v["last_seen"] > TTL]:
                log.info("node %s missed its heartbeats: removing", name)
                del nodes[name]
            try:
                await sync_haproxy()
            except Exception as exc:
                log.warning("HAProxy Runtime API not reachable (%s)", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(sweeper())
    yield
    task.cancel()


app = FastAPI(title="Registrar", lifespan=lifespan)


class Registration(BaseModel):
    name: str
    url: str
    weight: int = 100


class Deregistration(BaseModel):
    name: str


def live_nodes() -> list[dict]:
    return [{"name": n, "url": v["url"], "weight": v["weight"],
             "last_seen_s": round(time.time() - v["last_seen"], 1)}
            for n, v in sorted(nodes.items())]


@app.post("/register")
async def register(reg: Registration):
    if not NAME_RE.match(reg.name):
        raise HTTPException(400, "name must be 1-32 letters, digits, '-' or '_'")
    parsed = urlparse(reg.url)
    if parsed.scheme != "http" or not parsed.hostname or not parsed.port:
        raise HTTPException(400, "url must look like http://host:port")
    weight = max(1, min(reg.weight, 256))
    async with lock:
        is_new = reg.name not in nodes
        nodes[reg.name] = {"url": reg.url.rstrip("/"), "host": parsed.hostname,
                           "port": parsed.port, "weight": weight, "last_seen": time.time()}
        if is_new:
            log.info("node %s registered at %s", reg.name, reg.url)
            try:
                await sync_haproxy()     # add it right away instead of at the next sweep
            except Exception as exc:
                log.warning("HAProxy Runtime API not reachable (%s)", exc)
    return {"ok": True, "nodes": live_nodes()}


@app.post("/deregister")
async def deregister(body: Deregistration):
    async with lock:
        if nodes.pop(body.name, None):
            log.info("node %s deregistered", body.name)
            try:
                if HAPROXY_API:
                    await remove_from_haproxy(body.name)
            except Exception as exc:
                log.warning("HAProxy Runtime API not reachable (%s)", exc)
    return {"ok": True}


@app.get("/nodes")
async def get_nodes():
    return {"nodes": live_nodes()}
