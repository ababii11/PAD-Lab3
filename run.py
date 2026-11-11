"""Launcher: start, stop and watch the whole system (Windows, Linux, macOS).

    python run.py start              registrar + proxy + nodes (count from config.json)
    python run.py start 4 --algo leastconn
    python run.py add-node 5 [--weight 200]   add a node while running
    python run.py stop-node 2 [--api]         stop node2 (and its MongoDB unless --api)
    python run.py status                      one-time status
    python run.py watch                       live, coloured status in the terminal
    python run.py stop [--wipe]               stop everything (--wipe deletes data and logs)

All settings are in config.json. In PyCharm use the ready-made run configurations.

[RO] PE SCURT: acesta NU face parte din sistemul distribuit; e doar „telecomanda” care
pornește procesele în ordinea corectă:
    1. registrar  (9100)       -> trebuie să existe primul, ca să se poată înregistra ceilalți
    2. proxy      (8080)
    3. pentru i = 1..N:  mongod_i (27100+i), apoi node_i (8000+i)
Fiecare proces rulează separat, în fundal. PID-ul lui se salvează în run/<nume>.json, ca
`stop` să știe ce să oprească, iar log-ul în logs/<nume>.log. Setările ajung la procese prin
variabile de mediu (vezi start_proxy / start_node_api), citite din config.json.
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    import psutil
except ImportError:
    sys.exit("Missing packages. Run first:  python -m pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent
RUN, LOGS, DATA = ROOT / "run", ROOT / "logs", ROOT / "data"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
PY = sys.executable
WINDOWS = os.name == "nt"
ALGORITHMS = ["p2c", "roundrobin", "leastconn", "iphash"]

PROXY_PORT = CONFIG["proxy"]["port"]
REGISTRAR_URL = f"http://127.0.0.1:{CONFIG['registrar']['port']}"
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"


# ------------------------------ ports and paths ------------------------------
def node_port(i: int) -> int:
    return CONFIG["nodes"]["first_api_port"] + i - 1


def mongo_port(i: int) -> int:
    return CONFIG["nodes"]["first_mongo_port"] + i - 1


def find_mongod() -> str:
    explicit = os.environ.get("MONGOD") or CONFIG.get("mongod_path")
    if explicit:
        return explicit
    found = shutil.which("mongod")
    if found:
        return found
    if WINDOWS:   # standard install location, newest version first
        def version(p: str) -> list[int]:
            return [int(x) for x in Path(p).parts[-3].split(".") if x.isdigit()]
        found_all = glob.glob(r"C:\Program Files\MongoDB\Server\*\bin\mongod.exe")
        if found_all:
            return max(found_all, key=version)
    sys.exit('mongod not found: install MongoDB Community Server or set "mongod_path" '
             "in config.json")


# ------------------------------ processes ------------------------------
def read_meta(name: str) -> dict | None:
    f = RUN / f"{name}.json"
    return json.loads(f.read_text()) if f.exists() else None


def read_pid(name: str) -> psutil.Process | None:
    """The running process for `name`, or None (checks pid AND start time)."""
    info = read_meta(name)
    if not info:
        return None
    try:
        p = psutil.Process(info["pid"])
        if (abs(p.create_time() - info["created"]) < 1 and p.is_running()
                and p.status() != psutil.STATUS_ZOMBIE):   # zombie = already exited (Linux)
            return p
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return None


def is_running(name: str) -> bool:
    return read_pid(name) is not None


def _popen(args: list[str], log, env: dict) -> subprocess.Popen:
    common = dict(cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                  env={**os.environ, **env})
    if not WINDOWS:
        return subprocess.Popen(args, start_new_session=True, **common)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:   # leave PyCharm's job object, so stopping the run window doesn't kill the servers
        return subprocess.Popen(args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                                **common)
    except OSError:
        return subprocess.Popen(args, creationflags=flags, **common)


def spawn(name: str, args: list[str], env: dict | None = None, **meta) -> None:
    if is_running(name):
        print(f"  {name} already running")
        return
    RUN.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    with open(LOGS / f"{name}.log", "ab") as log:
        p = _popen(args, log, env or {})
    created = psutil.Process(p.pid).create_time()
    (RUN / f"{name}.json").write_text(json.dumps({"pid": p.pid, "created": created, **meta}))
    print(f"  started {name} (pid {p.pid}, log: logs/{name}.log)")


def _shutdown_mongod(name: str) -> None:
    """Ask mongod to shut down cleanly (keeps the database files consistent)."""
    port = mongo_port(int(name.removeprefix("mongod")))
    try:
        from pymongo import MongoClient
        MongoClient(f"mongodb://127.0.0.1:{port}/?directConnection=true",
                    serverSelectionTimeoutMS=2000).admin.command("shutdown")
    except Exception:
        pass   # the connection always drops when the server shuts down


def stop(name: str) -> None:
    p = read_pid(name)
    if p is None:
        return
    if name.startswith("mongod"):
        _shutdown_mongod(name)
        try:
            p.wait(15)
        except psutil.TimeoutExpired:
            pass
    if p.is_running():
        p.terminate()
        try:
            p.wait(10)
        except psutil.TimeoutExpired:
            p.kill()
    print(f"  stopped {name}")


def names(prefix: str) -> list[str]:
    if not RUN.exists():
        return []
    return sorted((f.stem for f in RUN.glob(f"{prefix}*.json")),
                  key=lambda n: (n.rstrip("0123456789"), int("0" + n[len(n.rstrip("0123456789")):])))


# ------------------------------ HTTP helpers ------------------------------
def wait_http(url: str, seconds: float = 60) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:
            time.sleep(0.3)
    return False


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.loads(r.read())


# ------------------------------ start pieces ------------------------------
def start_registrar() -> None:
    spawn("registrar", [PY, "-m", "uvicorn", "registrar.main:app", "--host", "127.0.0.1",
                        "--port", str(CONFIG["registrar"]["port"]), "--no-access-log"],
          {"NODE_TTL": str(CONFIG["registrar"]["node_ttl_s"])})
    if not wait_http(f"{REGISTRAR_URL}/nodes", 30):
        sys.exit("registrar did not start, see logs/registrar.log")


def start_proxy(algo: str) -> None:
    p = CONFIG["proxy"]
    spawn("proxy", [PY, "-m", "uvicorn", "proxy.main:app", "--host", "127.0.0.1",
                    "--port", str(p["port"]), "--no-access-log"],
          {"LB_ALGO": algo, "REGISTRAR_URL": REGISTRAR_URL,
           "CACHE_TTL": str(p["cache_ttl_s"]), "STALE_TTL": str(p["stale_ttl_s"]),
           "WRITE_SETTLE": str(p.get("write_settle_s", 5)),
           "RATE_LIMIT": str(p["rate_limit_requests"]),
           "RATE_WINDOW": str(p["rate_limit_window_s"]),
           "MAX_CONN_PER_IP": str(p["max_connections_per_ip"])})
    if not wait_http(f"{PROXY_URL}/_proxy/stats", 30):
        sys.exit("proxy did not start, see logs/proxy.log")


def start_mongod(i: int) -> None:
    dbpath = DATA / f"db{i}"
    dbpath.mkdir(parents=True, exist_ok=True)
    exe = (read_meta(f"mongod{i}") or {}).get("exe") or find_mongod()   # same binary as before
    # --replSet: a ONE-member replica set, only so that transactions work.
    # Every node still has its own, independent database.
    spawn(f"mongod{i}", [exe, "--replSet", f"rs-node{i}", "--port", str(mongo_port(i)),
                         "--bind_ip", "127.0.0.1", "--dbpath", str(dbpath),
                         "--wiredTigerCacheSizeGB", "0.25"], exe=exe)


def start_node_api(i: int, weight: int | None = None) -> None:
    if weight is None:   # restarting: keep the weight it had before
        weight = (read_meta(f"node{i}") or {}).get("weight", 100)
    n, port = CONFIG["nodes"], node_port(i)
    spawn(f"node{i}", [PY, "-m", "uvicorn", "dw_node.main:app", "--host", "127.0.0.1",
                       "--port", str(port), "--no-access-log"],
          {"NODE_NAME": f"node{i}", "PORT": str(port), "WEIGHT": str(weight),
           "MONGO_URL": f"mongodb://127.0.0.1:{mongo_port(i)}/?directConnection=true",
           "ADVERTISE_URL": f"http://127.0.0.1:{port}", "REGISTRAR_URL": REGISTRAR_URL,
           "HEARTBEAT_INTERVAL": str(n["heartbeat_interval_s"]),
           "PULL_INTERVAL": str(n["pull_interval_s"]),
           "RECONCILE_INTERVAL": str(n["reconcile_interval_s"])},
          weight=weight)


def add_node(i: int, weight: int | None = None) -> None:
    start_mongod(i)
    start_node_api(i, weight)
    if not wait_http(f"http://127.0.0.1:{node_port(i)}/health", 90):
        sys.exit(f"node{i} did not start, see logs/node{i}.log and logs/mongod{i}.log")
    print(f"  node{i} ready on http://127.0.0.1:{node_port(i)}")


# ------------------------------ status in the terminal ------------------------------
COLORS = {"green": "\033[92m", "yellow": "\033[93m", "red": "\033[91m"}


def dot(color: str) -> str:
    return f"{COLORS.get(color, '')}●\033[0m"


def print_status() -> None:
    try:
        s = get_json(f"{PROXY_URL}/_proxy/stats")
    except Exception:
        print("proxy not reachable - is the system started?  (python run.py start)")
        return
    print(f"Proxy {PROXY_URL}   algorithm: {s['algorithm']}   dashboard: {PROXY_URL}/_proxy")
    print(f"{'node':<7} {'server':<24} {'database':<30} requests")
    for n in s["nodes"]:
        print(f"{n['name']:<7} {dot(n['server'])} {n['server_text']:<22} "
              f"{dot(n['db'])} {n['db_text']:<28} {n['requests']}")
    c = s["cache"]
    print(f"cache: {c['entries']} entries, {c['hits']} hits, {c['misses']} misses, "
          f"{c['stale']} stale")


def watch() -> None:
    if WINDOWS:
        os.system("")   # enables colour codes in the Windows console
    try:
        while True:
            print("\033[2J\033[H", end="")
            print_status()
            print("\n(refreshes every 2 s, Ctrl+C to quit)")
            time.sleep(2)
    except KeyboardInterrupt:
        pass


# ------------------------------ commands ------------------------------
def cmd_start(count: int, algo: str) -> None:
    start_registrar()
    start_proxy(algo)
    for i in range(1, count + 1):
        add_node(i)
    print(f"\nReady.\n  Web client:  {PROXY_URL}/\n  Dashboard:   {PROXY_URL}/_proxy"
          f"\n  Live status: python run.py watch")


def cmd_stop(wipe: bool) -> None:
    for n in names("node") + ["proxy", "registrar"] + names("mongod"):
        stop(n)
    shutil.rmtree(RUN, ignore_errors=True)
    if wipe:
        shutil.rmtree(DATA, ignore_errors=True)
        shutil.rmtree(LOGS, ignore_errors=True)
        print("  deleted data/ and logs/")


def main() -> None:
    # The status dots (●) are not in legacy code pages such as cp1251, which Python uses
    # when output is piped or redirected on Windows: always write UTF-8 instead of crashing.
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Smart Proxy lab launcher")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("nodes", nargs="?", type=int, default=CONFIG["nodes"]["count"])
    s.add_argument("--algo", default=CONFIG["proxy"]["algorithm"], choices=ALGORITHMS)
    s = sub.add_parser("add-node"); s.add_argument("i", type=int)
    s.add_argument("--weight", type=int, default=None)
    s = sub.add_parser("stop-node"); s.add_argument("i", type=int)
    s.add_argument("--api", action="store_true", help="stop only the API, keep MongoDB")
    s = sub.add_parser("stop"); s.add_argument("--wipe", action="store_true")
    sub.add_parser("status")
    sub.add_parser("watch")
    a = ap.parse_args()

    if a.cmd == "start":
        cmd_start(a.nodes, a.algo)
    elif a.cmd == "add-node":
        add_node(a.i, a.weight)
    elif a.cmd == "stop-node":
        stop(f"node{a.i}")
        if not a.api:
            stop(f"mongod{a.i}")
    elif a.cmd == "stop":
        cmd_stop(a.wipe)
    elif a.cmd == "status":
        if WINDOWS:
            os.system("")
        print_status()
    elif a.cmd == "watch":
        watch()


if __name__ == "__main__":
    main()
