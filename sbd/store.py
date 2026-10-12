"""Small SQLite store: postcode->council lookups, address lists, bin schedules and refresh state."""
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from . import config

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_path: Optional[str] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS postcodes (postcode TEXT PRIMARY KEY, lad TEXT, district TEXT, fetched REAL);
CREATE TABLE IF NOT EXISTS addresses (postcode TEXT, module TEXT, source TEXT, data TEXT, fetched REAL, PRIMARY KEY (postcode, module));
CREATE TABLE IF NOT EXISTS bins (
    key TEXT PRIMARY KEY, module TEXT, uprn TEXT, postcode TEXT, number TEXT,
    data TEXT, hash TEXT, fetched REAL, changed_at REAL,
    last_requested REAL, next_refresh REAL, fail_count INTEGER DEFAULT 0, last_error TEXT, subscribed INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS bins_due ON bins (subscribed, next_refresh);
"""


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    global _conn, _path
    with _lock:
        if path is None and _conn is not None:
            return _conn
        path = path or config.DB_PATH
        if _conn is not None and _path == path:
            return _conn
        if _conn is not None:
            _conn.close()
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _path = path
        return _conn


def reset() -> None:
    """Closes the connection (tests use this to start from an empty database)."""
    global _conn, _path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        _path = None


def _x(sql: str, args: tuple = ()):
    with _lock:
        c = connect()
        cur = c.execute(sql, args)
        c.commit()
        return cur


def _q(sql: str, args: tuple = ()) -> List[sqlite3.Row]:
    with _lock:
        return connect().execute(sql, args).fetchall()


# ---- postcodes -------------------------------------------------------------
def get_postcode(pc: str) -> Optional[Dict[str, Any]]:
    rows = _q("SELECT * FROM postcodes WHERE postcode=?", (pc,))
    if rows and time.time() - rows[0]["fetched"] < config.POSTCODE_TTL:
        return dict(rows[0])
    return None


def put_postcode(pc: str, lad: Optional[str], district: Optional[str]) -> None:
    _x("INSERT OR REPLACE INTO postcodes VALUES (?,?,?,?)", (pc, lad, district, time.time()))


# ---- addresses -------------------------------------------------------------
def get_addresses(pc: str, module: str) -> Optional[Dict[str, Any]]:
    rows = _q("SELECT * FROM addresses WHERE postcode=? AND module=?", (pc, module))
    if rows and time.time() - rows[0]["fetched"] < config.ADDRESS_TTL:
        return {"source": rows[0]["source"], "data": json.loads(rows[0]["data"]), "fetched": rows[0]["fetched"]}
    return None


def put_addresses(pc: str, module: str, source: str, data: list) -> None:
    _x("INSERT OR REPLACE INTO addresses VALUES (?,?,?,?,?)", (pc, module, source, json.dumps(data), time.time()))


# ---- bins ------------------------------------------------------------------
def get_bins_row(key: str) -> Optional[Dict[str, Any]]:
    rows = _q("SELECT * FROM bins WHERE key=?", (key,))
    return dict(rows[0]) if rows else None


def touch_request(key: str, module: str, uprn: str, postcode: str, number: str) -> None:
    now = time.time()
    _x(
        "INSERT INTO bins (key,module,uprn,postcode,number,last_requested,next_refresh,subscribed) VALUES (?,?,?,?,?,?,?,1) "
        "ON CONFLICT(key) DO UPDATE SET last_requested=excluded.last_requested, subscribed=1",
        (key, module, uprn, postcode, number, now, now + 1800),
    )


def save_success(key: str, data_json: str, digest: str, next_refresh: float) -> bool:
    """Stores a good result. Returns True if the schedule differs from the previous one."""
    now = time.time()
    row = get_bins_row(key)
    changed = bool(row and row.get("hash") and row["hash"] != digest)
    changed_at = now if (changed or not row or not row.get("changed_at")) else row["changed_at"]
    _x(
        "UPDATE bins SET data=?, hash=?, fetched=?, changed_at=?, next_refresh=?, fail_count=0, last_error=NULL, subscribed=1 WHERE key=?",
        (data_json, digest, now, changed_at, next_refresh, key),
    )
    return changed


def save_failure(key: str, error: str, next_refresh: float, unsubscribe: bool) -> None:
    _x(
        "UPDATE bins SET fail_count=fail_count+1, last_error=?, next_refresh=?, subscribed=CASE WHEN ? THEN 0 ELSE subscribed END WHERE key=?",
        (error[:300], next_refresh, 1 if unsubscribe else 0, key),
    )


def due_rows(limit: int, active_since: float) -> List[Dict[str, Any]]:
    rows = _q(
        "SELECT * FROM bins WHERE subscribed=1 AND next_refresh<=? AND last_requested>=? ORDER BY next_refresh LIMIT ?",
        (time.time(), active_since, limit),
    )
    return [dict(r) for r in rows]


def expire_idle(active_since: float) -> int:
    return _x("UPDATE bins SET subscribed=0 WHERE subscribed=1 AND last_requested<?", (active_since,)).rowcount


def stats() -> Dict[str, Any]:
    out = {}
    out["schedules_cached"] = _q("SELECT COUNT(*) c FROM bins WHERE data IS NOT NULL")[0]["c"]
    out["subscribed"] = _q("SELECT COUNT(*) c FROM bins WHERE subscribed=1")[0]["c"]
    out["failing"] = _q("SELECT COUNT(*) c FROM bins WHERE fail_count>0")[0]["c"]
    out["address_lists"] = _q("SELECT COUNT(*) c FROM addresses")[0]["c"]
    out["postcodes"] = _q("SELECT COUNT(*) c FROM postcodes")[0]["c"]
    return out
