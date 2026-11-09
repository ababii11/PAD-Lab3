"""Smart proxy: the only address clients know (http://127.0.0.1:8080).

Ready-made libraries: Starlette (web framework, part of FastAPI), Uvicorn (server),
HTTPX (async HTTP client with connection pooling). Our own code is only the policy:

  * load balancing  P2C "power of two choices" (default) | roundrobin | leastconn | iphash
  * health checks   every 2 s; 2 failures -> DOWN, 2 successes -> UP again
  * failover        connection refused -> next node; GET answered 502/503 -> next node
  * caching         JSON GET /movies kept CACHE_TTL s; served STALE if no node can answer
  * filtering       rate limit, connection limit, blocked paths / agents / methods / sizes
  * discovery       node list from the registrar, refreshed every second
  * web pages       /          HTML client (web/index.html)
                    /_proxy    dashboard with stop/start buttons (only from this machine)
"""
import asyncio
import hashlib
import logging
import os
import random
import re
import sys
import time
import uuid
from collections import Counter, defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import run as launcher  # noqa: E402  (process control for the dashboard buttons)

LB_ALGO = os.environ.get("LB_ALGO", "p2c")
REGISTRAR_URL = os.environ.get("REGISTRAR_URL", "http://127.0.0.1:9100")
CACHE_TTL = float(os.environ.get("CACHE_TTL", "5"))
STALE_TTL = float(os.environ.get("STALE_TTL", "60"))
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "100"))
RATE_WINDOW = float(os.environ.get("RATE_WINDOW", "10"))
MAX_CONN_PER_IP = int(os.environ.get("MAX_CONN_PER_IP", "20"))
MAX_BODY = 64 * 1024
HEALTH_INTERVAL, FALL, RISE = 2.0, 2, 2

BLOCKED_PATHS = ("/sync", "/info", "/docs", "/openapi.json", "/redoc", "/health")
BLOCKED_AGENTS = ("sqlmap", "nikto", "masscan")
ALLOWED_METHODS = {"GET", "HEAD", "POST", "PUT", "DELETE"}
SAFE_METHODS = {"GET", "HEAD"}
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
              "host", "content-length", "content-encoding"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s proxy %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("proxy")
events: deque = deque(maxlen=40)        # shown on the dashboard
counters: Counter = Counter()


def event(text: str) -> None:
    events.appendleft((time.strftime("%H:%M:%S"), text))
    log.info(text)


# ============================ nodes ============================
class Backend:
    def __init__(self, name: str, url: str, weight: int) -> None:
        self.name, self.url, self.weight = name, url, max(1, weight)
        self.up = False
        self.fails = self.oks = 0
        self.active = self.total = self.errors = 0
        self.current_weight = 0          # smooth weighted round robin state
        self.status: dict = {}           # last /sync/status answer (for the dashboard)

    def record_check(self, ok: bool) -> None:
        if ok:
            self.oks, self.fails = self.oks + 1, 0
            if not self.up and self.oks >= RISE:
                self.up = True
                event(f"{self.name} is UP")
        else:
            self.fails, self.oks = self.fails + 1, 0
            if self.up and self.fails >= FALL:
                self.up = False
                event(f"{self.name} is DOWN")


backends: dict[str, Backend] = {}       # nodes the registrar currently lists
seen_urls: dict[str, str] = {}          # every node ever seen (dashboard keeps showing it)


# ============================ load balancing ============================
def pick_p2c(cands: list[Backend]) -> Backend:
    """Power of two choices: two random nodes, take the less busy one.

    Same idea as HAProxy's `balance random(2)`. Cheap (no global scan), and it avoids
    both the herd effect of always-pick-the-least-loaded and the blindness of random.
    """
    if len(cands) == 1:
        return cands[0]
    a, b = random.sample(cands, 2)
    return a if a.active / a.weight <= b.active / b.weight else b


def pick_leastconn(cands: list[Backend]) -> Backend:
    low = min(b.active / b.weight for b in cands)
    return random.choice([b for b in cands if b.active / b.weight == low])


def pick_iphash(cands: list[Backend], client_ip: str) -> Backend:
    """Rendezvous hashing: one client always reaches the same node."""
    return max(cands, key=lambda b: hashlib.sha1(f"{client_ip}|{b.name}".encode()).digest())


def pick_roundrobin(cands: list[Backend]) -> Backend:
    """Smooth weighted round robin (a weight-200 node gets twice the requests)."""
    total = sum(b.weight for b in cands)
    for b in cands:
        b.current_weight += b.weight
    best = max(cands, key=lambda b: b.current_weight)
    best.current_weight -= total
    return best


def choose(client_ip: str, exclude: set[str]) -> Backend | None:
    cands = [b for b in backends.values() if b.up and b.name not in exclude]
    if not cands:
        return None
    if LB_ALGO == "leastconn":
        return pick_leastconn(cands)
    if LB_ALGO == "iphash":
        return pick_iphash(cands, client_ip)
    if LB_ALGO == "roundrobin":
        return pick_roundrobin(cands)
    return pick_p2c(cands)


# ============================ discovery and health ============================
http: httpx.AsyncClient


async def refresh_nodes() -> None:
    """Take the live node list from the registrar."""
    try:
        r = await http.get(f"{REGISTRAR_URL}/nodes", timeout=2)
        listed = {n["name"]: n for n in r.json()["nodes"]}
    except Exception:
        return   # registrar down: keep the current list
    for name, n in listed.items():
        await add_or_update(name, n["url"], n.get("weight", 100))
    for name in list(backends.keys() - listed.keys()):
        del backends[name]
        event(f"{name} left the registry")


async def add_or_update(name: str, url: str, weight: int) -> None:
    b = backends.get(name)
    if b is not None and b.url == url:
        b.weight = max(1, weight)
        return
    backends[name] = b = Backend(name, url, weight)
    seen_urls[name] = url
    event(f"{name} registered at {url}")
    for _ in range(RISE):     # check right away instead of waiting for the next round
        await health_check(b)


async def health_check(b: Backend) -> None:
    try:
        r = await http.get(f"{b.url}/health", timeout=1.5)
        b.record_check(r.status_code == 200)
    except httpx.HTTPError:
        b.record_check(False)


async def fetch_status(b: Backend) -> None:
    try:
        r = await http.get(f"{b.url}/sync/status", timeout=1.5)
        b.status = r.json()
    except (httpx.HTTPError, ValueError):
        b.status = {}


async def check_all() -> None:
    nodes = list(backends.values())
    await asyncio.gather(*(health_check(b) for b in nodes), *(fetch_status(b) for b in nodes))


async def repeat(fn, interval: float) -> None:
    while True:
        try:
            await fn()
        except Exception as exc:
            log.warning("%s failed: %s", fn.__name__, exc)
        await asyncio.sleep(interval)


# ============================ cache ============================
class Cache:
    """key -> (stored_at, status, headers, body). Fresh for CACHE_TTL s, usable as a
    last resort ("stale") for STALE_TTL s when no node can answer."""

    def __init__(self) -> None:
        self.entries: dict[str, tuple] = {}
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.hits = self.misses = self.stale = 0

    def fresh(self, key: str):
        e = self.entries.get(key)
        return e if e and time.monotonic() - e[0] < CACHE_TTL else None

    def stale_copy(self, key: str):
        e = self.entries.get(key)
        return e if e and time.monotonic() - e[0] < CACHE_TTL + STALE_TTL else None

    def put(self, key: str, resp: Response) -> None:
        self.entries[key] = (time.monotonic(), resp.status_code,
                             list(resp.headers.items()), resp.body)

    def clear(self) -> None:
        self.entries.clear()


cache = Cache()


def is_cacheable(request: Request) -> bool:
    return (request.method == "GET" and request.url.path.startswith("/movies")
            and "xml" not in request.headers.get("accept", "").lower()
            and request.query_params.get("format", "") != "xml"
            and "no-cache" not in request.headers.get("cache-control", "").lower())


def cache_key(request: Request) -> str:
    return f"{request.url.path}?{request.url.query}"


def from_cache(entry: tuple, label: str) -> Response:
    _, status, headers, body = entry
    resp = Response(body, status_code=status, headers=dict(headers))
    resp.headers["X-Cache-Status"] = label
    return resp


# ============================ traffic filtering ============================
recent: dict[str, deque] = defaultdict(deque)     # ip -> times of recent requests
open_conns: Counter = Counter()                   # ip -> requests in progress


def deny(status: int, reason: str) -> Response:
    counters[f"denied {status}"] += 1
    return JSONResponse({"error": reason}, status_code=status)


def rate_limited(ip: str) -> bool:
    now, times = time.monotonic(), recent[ip]
    while times and times[0] < now - RATE_WINDOW:
        times.popleft()
    if len(times) >= RATE_LIMIT:
        return True
    times.append(now)
    return False


def filter_request(request: Request, ip: str) -> Response | None:
    if request.method not in ALLOWED_METHODS:
        return deny(405, "method not allowed")
    if request.url.path.startswith(BLOCKED_PATHS):
        return deny(403, "forbidden")
    if any(a in request.headers.get("user-agent", "").lower() for a in BLOCKED_AGENTS):
        return deny(403, "forbidden")
    if int(request.headers.get("content-length", "0") or 0) > MAX_BODY:
        return deny(413, "body too large")
    if rate_limited(ip):
        return deny(429, f"rate limit: {RATE_LIMIT} requests per {RATE_WINDOW:g} s")
    if open_conns[ip] >= MAX_CONN_PER_IP:
        return deny(429, f"max {MAX_CONN_PER_IP} simultaneous requests")
    return None


# ============================ forwarding ============================
def upstream_headers(request: Request, ip: str, rid: str) -> dict:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    headers.update({"X-Forwarded-For": ip, "X-Request-ID": rid})
    return headers


def to_response(r: httpx.Response, b: Backend) -> Response:
    headers = {k: v for k, v in r.headers.items() if k.lower() not in HOP_BY_HOP}
    headers["X-Upstream"] = b.name
    return Response(r.content, status_code=r.status_code, headers=headers)


async def send_once(b: Backend, request: Request, body: bytes, headers: dict) -> httpx.Response:
    b.active += 1
    b.total += 1
    try:
        return await http.request(request.method, b.url + request.url.path,
                                  params=request.query_params, content=body, headers=headers)
    finally:
        b.active -= 1


def retry_elsewhere(request: Request, r: httpx.Response) -> bool:
    """A read that failed on the node, or any request the node says it did not apply."""
    if r.status_code not in (502, 503):
        return False
    return request.method in SAFE_METHODS or r.headers.get("x-retry-safe") == "1"


async def forward(request: Request, body: bytes, ip: str, rid: str) -> Response:
    """Send to a node chosen by the algorithm; fail over when it is safe to do so."""
    headers, tried = upstream_headers(request, ip, rid), set()
    while (b := choose(ip, tried)) is not None:
        tried.add(b.name)
        try:
            r = await send_once(b, request, body, headers)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # never reached the node: safe to retry elsewhere, even a POST
            b.errors += 1
            b.record_check(False)
            continue
        except httpx.TimeoutException:
            # the node may have done the work: retrying could duplicate a write
            b.errors += 1
            return deny(504, f"{b.name} did not answer in time")
        except httpx.HTTPError as exc:
            b.errors += 1
            return deny(502, f"bad answer from {b.name}: {exc}")
        if retry_elsewhere(request, r):
            b.errors += 1        # e.g. the node's database is down: send it to another node
            continue
        return to_response(r, b)
    return deny(503, "no healthy node available")


async def cached_get(request: Request, body: bytes, ip: str, rid: str) -> Response:
    key = cache_key(request)
    if (entry := cache.fresh(key)) is not None:
        cache.hits += 1
        return from_cache(entry, "HIT")
    async with cache.locks[key]:                   # one request refills, the others wait
        if (entry := cache.fresh(key)) is not None:
            cache.hits += 1
            return from_cache(entry, "HIT")
        resp = await forward(request, body, ip, rid)
        if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
            cache.put(key, resp)
        elif resp.status_code >= 500 and (old := cache.stale_copy(key)) is not None:
            cache.stale += 1
            event(f"no node could answer {key}: served a stale cached copy")
            return from_cache(old, "STALE")
        cache.misses += 1
        resp.headers["X-Cache-Status"] = "MISS"
        return resp


async def proxy(request: Request) -> Response:
    ip = request.client.host if request.client else "?"
    rid = uuid.uuid4().hex[:16]
    counters["requests"] += 1
    resp = filter_request(request, ip)
    if resp is None:
        open_conns[ip] += 1
        try:
            body = await request.body()
            if is_cacheable(request):
                resp = await cached_get(request, body, ip, rid)
            else:
                resp = await forward(request, body, ip, rid)
                if request.method not in SAFE_METHODS and resp.status_code < 300:
                    cache.clear()              # lists and items may have changed
        finally:
            open_conns[ip] -= 1
    resp.headers["X-Request-ID"] = rid
    return resp


# ============================ dashboard ============================
def local_only(request: Request) -> bool:
    return request.client is not None and request.client.host in ("127.0.0.1", "::1")


def server_light(name: str) -> tuple[str, str]:
    b = backends.get(name)
    if b and b.up:
        return "green", "running"
    if launcher.is_running(name) or (b and b.status):
        return "yellow", "reconnecting"
    return "red", "down"


def db_light(name: str) -> tuple[str, str]:
    i = int(name.removeprefix("node"))
    status = backends[name].status if name in backends else {}
    if status:
        if not status.get("db_ok"):
            return "red", "disconnected"
        if not status.get("in_sync"):
            return "yellow", "synchronizing"
        return "green", "connected, in sync"
    if launcher.is_running(f"mongod{i}"):
        return "yellow", "running, sync paused (server down)"
    return "red", "disconnected"


def known_nodes() -> list[str]:
    names = set(seen_urls) | set(launcher.names("node"))
    return sorted(names, key=lambda n: int(re.sub(r"\D", "", n) or 0))


def node_row(name: str) -> dict:
    i = int(name.removeprefix("node"))
    b = backends.get(name)
    server, server_text = server_light(name)
    db, db_text = db_light(name)
    return {"name": name, "url": seen_urls.get(name, f"http://127.0.0.1:{launcher.node_port(i)}"),
            "server": server, "server_text": server_text, "db": db, "db_text": db_text,
            "server_process": launcher.is_running(name),
            "db_process": launcher.is_running(f"mongod{i}"),
            "weight": b.weight if b else None, "active": b.active if b else 0,
            "requests": b.total if b else 0, "errors": b.errors if b else 0}


def stats_data() -> dict:
    return {"algorithm": LB_ALGO,
            "nodes": [node_row(n) for n in known_nodes()],
            "cache": {"entries": len(cache.entries), "hits": cache.hits, "misses": cache.misses,
                      "stale": cache.stale, "ttl_s": CACHE_TTL, "stale_ttl_s": STALE_TTL},
            "counters": dict(counters),
            "events": [f"{t}  {text}" for t, text in events]}


async def stats(request: Request) -> Response:
    return JSONResponse(stats_data()) if local_only(request) else deny(403, "forbidden")


async def dashboard(request: Request) -> Response:
    if not local_only(request):
        return deny(403, "forbidden")
    return FileResponse(ROOT / "web" / "dashboard.html")


async def client_page(request: Request) -> Response:
    return FileResponse(ROOT / "web" / "index.html")


CONTROL = {
    ("server", "stop"): lambda i: launcher.stop(f"node{i}"),
    ("server", "start"): lambda i: launcher.start_node_api(i),
    ("db", "stop"): lambda i: launcher.stop(f"mongod{i}"),
    ("db", "start"): lambda i: launcher.start_mongod(i),
}


async def control(request: Request) -> Response:
    """POST {"node": "node2", "part": "server"|"db", "action": "stop"|"start"}"""
    if not local_only(request):
        return deny(403, "forbidden")
    cmd = await request.json()
    match = re.fullmatch(r"node(\d+)", str(cmd.get("node", "")))
    action = CONTROL.get((cmd.get("part"), cmd.get("action")))
    if not match or action is None:
        return deny(400, "expected node, part (server|db) and action (stop|start)")
    try:
        await asyncio.to_thread(action, int(match.group(1)))
    except BaseException as exc:          # launcher errors use sys.exit -> SystemExit
        event(f"dashboard: {cmd['action']} {cmd['part']} of {cmd['node']} FAILED: {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    event(f"dashboard: {cmd['action']} {cmd['part']} of {cmd['node']}")
    return JSONResponse({"ok": True})


# ============================ app ============================
@asynccontextmanager
async def lifespan(app):
    global http
    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=2.0),
                             limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))
    tasks = [asyncio.create_task(repeat(refresh_nodes, 1.0)),
             asyncio.create_task(repeat(check_all, HEALTH_INTERVAL))]
    event(f"smart proxy started, algorithm={LB_ALGO}")
    yield
    for t in tasks:
        t.cancel()
    await http.aclose()


app = Starlette(lifespan=lifespan, routes=[
    Route("/", client_page),
    Route("/_proxy", dashboard),
    Route("/_proxy/stats", stats),
    Route("/_proxy/control", control, methods=["POST"]),
    Route("/{path:path}", proxy, methods=list(ALLOWED_METHODS | {"PATCH", "OPTIONS"})),
])
