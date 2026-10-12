"""Bin schedule engine: cache, stale-while-revalidate, polite scraping and background refresh."""
import hashlib
import json
import logging
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, Optional, Set

from . import config, store
from .registry import Council, Registry

log = logging.getLogger("sbd.engine")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLLECT = os.path.join(REPO_ROOT, "uk_bin_collection", "uk_bin_collection", "collect_data.py")


class ScrapeError(Exception):
    def __init__(self, msg: str, kind: str = "failed"):
        super().__init__(msg)
        self.kind = kind


class Spec:
    """One property: the inputs a council scraper needs."""

    def __init__(self, module: str, uprn: str = "", postcode: str = "", number: str = ""):
        self.module = module
        self.uprn = re.sub(r"\D", "", uprn or "")
        self.postcode = re.sub(r"\s+", " ", (postcode or "").strip().upper())
        self.number = (number or "").strip()

    @property
    def key(self) -> str:
        raw = "|".join([self.module, self.uprn, self.postcode.replace(" ", ""), self.number.lower()])
        return hashlib.sha1(raw.encode()).hexdigest()


def _digest(bins: list) -> str:
    norm = sorted((str(b.get("bin") or b.get("type")), str(b.get("date") or b.get("collectionDate"))) for b in bins)
    return hashlib.sha1(json.dumps(norm).encode()).hexdigest()


class Engine:
    def __init__(self, registry: Registry, runner: Optional[Callable[[Council, Spec], dict]] = None):
        self.registry = registry
        self.run_scraper = runner or self._subprocess_scraper
        self._slots = threading.BoundedSemaphore(config.MAX_PARALLEL)
        self._gap_lock = threading.Lock()
        self._last_scrape: Dict[str, float] = {}
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._queued: Set[str] = set()
        self._qlock = threading.Lock()
        self._threads_started = False

    # ---- scraping ----------------------------------------------------------------------------
    def _subprocess_scraper(self, council: Council, spec: Spec) -> dict:
        cmd = [sys.executable, COLLECT, council.module, council.url or "https://example.com"]
        if council.skip_get_url:
            cmd.append("-s")
        if spec.uprn:
            cmd += ["-u", spec.uprn]
        if spec.postcode:
            cmd += ["-p", spec.postcode]
        if spec.number:
            cmd += ["-n", spec.number]
        if council.selenium and config.SELENIUM_URL:
            cmd += ["-w", config.SELENIUM_URL]
        env = os.environ.copy()
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=config.SCRAPE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ScrapeError("The council website took too long to answer.", "timeout")
        out = (res.stdout or "").strip()
        if res.returncode != 0:
            tail = (res.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
            raise ScrapeError("Scraper failed: " + tail[0][:200])
        if "Exception encountered" in out or "Invalid UPRN" in out:
            raise ScrapeError("The council system did not recognise this address.", "not_found")
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            m = re.search(r'(\{.*"bins".*\})', out, re.DOTALL)
            if m:
                return json.loads(m.group(1))
            raise ScrapeError("Could not read the scraper output.")

    def _wait_turn(self, council: Council) -> None:
        with self._gap_lock:
            now = time.time()
            start = max(now, self._last_scrape.get(council.module, 0) + config.MODULE_GAP)
            self._last_scrape[council.module] = start
        if start > now:
            time.sleep(start - now)

    def scrape(self, council: Council, spec: Spec) -> list:
        if not self._slots.acquire(timeout=config.SYNC_WAIT):
            raise ScrapeError("The server is busy, please try again shortly.", "busy")
        try:
            self._wait_turn(council)
            data = self.run_scraper(council, spec)
        finally:
            self._slots.release()
        bins = data.get("bins") if isinstance(data, dict) else None
        if not bins:
            raise ScrapeError("The council returned no collection dates for this address.", "no_dates")
        return bins

    # ---- cache -------------------------------------------------------------------------------
    def _ttl(self, council: Council) -> int:
        return config.FRESH_TTL_BROWSER if council.selenium else config.FRESH_TTL

    def _jitter(self, seconds: float) -> float:
        return seconds * random.uniform(0.9, 1.15)

    def refresh(self, spec: Spec) -> list:
        council = self.registry.get(spec.module)
        key = spec.key
        try:
            bins = self.scrape(council, spec)
        except ScrapeError as exc:
            row = store.get_bins_row(key) or {}
            fails = (row.get("fail_count") or 0) + 1
            backoff = min(24 * 3600 * (2 ** (fails - 1)), 7 * 24 * 3600)
            store.save_failure(key, str(exc), time.time() + self._jitter(backoff), fails >= config.MAX_FAILS)
            raise
        store.save_success(key, json.dumps(bins), _digest(bins), time.time() + self._jitter(self._ttl(council)))
        return bins

    def get(self, spec: Spec, force: bool = False) -> dict:
        """Returns {"bins": [...], "meta": {...}}. Raises ScrapeError when there is nothing to show."""
        council = self.registry.get(spec.module)
        if council is None:
            raise ScrapeError("Unknown council.", "unknown_council")
        if council.reason:
            raise ScrapeError(
                "This council needs a browser-based lookup that this server does not run yet." if council.reason == "needs_browser"
                else "This council needs a council-specific link and is not supported through the postcode form yet.",
                council.reason,
            )
        key = spec.key
        store.touch_request(key, spec.module, spec.uprn, spec.postcode, spec.number)
        row = store.get_bins_row(key)
        now = time.time()
        have = bool(row and row.get("data"))
        age = (now - row["fetched"]) if have else None

        if have and not force and age < self._ttl(council):
            return self._reply(row, "fresh")
        if have and not force and age < config.STALE_MAX:
            self.enqueue(spec)
            return self._reply(row, "stale")
        try:
            self.refresh(spec)
            return self._reply(store.get_bins_row(key), "fresh")
        except ScrapeError:
            if have:
                return self._reply(store.get_bins_row(key), "stale-error")
            raise

    def _reply(self, row: dict, status: str) -> dict:
        return {
            "bins": json.loads(row["data"]),
            "meta": {
                "status": status,
                "fetched": int(row["fetched"]),
                "age_seconds": int(time.time() - row["fetched"]),
                "changed_at": int(row["changed_at"]) if row.get("changed_at") else None,
                "last_error": row.get("last_error"),
            },
        }

    def peek(self, spec: Spec) -> Optional[dict]:
        """Cache-only read (used by the .ics feed). Queues a refresh when old or missing."""
        council = self.registry.get(spec.module)
        if council is None or council.reason:
            return None
        key = spec.key
        store.touch_request(key, spec.module, spec.uprn, spec.postcode, spec.number)
        row = store.get_bins_row(key)
        if not row or not row.get("data"):
            self.enqueue(spec)
            return None
        if time.time() - row["fetched"] >= self._ttl(council):
            self.enqueue(spec)
        return self._reply(row, "cached")

    # ---- background work ----------------------------------------------------------------------
    def enqueue(self, spec: Spec) -> None:
        with self._qlock:
            if spec.key in self._queued:
                return
            self._queued.add(spec.key)
        self._queue.put(json.dumps([spec.module, spec.uprn, spec.postcode, spec.number]))
        self.start_threads()

    def _worker(self) -> None:
        while True:
            raw = self._queue.get()
            module, uprn, postcode, number = json.loads(raw)
            spec = Spec(module, uprn, postcode, number)
            try:
                self.refresh(spec)
                log.info("background refresh ok: %s", module)
            except Exception as exc:
                log.warning("background refresh failed for %s: %s", module, exc)
            finally:
                with self._qlock:
                    self._queued.discard(spec.key)

    def tick(self) -> int:
        """One scheduler pass: queue schedules that are due. Returns how many were queued."""
        active_since = time.time() - config.SUBSCRIPTION_DAYS * 86400
        store.expire_idle(active_since)
        n = 0
        for row in store.due_rows(config.REFRESH_BATCH, active_since):
            council = self.registry.get(row["module"])
            if council is None or council.reason:
                continue
            self.enqueue(Spec(row["module"], row["uprn"] or "", row["postcode"] or "", row["number"] or ""))
            n += 1
        return n

    def _scheduler(self) -> None:
        time.sleep(30)
        while True:
            try:
                self.tick()
            except Exception as exc:
                log.warning("scheduler error: %s", exc)
            time.sleep(config.SCHEDULER_TICK)

    def start_threads(self) -> None:
        if self._threads_started:
            return
        self._threads_started = True
        threading.Thread(target=self._worker, daemon=True, name="sbd-worker").start()

    def start_scheduler(self) -> None:
        self.start_threads()
        threading.Thread(target=self._scheduler, daemon=True, name="sbd-scheduler").start()

    def queue_size(self) -> int:
        return self._queue.qsize()
