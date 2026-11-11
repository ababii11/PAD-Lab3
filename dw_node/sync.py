"""Background work of a DW node:

  * heartbeat   -- tell the registrar "I am alive at <url>" (service discovery)
  * pull        -- change feed: GET <peer>/sync/changes?since=N from every peer
  * reconcile   -- full comparison with every peer, repairs anything missed
  * trim        -- delete old change-log entries and old tombstones

[RO] PE SCURT: patru bucle care rulează continuu pe fiecare server, în fundal:
    heartbeat()      la 3 s:  „sunt viu la adresa X” către registrar; primește înapoi lista
                              celorlalte servere (peers)
    pull_all()       la 2 s:  de la FIECARE peer cere doar schimbările noi (după ultimul număr
                              primit) și le aplică cu regula de conflict
    reconcile_all()  la 60 s: compară lista completă (id, version, node) cu fiecare peer și
                              copiază ce lipsește; e plasa de siguranță pentru ce n-a prins pull-ul
    trim()           la 10 min: curăță jurnalul vechi
Este un model „PULL”: fiecare server își ia singur datele de la ceilalți. Un server căzut
nu blochează pe nimeni; când revine, cere tot ce a pierdut, pornind de la ultimul număr
salvat în baza lui (colecția `peers`).
"""
import asyncio
import logging
import time

import httpx

from . import settings
from .store import Store, is_newer

log = logging.getLogger("dw.sync")


class Syncer:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.http = httpx.AsyncClient(timeout=5.0)
        self.peers: dict[str, str] = {}          # name -> url, from the registrar
        self.status: dict[str, dict] = {}        # name -> last result, for /info
        self._tasks: list[asyncio.Task] = []
        self._reconcile_now: set[str] = set()
        self._last_applied = 0.0                 # when we last copied a change from a peer

    def caught_up(self) -> bool:
        """True if, at the last pull, we had copied everything every reachable peer had."""
        now = time.time()
        for name in self.peers:
            st = self.status.get(name, {})
            if st.get("reachable") is False:
                continue                      # a peer that is down cannot be compared
            if not st.get("caught_up") or now - st.get("last_pull", 0) > 3 * settings.PULL_INTERVAL:
                return False
        return True

    def recently_active(self) -> bool:
        """True while we are still copying changes from peers (catching up)."""
        return time.time() - self._last_applied < settings.PULL_INTERVAL * 2

    # ---------- lifecycle ----------
    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop(self.heartbeat, settings.HEARTBEAT_INTERVAL)),
            asyncio.create_task(self._loop(self.pull_all, settings.PULL_INTERVAL)),
            asyncio.create_task(self._loop(self.reconcile_all, settings.RECONCILE_INTERVAL,
                                           first_delay=3)),
            asyncio.create_task(self._loop(self.store.trim, 600)),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        try:
            await self.http.post(f"{settings.REGISTRAR_URL}/deregister",
                                 json={"name": settings.NODE_NAME}, timeout=2)
        except Exception:
            pass
        await self.http.aclose()

    @staticmethod
    async def _loop(fn, interval: float, first_delay: float = 0) -> None:
        if first_delay:
            await asyncio.sleep(first_delay)
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s failed: %s", fn.__name__, exc)
            await asyncio.sleep(interval)

    # ---------- service discovery ----------
    async def heartbeat(self) -> None:
        body = {"name": settings.NODE_NAME, "url": settings.ADVERTISE_URL,
                "weight": settings.WEIGHT}
        try:
            r = await self.http.post(f"{settings.REGISTRAR_URL}/register", json=body)
            r.raise_for_status()
            nodes = r.json().get("nodes", [])
        except Exception as exc:
            log.warning("registrar unreachable (%s); keeping last known peers", exc)
            return
        new_peers = {n["name"]: n["url"] for n in nodes if n["name"] != settings.NODE_NAME}
        # [RO] Un peer văzut prima dată (sau unul care a revenit) primește o comparație
        # completă, nu doar pull: e cel mai sigur mod de a recupera după o absență.
        for name in new_peers.keys() - self.peers.keys():
            log.info("discovered peer %s at %s", name, new_peers[name])
            self._reconcile_now.add(name)       # first contact: full comparison
        self.peers = new_peers

    # ---------- change feed (pull) ----------
    async def pull_all(self) -> None:
        for name, url in list(self.peers.items()):
            try:
                if name in self._reconcile_now:
                    await self.reconcile_with(name, url)
                    self._reconcile_now.discard(name)
                applied = await self.pull_from(name, url)
                self.status.setdefault(name, {}).update(
                    url=url, reachable=True, caught_up=True, last_pull=time.time())
                if applied:
                    self._last_applied = time.time()
                    log.info("pulled %d change(s) from %s", applied, name)
            except httpx.HTTPError as exc:
                self.status.setdefault(name, {}).update(url=url, reachable=False,
                                                        error=str(exc))

    async def pull_from(self, name: str, url: str) -> int:
        # [RO] Exemplu: ultima dată am primit de la node2 până la nr. 17 (salvat în baza
        # noastră). Cerem /sync/changes?since=17 și primim 18, 19, 20; le aplicăm și salvăm 20.
        # Pentru că poziția stă în baza de date, nu în memorie, un restart nu pierde nimic.
        # Două cazuri speciale: peer-ul are bază nouă (alt `instance`) -> o luăm de la 0;
        # intrările de care avem nevoie au fost șterse („gap”) -> comparație completă.
        state =await self.store.get_peer(name) or {}
        last = state.get("last_seq", 0)
        applied = 0
        while True:
            r = await self.http.get(f"{url}/sync/changes",
                                    params={"since": last, "limit": settings.PULL_BATCH})
            r.raise_for_status()
            feed = r.json()

            # Peer's database was replaced (new instance id): start over.
            if state.get("instance") and feed["instance"] != state["instance"]:
                log.warning("%s has a new database; resetting its feed position", name)
                last = 0
                state["instance"] = feed["instance"]
                await self.store.save_peer(name, last_seq=0, instance=feed["instance"], url=url)
                self._reconcile_now.add(name)
                continue

            # Gap: the entries we need were trimmed from the peer's log.
            if last < feed["min_seq"] - 1:
                log.warning("gap in %s's change log (have %d, oldest %d): reconciling",
                            name, last, feed["min_seq"])
                target = feed["max_seq"]
                await self.reconcile_with(name, url)
                await self.store.save_peer(name, last_seq=target, instance=feed["instance"], url=url)
                return applied

            for change in feed["changes"]:
                if await self.store.apply_remote(change["doc"]):
                    applied += 1
                last = change["seq"]
            await self.store.save_peer(name, last_seq=last, instance=feed["instance"], url=url)
            state["instance"] = feed["instance"]
            if len(feed["changes"]) < settings.PULL_BATCH:
                return applied

    # ---------- periodic full reconciliation ----------
    async def reconcile_all(self) -> None:
        for name, url in list(self.peers.items()):
            try:
                await self.reconcile_with(name, url)
            except httpx.HTTPError as exc:
                log.warning("reconcile with %s failed: %s", name, exc)

    async def reconcile_with(self, name: str, url: str) -> int:
        """Compare (id, version, node) lists and copy every record the peer has newer."""
        # [RO] Pasul 1: luăm de la peer doar „cuprinsul” (id, version, node) pentru toate filmele.
        # Pasul 2: îl comparăm cu al nostru și cerem documentele complete doar pentru cele la
        # care peer-ul e mai nou sau pe care nu le avem deloc.
        r =await self.http.get(f"{url}/sync/digest")
        r.raise_for_status()
        theirs = r.json()["items"]
        mine = {i: {"version": v, "node": n} for i, v, n in await self.store.digest()}
        needed = [i for i, v, n in theirs
                  if i not in mine or is_newer({"version": v, "node": n}, mine[i])]
        fixed = 0
        for start in range(0, len(needed), 200):
            chunk = needed[start:start + 200]
            r = await self.http.post(f"{url}/sync/docs", json={"ids": chunk})
            r.raise_for_status()
            for doc in r.json()["docs"]:
                if await self.store.apply_remote(doc):
                    fixed += 1
        self.status.setdefault(name, {}).update(last_reconcile=time.time(), reconcile_fixed=fixed)
        if fixed:
            self._last_applied = time.time()
            log.info("reconciliation with %s repaired %d record(s)", name, fixed)
        return fixed
