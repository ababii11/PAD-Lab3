"""Data-warehouse (DW) node: HTTP API over its own MongoDB.

Public API (reached by clients only through HAProxy):
    GET    /movies?offset=0&limit=20      list
    GET    /movies/{id}                   one movie (ETag = version)
    POST   /movies                        create
    PUT    /movies/{id}                   update  (If-Match: "<version>" optional)
    DELETE /movies/{id}                   delete  (If-Match optional)
    GET    /health                        used by HAProxy health checks
Every response may be JSON or XML (?format=xml or Accept: application/xml).

Internal API (node-to-node sync; HAProxy blocks it from the outside):
    GET  /sync/changes?since=N    change feed
    GET  /sync/digest             (id, version, node) for every record
    POST /sync/docs               full documents for a list of ids
    GET  /sync/checksum           hash of the whole data set (to compare nodes)
    GET  /info                    node status
"""
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo.errors import PyMongoError, ServerSelectionTimeoutError

from . import settings
from .formats import respond
from .store import NotFound, Store, VersionConflict, public
from .sync import Syncer

logging.basicConfig(level=logging.INFO,
                    format=f"%(asctime)s {settings.NODE_NAME} %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("dw.api")

store = Store()
syncer = Syncer(store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.init()
    syncer.start()
    log.info("node %s ready at %s", settings.NODE_NAME, settings.ADVERTISE_URL)
    yield
    await syncer.stop()


app = FastAPI(title=f"DW node {settings.NODE_NAME}", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_unavailable(request: Request, exc: PyMongoError):
    """Our MongoDB is down: answer 503 so the proxy can use another node."""
    log.warning("database error: %s", exc)
    # No server could even be selected, so nothing was written: the proxy may safely
    # resend this request (even a POST) to another node.
    headers = {"X-Retry-Safe": "1"} if isinstance(exc, ServerSelectionTimeoutError) else {}
    return JSONResponse({"error": "database unavailable", "node": settings.NODE_NAME},
                        status_code=503, headers=headers)


@app.middleware("http")
async def add_node_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Node"] = settings.NODE_NAME   # shows which server answered
    if (request.method == "GET" and response.status_code == 200
            and request.url.path.startswith("/movies")):
        # s-maxage: only shared caches (HAProxy) may keep it, for at most 5 seconds.
        response.headers.setdefault("Cache-Control", "s-maxage=5")
    return response


class MovieIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    year: int | None = Field(default=None, ge=1870, le=2100)
    genre: str | None = None
    actors: list[str] = []
    rating: float | None = Field(default=None, ge=0, le=10)


def parse_if_match(value: str | None) -> int | None:
    if not value or value.strip() == "*":
        return None
    value = value.strip().removeprefix("W/").strip('"')
    try:
        return int(value)
    except ValueError:
        return -1  # unparsable -> never matches -> 409


def error(request: Request, status: int, message: str, **extra) -> Response:
    return respond(request, {"error": message, **extra}, root="error", status=status)


def etag(doc: dict) -> dict:
    return {"ETag": f'"{doc["version"]}"'}


# ---------------- public API ----------------
@app.get("/health")
async def health():
    if await store.ping():
        return {"status": "ok", "node": settings.NODE_NAME}
    return Response('{"status":"db-down"}', status_code=503, media_type="application/json")


@app.get("/movies")
async def list_movies(request: Request, offset: int = Query(0, ge=0),
                      limit: int = Query(20, ge=1, le=100)):
    total, items = await store.list_movies(offset, limit)
    return respond(request, {"total": total, "offset": offset, "limit": limit, "items": items},
                   root="movies")


@app.get("/movies/{movie_id}")
async def get_movie(request: Request, movie_id: str):
    try:
        doc = await store.get_movie(movie_id)
    except NotFound:
        return error(request, 404, "movie not found")
    return respond(request, doc, root="movie", headers=etag(doc))


@app.post("/movies", status_code=201)
async def create_movie(request: Request, movie: MovieIn):
    doc = await store.local_write(str(uuid.uuid4()), movie.model_dump(),
                                  expected_version=None, must_exist=False)
    out = public(doc)
    return respond(request, out, root="movie", status=201,
                   headers={**etag(out), "Location": f"/movies/{out['id']}"})


@app.put("/movies/{movie_id}")
async def update_movie(request: Request, movie_id: str, movie: MovieIn,
                       if_match: str | None = Header(default=None)):
    try:
        doc = await store.local_write(movie_id, movie.model_dump(),
                                      expected_version=parse_if_match(if_match), must_exist=True)
    except NotFound:
        return error(request, 404, "movie not found")
    except VersionConflict as exc:
        return error(request, 409, "version conflict: reload and retry",
                     current_version=exc.current_version)
    out = public(doc)
    return respond(request, out, root="movie", headers=etag(out))


@app.delete("/movies/{movie_id}")
async def delete_movie(request: Request, movie_id: str,
                       if_match: str | None = Header(default=None)):
    try:
        await store.local_write(movie_id, None, expected_version=parse_if_match(if_match),
                                must_exist=True)
    except NotFound:
        return error(request, 404, "movie not found")
    except VersionConflict as exc:
        return error(request, 409, "version conflict: reload and retry",
                     current_version=exc.current_version)
    return Response(status_code=204)


# ---------------- internal sync API ----------------
@app.get("/sync/changes")
async def sync_changes(since: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=5000)):
    return await store.changes_since(since, limit)


@app.get("/sync/digest")
async def sync_digest():
    return {"node": settings.NODE_NAME, "items": await store.digest()}


class IdList(BaseModel):
    ids: list[str]


@app.post("/sync/docs")
async def sync_docs(body: IdList):
    return {"node": settings.NODE_NAME, "docs": await store.docs_by_ids(body.ids[:1000])}


@app.get("/sync/checksum")
async def sync_checksum():
    return await store.checksum()


@app.get("/sync/status")
async def sync_status():
    """For the dashboard: is our database reachable, and what does our data look like."""
    db_ok = await store.ping()
    return {"node": settings.NODE_NAME, "db_ok": db_ok,
            "checksum": (await store.checksum())["hash"] if db_ok else None,
            "in_sync": db_ok and syncer.caught_up(), "syncing": syncer.recently_active(),
            "peers": sorted(syncer.peers)}


@app.get("/info")
async def info(request: Request):
    peers = {p["_id"]: {"last_seq": p.get("last_seq", 0)} for p in await store.peers()}
    for name, st in syncer.status.items():
        peers.setdefault(name, {}).update(st)
    data = {"node": settings.NODE_NAME, "url": settings.ADVERTISE_URL,
            "known_peers": sorted(syncer.peers), "peers": peers,
            "checksum": await store.checksum()}
    return respond(request, data, root="info")
