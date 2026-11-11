"""End-to-end demo of the lab, run against a started system (scripts/start-all.sh).

    python tests/demo.py

The demo talks to the proxy like a normal client. To PROVE synchronisation it
also asks the registrar for the node addresses and compares the nodes directly
(a normal client never does that).
"""
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

PROXY = "http://127.0.0.1:8080"
REGISTRAR = "http://127.0.0.1:9100"
NO_CACHE = {"Cache-Control": "no-cache"}


def title(text: str) -> None:
    print(f"\n=== {text} " + "=" * max(0, 60 - len(text)))


def nodes() -> dict[str, str]:
    return {n["name"]: n["url"] for n in httpx.get(f"{REGISTRAR}/nodes").json()["nodes"]}


def checksums() -> dict[str, str]:
    return {name: httpx.get(f"{url}/sync/checksum").json()["hash"] for name, url in nodes().items()}


def wait_in_sync(timeout: float = 15) -> float:
    start = time.time()
    while time.time() - start < timeout:
        if len(set(checksums().values())) == 1:
            return time.time() - start
        time.sleep(0.2)
    raise SystemExit(f"nodes did not converge: {checksums()}")


def main() -> None:
    http = httpx.Client(base_url=PROXY, timeout=10)

    title("1. Service discovery")
    found = nodes()
    for name, url in found.items():
        print(f"  {name} at {url}")
    if not found:
        raise SystemExit("no nodes registered - run scripts/start-all.sh first")

    title("2. Load balancing, P2C by default (cache bypassed)")
    seen = [http.get("/movies", headers=NO_CACHE).headers["x-node"] for _ in range(2 * len(found))]
    print("  requests answered by:", " ".join(seen))

    title("3. Create through the proxy, then watch it reach every node")
    r = http.post("/movies", json={"title": "Demo movie", "year": 2026, "genre": "demo",
                                   "actors": ["A. Actor", "B. Actor"], "rating": 7.5})
    movie = r.json()
    print(f"  created {movie['id']} on {r.headers['x-node']} (version {movie['version']})")
    print(f"  all nodes identical after {wait_in_sync():.2f} s  {checksums()}")

    title("4. Cache in the proxy (5 s micro-cache)")
    for _ in range(3):
        h = http.get("/movies").headers
        print(f"  X-Cache-Status={h['x-cache-status']:<4}  answered by {h['x-node']}")

    title("5. JSON or XML, chosen by the client")
    print("  JSON:", http.get(f"/movies/{movie['id']}").text[:90], "...")
    print("  XML: ", http.get(f"/movies/{movie['id']}",
                              headers={"Accept": "application/xml"}).text[:90], "...")

    title("6. Optimistic concurrency (If-Match)")
    stale = http.put(f"/movies/{movie['id']}", json={"title": "stale edit"},
                     headers={"If-Match": '"0"'})
    print(f"  update with old version 0  -> {stale.status_code} {stale.json()}")
    ok = http.put(f"/movies/{movie['id']}", json={"title": "Demo movie (edited)", "year": 2026},
                  headers={"If-Match": f'"{movie["version"]}"'})
    print(f"  update with right version  -> {ok.status_code}, new version {ok.json()['version']}")
    wait_in_sync()

    title("7. Conflict: two nodes edit the same movie at the same time")
    names = sorted(found)[:2]
    if len(names) == 2:
        clients = {name: httpx.Client(base_url=found[name], timeout=10) for name in names}

        def edit(name: str, version: int) -> int:
            return clients[name].put(f"/movies/{movie['id']}", json={"title": f"edited on {name}"},
                                     headers={"If-Match": f'"{version}"'}).status_code

        # Both edits must start from the same version and be sent AT THE SAME TIME:
        # sent one after the other, the second node may already have pulled the first
        # edit and answers 409, so there is no conflict to resolve.
        for attempt in range(1, 6):
            current = clients[names[0]].get(f"/movies/{movie['id']}").json()["version"]
            with ThreadPoolExecutor(len(names)) as pool:
                codes = dict(zip(names, pool.map(lambda n: edit(n, current), names)))
            print(f"  attempt {attempt}: both edit version {current} ->", codes)
            wait_in_sync()
            if all(c == 200 for c in codes.values()):
                break
        else:
            raise SystemExit("could not produce a simultaneous edit on two nodes")
        for c in clients.values():
            c.close()
        for name, url in nodes().items():
            d = httpx.get(f"{url}/movies/{movie['id']}").json()
            print(f"  {name}: title={d['title']!r} version={d['version']} node={d['node']}")
        winner = max(names)   # same version on both, so the higher node name wins
        print(f"  -> every node kept the same winner, {winner} (same version, higher node name)")

    title("8. Delete (soft delete, synced like any change)")
    print("  delete ->", http.delete(f"/movies/{movie['id']}").status_code)
    wait_in_sync()
    codes = {n: httpx.get(f"{u}/movies/{movie['id']}").status_code for n, u in nodes().items()}
    print("  GET on every node ->", codes)

    title("9. Traffic filtering")
    print("  internal endpoint /sync/changes ->", http.get("/sync/changes").status_code)
    print("  scanner user-agent ->",
          http.get("/movies", headers={"User-Agent": "sqlmap/1.0"}).status_code)
    time.sleep(1)
    codes = [http.get("/movies").status_code for _ in range(130)]
    print(f"  130 quick requests -> {codes.count(200)} x 200, {codes.count(429)} x 429 (rate limit)")
    print("\nDone. Dashboard: http://127.0.0.1:8080/_proxy   Web client: http://127.0.0.1:8080/")


if __name__ == "__main__":
    main()
