"""Command-line client. It knows ONLY the proxy address, never the servers.

    python client/cli.py list [--offset 0] [--limit 20]
    python client/cli.py get <id>
    python client/cli.py create "Inception" --year 2010 --genre sci-fi --actor DiCaprio --actor Page
    python client/cli.py update <id> "New title" [--year ...] [--version 3]
    python client/cli.py delete <id> [--version 3]

Options: --xml (ask for XML), --proxy http://host:8080, -v (show which node answered)
"""
import argparse
import json
import os
import sys

import httpx

DEFAULT_PROXY = os.environ.get("PROXY_URL", "http://127.0.0.1:8080")


def main() -> int:
    p = argparse.ArgumentParser(description="Smart Proxy lab client")
    p.add_argument("--proxy", default=DEFAULT_PROXY)
    p.add_argument("--xml", action="store_true", help="request XML instead of JSON")
    p.add_argument("-v", "--verbose", action="store_true", help="show response headers")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list"); s.add_argument("--offset", type=int, default=0)
    s.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("get"); s.add_argument("id")
    for name in ("create", "update"):
        s = sub.add_parser(name)
        if name == "update":
            s.add_argument("id")
        s.add_argument("title")
        s.add_argument("--year", type=int)
        s.add_argument("--genre")
        s.add_argument("--actor", action="append", default=[])
        s.add_argument("--rating", type=float)
        if name == "update":
            s.add_argument("--version", type=int, help="expected version (If-Match)")
    s = sub.add_parser("delete"); s.add_argument("id")
    s.add_argument("--version", type=int, help="expected version (If-Match)")
    a = p.parse_args()

    headers = {"Accept": "application/xml" if a.xml else "application/json"}
    if getattr(a, "version", None) is not None:
        headers["If-Match"] = f'"{a.version}"'

    with httpx.Client(base_url=a.proxy, headers=headers, timeout=10) as http:
        if a.cmd == "list":
            r = http.get("/movies", params={"offset": a.offset, "limit": a.limit})
        elif a.cmd == "get":
            r = http.get(f"/movies/{a.id}")
        elif a.cmd in ("create", "update"):
            body = {"title": a.title, "year": a.year, "genre": a.genre,
                    "actors": a.actor, "rating": a.rating}
            r = (http.post("/movies", json=body) if a.cmd == "create"
                 else http.put(f"/movies/{a.id}", json=body))
        else:
            r = http.delete(f"/movies/{a.id}")

    if a.verbose:
        print(f"HTTP {r.status_code}  node={r.headers.get('x-node')}  "
              f"cache={r.headers.get('x-cache-status')}  etag={r.headers.get('etag')}",
              file=sys.stderr)
    if r.content:
        if "json" in r.headers.get("content-type", ""):
            print(json.dumps(r.json(), indent=2, ensure_ascii=False))
        else:
            print(r.text)
    elif r.status_code == 204:
        print("deleted")
    return 0 if r.status_code < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
