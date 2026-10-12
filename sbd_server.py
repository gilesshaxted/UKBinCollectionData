"""Bin Portal API (v6): council-aware, cache-first, polite to council websites.

Run:  python sbd_server.py        (PORT env var, default 10000)
"""
import logging
import os
import re
import time
from collections import defaultdict, deque
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from sbd import addresses as addr_mod
from sbd import config, store
from sbd.engine import Engine, ScrapeError, Spec
from sbd.registry import Registry
from sbd.resolve import PostcodeError, lookup_area

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sbd_server")

VERSION = "6.0"
registry = Registry()
engine = Engine(registry)
app = FastAPI(title="Bin Portal API", version=VERSION)


@app.on_event("startup")
def _startup():
    store.connect()
    engine.start_scheduler()
    logger.info("Loaded %d councils (%d supported here)", len(registry.councils), sum(c.supported for c in registry.councils.values()))


# ---- light protection: optional token + per-IP rate limit ---------------------------------------
_hits = defaultdict(deque)


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path not in ("/", "/health"):
        if config.API_TOKEN and request.headers.get("x-sbd-token") != config.API_TOKEN:
            return JSONResponse({"detail": "Missing or wrong server token."}, status_code=401)
        ip = request.client.host if request.client else "?"
        q = _hits[ip]
        now = time.time()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= config.RATE_LIMIT_PER_MIN:
            return JSONResponse({"detail": "Too many requests."}, status_code=429)
        q.append(now)
    return await call_next(request)


# ---- models ---------------------------------------------------------------------------------------
class AddressReq(BaseModel):
    postcode: str
    module: Optional[str] = None
    os_api_key: Optional[str] = None  # the caller's own key; used for this request only, never stored or logged


class BinsReq(BaseModel):
    module: str
    uprn: Optional[str] = ""
    postcode: Optional[str] = ""
    house_number: Optional[str] = ""
    refresh: bool = False


class LegacyBinReq(BaseModel):
    address_data: str
    module: str
    os_api_key: Optional[str] = None


# ---- basic ----------------------------------------------------------------------------------------
@app.get("/")
def home():
    return {"status": "OK", "version": VERSION, "councils": len(registry.councils)}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/status")
def status():
    unsupported = [c for c in registry.councils.values() if not c.supported]
    return {
        "version": VERSION,
        "councils": len(registry.councils),
        "supported_here": len(registry.councils) - len(unsupported),
        "needs_browser": sum(1 for c in unsupported if c.reason == "needs_browser"),
        "needs_council_link": sum(1 for c in unsupported if c.reason == "needs_council_link"),
        "selenium_configured": bool(config.SELENIUM_URL),
        "queue": engine.queue_size(),
        **store.stats(),
    }


@app.get("/councils")
def councils():
    return {"councils": registry.all_public()}


@app.get("/get_councils")
def get_councils():
    """Legacy list used by older plugin versions: names with spaces."""
    return {"councils": sorted(c.display for c in registry.councils.values())}


# ---- resolve ----------------------------------------------------------------------------------------
@app.get("/resolve")
def resolve(postcode: str):
    pc = addr_mod.normalise_postcode(postcode)
    if not pc:
        raise HTTPException(400, "Please enter a valid UK postcode.")
    try:
        area = lookup_area(pc)
    except PostcodeError as exc:
        raise HTTPException(exc.status, str(exc))
    council = registry.match_area(area["lad"], area["district"])
    out = {"postcode": pc, "district": area["district"], "lad": area["lad"], "council": None}
    if council:
        out["council"] = council.public()
    return out


# ---- addresses ----------------------------------------------------------------------------------------
@app.post("/addresses")
def addresses(req: AddressReq):
    pc = addr_mod.normalise_postcode(req.postcode)
    if not pc:
        raise HTTPException(400, "Please enter a valid UK postcode.")
    council = registry.get(req.module) if req.module else None
    if req.module and council is None:
        raise HTTPException(400, "Unknown council.")
    res = addr_mod.lookup(pc, council.module if council else None, req.os_api_key)
    return {"postcode": pc, **res}


@app.post("/get_addresses")
def get_addresses(req: AddressReq):
    """Legacy shape for older plugins: a bare list, or one 'error' row."""
    pc = addr_mod.normalise_postcode(req.postcode)
    if not pc:
        return [{"uprn": "error", "address": "Please enter a valid UK postcode."}]
    council = registry.get(req.module) if req.module else None
    res = addr_mod.lookup(pc, council.module if council else None, req.os_api_key)
    if res["addresses"]:
        return res["addresses"]
    return [{"uprn": "error", "address": f"No addresses found for {pc}."}]


# ---- bins ----------------------------------------------------------------------------------------------
def _validate(req: BinsReq) -> Spec:
    council = registry.get(req.module)
    if council is None:
        raise HTTPException(400, "Unknown council.")
    pc = ""
    if req.postcode:
        pc = addr_mod.normalise_postcode(req.postcode) or ""
        if not pc:
            raise HTTPException(400, "Please enter a valid UK postcode.")
    uprn = re.sub(r"\D", "", req.uprn or "")
    number = (req.house_number or "").strip()
    if len(number) > 40:
        raise HTTPException(400, "House number or name is too long.")
    if "uprn" in council.fields and not uprn:
        raise HTTPException(400, "This council needs a UPRN.")
    if "postcode" in council.fields and not pc:
        raise HTTPException(400, "This council needs a postcode.")
    if "house_number" in council.fields and "uprn" not in council.fields and not number:
        raise HTTPException(400, "This council needs a house number or name.")
    if council.module == "WiltshireCouncil" and not pc:
        raise HTTPException(400, "Wiltshire Council requires both UPRN and Postcode.")
    return Spec(council.module, uprn, pc, number)


def _run(spec: Spec, force: bool = False):
    try:
        return engine.get(spec, force=force)
    except ScrapeError as exc:
        code = {"needs_browser": 501, "needs_council_link": 501, "busy": 503, "timeout": 504, "not_found": 404,
                "no_dates": 404, "unknown_council": 400}.get(exc.kind, 502)
        raise HTTPException(code, str(exc))


@app.post("/bins")
def bins(req: BinsReq):
    return _run(_validate(req), req.refresh)


def _parse_legacy(data: str):
    """Pulls UPRN / postcode / house text out of the old free-text 'address_data' string."""
    text = data.strip()
    text = re.sub(r"https?://\S+", "", text).strip()
    pc_re = r"[A-Za-z]{1,2}[0-9][A-Za-z0-9]?\s?[0-9][A-Za-z]{2}"
    pc = re.search(pc_re, text)
    postcode = pc.group(0).upper() if pc else ""
    if pc:
        text = text.replace(pc.group(0), "").strip()
    u = re.search(r"\b\d{8,12}\b", text)
    uprn = u.group(0) if u else ""
    if u:
        text = text.replace(uprn, "").strip()
    return uprn, postcode, text.strip(",. ")


@app.post("/get_bins")
def get_bins(req: LegacyBinReq):
    """Legacy endpoint used by plugin v5.x. Same cache and rules as /bins."""
    uprn, postcode, rest = _parse_legacy(req.address_data)
    return _run(_validate(BinsReq(module=req.module, uprn=uprn, postcode=postcode, house_number=rest)))


# ---- calendar feed (cache only) ----------------------------------------------------------------------------
def _ics_title(raw: str) -> str:
    low = raw.lower()
    if "glass" in low:
        return "Glass Recycling"
    if "recycl" in low:
        return "Recycling"
    if "garden" in low:
        return "Garden Waste"
    if "waste" in low or "refuse" in low:
        return "Household Waste"
    return raw


@app.get("/feed.ics")
def feed(module: str, uprn: str = "", postcode: str = "", house_number: str = ""):
    from datetime import datetime, timezone
    spec = _validate(BinsReq(module=module, uprn=uprn, postcode=postcode, house_number=house_number))
    cached = engine.peek(spec)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Bin Portal//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "X-WR-CALNAME:Bin Collection Schedule", "REFRESH-INTERVAL;VALUE=DURATION:P1D"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    events = 0
    for b in (cached or {}).get("bins", []):
        raw = str(b.get("bin") or b.get("type") or "Bin")
        ds = str(b.get("date") or b.get("collectionDate") or "")
        d = None
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                d = datetime.strptime(ds, fmt)
                break
            except ValueError:
                pass
        if not d:
            continue
        title = _ics_title(raw)
        lines += ["BEGIN:VEVENT", f"UID:{spec.key[:16]}-{title.replace(' ', '')}-{d:%Y%m%d}@binportal", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"SUMMARY:{title}", f"DESCRIPTION:Waste collection for {title}",
                  "TRANSP:TRANSPARENT", "END:VEVENT"]
        events += 1
    if not events:
        lines += ["BEGIN:VEVENT", f"UID:pending-{stamp}@binportal", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{stamp[:8]}",
                  "SUMMARY:Bin dates are being fetched", "DESCRIPTION:Your calendar is waiting for fresh data. Check back soon.",
                  "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
