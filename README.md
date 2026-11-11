# PAD Lab 2: Smart Proxy (caching, availability, recovery)

A small, resilient web application: a smart proxy spreads requests over several
application servers with **Power of Two Choices**. Each server has **its own MongoDB**,
and the servers keep their databases synchronised. Built in Python with ready-made
libraries. Runs on **Windows** (also Linux/macOS), started from PyCharm or a terminal.
Advanced security is out of scope by design.

- [1. Architecture](#1-architecture)
- [2. Libraries](#2-libraries)
- [3. Setup (Windows + PyCharm)](#3-setup-windows--pycharm)
- [4. Run](#4-run)
- [5. Essential requirements: how each is met](#5-essential-requirements-how-each-is-met)
- [6. Load balancing: P2C and how it relates to HAProxy](#6-load-balancing-p2c-and-how-it-relates-to-haproxy)
- [7. Database synchronisation](#7-database-synchronisation)
- [8. Caching, including during failures and recovery](#8-caching-including-during-failures-and-recovery)
- [9. Testing server and database failures](#9-testing-server-and-database-failures)
- [10. Optional demonstration features](#10-optional-demonstration-features)
- [11. Trade-offs and limitations](#11-trade-offs-and-limitations)
- [12. Reference](#12-reference)

## 1. Architecture

```
              browser: web client (/)  ·  dashboard (/_proxy, optional)
                              │  only knows http://127.0.0.1:8080
                 ┌────────────▼─────────────┐
                 │  smart proxy  :8080      │  P2C load balancing, health checks,
                 │                          │  failover, cache, traffic filtering
                 └──┬──────────┬─────────┬──┘
     node list      │          │         │      ┌───────────────────────┐
     every 1 s  ◀───┼──────────┼─────────┼──────│ registrar :9100       │ ◀── heartbeats
                    │          │         │      │ (service discovery)   │     every 3 s
              ┌─────▼──┐  ┌────▼───┐  ┌──▼─────┐└───────────────────────┘
              │ node1  │  │ node2  │  │ node3  │ ...nodeN   application servers
              │ :8001  │◀─▶ :8002  │◀─▶ :8003  │  pull each other's changes every 2 s
              └───┬────┘  └───┬────┘  └───┬────┘
              mongod1      mongod2     mongod3     one independent database per server
              :27101       :27102      :27103
```

| Component | File | Role |
|---|---|---|
| Smart proxy | `proxy/main.py` | Single entry point. Chooses a server (P2C), health-checks servers, fails over, caches, filters traffic, serves the two HTML pages |
| Registrar | `registrar/main.py` | Service discovery. Servers announce themselves; nothing is hard-coded |
| Application server | `dw_node/` | REST API for movies, its own MongoDB, synchronisation with the other servers |
| Launcher | `run.py` | Starts/stops every process; reads `config.json` |
| Web client | `web/index.html` | The user interface (HTML) |
| Dashboard (optional) | `web/dashboard.html` | Status lights and stop/start buttons per server and database |

All settings are in **`config.json`** (ports, number of servers, algorithm, cache times,
rate limits, sync intervals). Data is exchanged as **JSON**. XML is also available on request
(`Accept: application/xml` or `?format=xml`) because the course handout asks that the client
can choose JSON or XML; JSON stays the default everywhere.

## 2. Libraries

| Library | Used for |
|---|---|
| **FastAPI** (+ Pydantic) | application server REST API and input validation |
| **Starlette** | the proxy's web layer (FastAPI is built on it) |
| **Uvicorn** | ASGI web server for all three Python services |
| **HTTPX** | async HTTP client: proxy → servers, server → server sync, heartbeats |
| **PyMongo** (async API) | MongoDB access, transactions |
| **psutil** | launcher: start/stop/check processes on Windows and Linux |
| **MongoDB 9.0** | one database per server, each a one-member replica set (needed for transactions) |

Our own code is the policy on top: the P2C choice (10 lines), health checks, cache rules and
the synchronisation protocol, each in small functions.

## 3. Setup (Windows + PyCharm)

1. **MongoDB.** Already installed in `C:\Program Files\MongoDB\Server\9.0`; `run.py` finds
   `mongod.exe` there by itself. The Windows "MongoDB" service on port 27017 can keep running:
   this project starts its own MongoDB processes on ports 27101, 27102, ...
   (A different location goes in `config.json` → `"mongod_path"`.)
2. **Open the folder in PyCharm** (`File → Open → Desktop\pad3`) and select a Python 3.10+
   interpreter (`File → Settings → Project → Python Interpreter`).
3. **Install the packages**: open `requirements.txt` and click *Install requirements*, or in
   PyCharm's terminal run `python -m pip install -r requirements.txt`.

## 4. Run

PyCharm shows ready-made run configurations in the drop-down at the top right:

| Run configuration | Same in a terminal | What it does |
|---|---|---|
| **1 Start system** | `python run.py start` | registrar + proxy + 3 servers with their databases (10–20 s) |
| **2 Live status** | `python run.py watch` | coloured server/database status in the terminal |
| **3 Demo test** | `python tests/demo.py` | automatic walk-through of every feature |
| **4 Stop system** | `python run.py stop` | stops all processes, keeps data |
| **5 Stop and delete data** | `python run.py stop --wipe` | stops and deletes databases and logs |

Then open:
- **<http://127.0.0.1:8080/>**: the web client (list, add, edit, delete movies; shows which server answered and whether the cache did)
- **<http://127.0.0.1:8080/_proxy>**: the load-balancer dashboard (optional feature)

More commands: `python run.py start 4` (4 servers), `python run.py start --algo roundrobin`,
`python run.py add-node 5` (add a server while running), `python run.py add-node 6 --weight 200`
(a server that gets twice the traffic), `python run.py stop-node 2`, `python run.py status`.
Logs are in `logs\`, databases in `data\db<N>`.

The servers run in the background, so the *1 Start system* window ends when the system is up.
Stop them with *4 Stop system*, not by closing PyCharm.

## 5. Essential requirements: how each is met

| Requirement | How |
|---|---|
| Python, MongoDB | Python 3.10+, MongoDB 9.0 |
| P2C load balancing | `pick_p2c` in `proxy/main.py`, default in `config.json` (section 6) |
| JSON for data and configuration | REST API in JSON; settings in `config.json`; XML only on request |
| Ready-made libraries | section 2 |
| Small, atomic functions | each function does one thing (e.g. `pick_p2c`, `rate_limited`, `apply_remote`, `pull_from`) |
| HTML client | `web/index.html`, served by the proxy at `/` |
| Several servers, each with its own MongoDB | `run.py start N`: server *i* uses MongoDB on port 27100+*i* |
| Data consistent and synchronised | transactions + change feed + reconciliation + one conflict rule (section 7) |
| Available when any one server or database fails | health checks + failover in the proxy; a server with a broken database reports unhealthy (section 9) |
| Synchronisation restored after recovery | a returning server pulls everything it missed and reconciles (section 7) |
| Caching, and its behaviour during failures | section 8 |

## 6. Load balancing: P2C and how it relates to HAProxy

**Power of Two Choices (P2C):** for each request, pick **two** healthy servers at random and
send the request to the one with **fewer requests in progress** (divided by its weight).

```python
def pick_p2c(cands):
    if len(cands) == 1:
        return cands[0]
    a, b = random.sample(cands, 2)
    return a if a.active / a.weight <= b.active / b.weight else b
```

Why P2C: it does not have to scan every server, it reacts to a slow or overloaded server
(that server keeps more requests in flight, so it loses the comparison), and it avoids the
"herd" effect of always choosing the single least-loaded server, where every request rushes
to the same server at once.

**HAProxy relation.** HAProxy implements exactly this as `balance random(2)` ("draw two
servers, keep the least loaded"). On Linux the project can run behind HAProxy instead of the
Python proxy: `config/haproxy.cfg` uses `balance random(2)` by default, and the registrar adds
servers to HAProxy at runtime through HAProxy's Runtime API. HAProxy has no Windows build, so
on Windows the same algorithm runs in the Python proxy. Both variants use the same servers,
registrar and synchronisation.

Other algorithms for comparison (`--algo` or `config.json`): `roundrobin` (weighted),
`leastconn`, `iphash` (a client always reaches the same server).

**Failover rules in the proxy:**
- health check `GET /health` every 2 s: 2 failures → server DOWN, 2 successes → UP again;
- connection refused → the request goes to another server (safe even for POST, it never arrived);
- a server answers 503 because its database is down → reads go to another server, and so do
  writes the server marks `X-Retry-Safe` (the database was unreachable, so nothing was saved);
- a server accepts a request but does not answer within 15 s → 504, **not** retried, because
  it may already have saved it and a retry could save it twice.

## 7. Database synchronisation

Every record carries `version` (integer, +1 on every change) and `node` (which server made that version).

1. **Local write in one transaction.** The movie and an entry in that server's *change log*
   (with the next sequence number) are saved together or not at all.
2. **Change feed (pull).** Every 2 s each server asks every other server
   `GET /sync/changes?since=<last number I got from you>`, applies the changes and stores how
   far it got, in its own database, so a restart loses nothing. A server logs only its own
   writes, so changes never echo back and forth.
3. **One conflict rule on every server:** the version with the greater `(version, node)` wins.
   A change is applied only if it wins, so applying it twice changes nothing. Clock time is
   never used, because server clocks differ.
4. **Deletes are tombstones** (`deleted: true` with a new version), so an older update cannot
   bring a deleted movie back.
5. **Full reconciliation** every 60 s and whenever a server meets a peer for the first time:
   compare all `(id, version, node)` triples and copy what the peer has newer. This repairs
   what the feed cannot: a server that was offline longer than the change log is kept, a
   database that was replaced, data changed outside the API.
6. **Two users editing the same movie on the same server:** `If-Match: "<version>"` → 409
   Conflict instead of a silent overwrite. The web client uses this when you edit.

**Recovery:** a server that comes back registers again, pulls every change it missed from each
peer, reconciles, and only then reports "in sync". Its health check passes as soon as its own
database answers, so it serves again within seconds.

`GET /sync/checksum` on each server (direct port, e.g. `http://127.0.0.1:8001/sync/checksum`)
returns a hash of all `(id, version, node, deleted)`; equal hashes prove identical databases.

## 8. Caching, including during failures and recovery

The proxy caches JSON answers to `GET /movies…` (key: path + query).

| Situation | Behaviour |
|---|---|
| Normal | a fresh copy is served for **5 s** (`X-Cache-Status: HIT`). Only one request refills an expired entry; the others wait for it, so a popular page cannot overload the servers |
| Any successful POST/PUT/DELETE through the proxy | the whole cache is cleared. For the next **5 s** (`write_settle_s`, about two sync rounds) reads go to the server that made the write and nothing is cached, so users see their own change right away and an older copy from a server that has not pulled it yet can never be cached |
| One server or database down | no visible effect: hits never touch a server, and misses go to a healthy one |
| **All** servers down | a copy up to **60 s** old is served instead of an error (`X-Cache-Status: STALE`). Uncached pages and all writes get 503 |
| Servers back | the next request after the 5 s freshness window fetches new data (`MISS`). Stale copies are never preferred over a working server |
| Bypass | XML requests and `Cache-Control: no-cache` are never cached (the web client's *Edit* uses no-cache to get the latest version) |

Times are in `config.json` (`cache_ttl_s`, `stale_ttl_s`, `write_settle_s`). The cache lives in the
proxy's memory, so it is empty after a proxy restart. If the server that made the write goes down
during those 5 s, reads fall back to the other servers, which may still lack the change for up to
~2 s (see section 11).

## 9. Testing server and database failures

Start with *1 Start system*, open the dashboard (<http://127.0.0.1:8080/_proxy>) and the web
client side by side, and keep *2 Live status* running if you like.

| Test | Do | Expected |
|---|---|---|
| Server fails | **Stop server** on node2 | node2 server light red within ~4 s; the web client keeps working (`server:` shows node1/node3) |
| Writes while it is down | add or edit movies in the web client | saved on the other servers |
| Server recovers | **Start server** on node2 | yellow (reconnecting) → green; database light yellow while it catches up → green; the new movies appear on node2 |
| Database fails | **Stop database** on node3 | node3 database light red, server light yellow (alive, but reports unhealthy, so it gets no traffic); the client keeps working |
| Database recovers | **Start database** on node3 | both lights back to green; node3 gets the changes it missed |
| Everything down | stop all three servers, reload the list in the client | the list still shows (`cache: STALE`) for up to 60 s; adding a movie fails with 503 |
| Conflict | edit the same movie in two browser tabs, save both | the second save gets a conflict message and reloads the movie |
| Automatic check | *3 Demo test* | runs discovery, balancing, sync, cache, XML/JSON, conflicts, delete, filtering |

From a terminal the same failures are `python run.py stop-node 2` (server and database),
`python run.py stop-node 2 --api` (server only) and `python run.py add-node 2` (start again).

## 10. Optional demonstration features

These are extras for demonstrating the system; the essential application works without them.

- **HTML dashboard** at `/_proxy` (reachable only from the same computer): one row per server
  with **Stop/Start server** and **Stop/Start database** buttons and two lights:
  - **Server:** green = running and receiving traffic · red = down (process stopped or unreachable)
    · yellow = reconnecting (process alive but health checks not passing yet, or its database is down).
  - **Database:** green = connected and copied everything its reachable peers had at the last pull
    · red = disconnected · yellow = out of sync or synchronizing (catching up, or the database runs
    while its server is down, so it cannot sync).

  It also shows requests/errors per server, cache statistics and an event log. The buttons
  really stop and start the processes (through `run.py`), so recovery is real, not simulated.
- **Terminal status:** `python run.py watch` (live, coloured) or `python run.py status` (once).
- `client/cli.py`: a small command-line client, kept for scripting only. The HTML client is the main interface.

## 11. Trade-offs and limitations

- **Eventual consistency, not instant.** Each server writes locally and the others copy the change
  within about 2 s. During that window a read that lands on another server can return the older
  version. This is the price of every server staying writable and available on its own. Mitigation:
  `--algo iphash` keeps a client on one server.
- **Simultaneous edits on two different servers:** both are accepted locally; afterwards every
  server keeps the same winner (higher version, then higher server name) and the other edit is
  lost. `If-Match` prevents this only when both edits reach the same server.
- **A write that times out** (server accepted it but did not answer in 15 s) gets 504 and is not
  retried; it may or may not have been saved.
- **Stale cache** can be up to 60 s old when every server is down, by design (better than an error page).
- **Retention:** change-log entries are kept 24 h and tombstones 7 days (`dw_node/settings.py`).
  A server offline longer than 7 days should be wiped (`stop-node`, delete `data\db<N>`, `add-node`).
- **Single points of failure:** the proxy (one instance) and, for new registrations only, the
  registrar. If the registrar is down, the proxy and servers keep using the list they already have.
- Everything runs on one computer here (ports instead of machines); moving a server to another
  machine only needs its `ADVERTISE_URL`/`MONGO_URL` changed.
- No TLS or authentication (out of scope).

## 12. Reference

**API through the proxy** (`:8080`):

| Method | Path | Notes |
|---|---|---|
| GET | `/movies?offset=0&limit=20` | list, sorted by title |
| GET | `/movies/{id}` | `ETag: "<version>"` |
| POST | `/movies` | `{"title", "year", "genre", "actors": [], "rating"}` → 201 |
| PUT | `/movies/{id}` | optional `If-Match: "<version>"` → 409 if changed meanwhile |
| DELETE | `/movies/{id}` | optional `If-Match` → 204 |

Response headers: `X-Node` / `X-Upstream` (server that answered), `X-Cache-Status`, `X-Request-ID`.

**Traffic filtering** (in the proxy): more than 100 requests per 10 s or 20 at once from one IP → 429;
internal paths (`/sync`, `/info`, `/health`, `/docs`) → 403; scanner user agents → 403;
other HTTP methods → 405; bodies over 64 KB → 413.

**Internal server endpoints** (direct ports only): `/health`, `/sync/changes`, `/sync/digest`,
`/sync/docs`, `/sync/checksum`, `/sync/status`, `/info`, `/docs` (Swagger UI).

**Project layout**
```
config.json            all settings
run.py                 launcher (start / stop / add-node / stop-node / status / watch)
proxy/main.py          smart proxy + dashboard API
registrar/main.py      service discovery
dw_node/main.py        server REST API
dw_node/store.py       MongoDB: transactions, change log, conflict rule
dw_node/sync.py        heartbeat, change-feed pulling, reconciliation, clean-up
dw_node/formats.py     JSON / XML output
dw_node/settings.py    server settings (set by run.py from config.json)
web/index.html         HTML client
web/dashboard.html     dashboard (optional)
tests/demo.py          automatic demonstration
client/cli.py          command-line client (secondary)
.run/                  PyCharm run configurations
config/haproxy.cfg, scripts/*.sh   optional HAProxy variant for Linux/WSL
```
