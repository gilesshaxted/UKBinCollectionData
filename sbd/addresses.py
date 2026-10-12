"""Postcode -> address list (with UPRNs).

Order: stored list -> the council's own free lookup (adapter) -> the caller's own OS Places key.
OS-derived lists are never written to the shared store (OS licence), they are only passed back.
"""
import logging
import re
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

import requests

from . import config, store

log = logging.getLogger("sbd.addresses")
UA = "BinPortal/6.0 (+https://github.com/gilesshaxted/UKBinCollectionData)"
PC_RE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?[0-9][A-Z]{2}$")


def normalise_postcode(raw: str) -> Optional[str]:
    clean = re.sub(r"\s+", "", (raw or "")).upper()
    if not PC_RE.match(clean):
        return None
    return clean[:-3] + " " + clean[-3:]


# ---- adapters: one function per council that has a free, keyless address lookup -------------
def wiltshire(postcode: str) -> Optional[List[dict]]:
    now = datetime.now()
    r = requests.post(
        "https://ilambassadorformsprod.azurewebsites.net/wastecollectiondays/addresslist",
        data={"Month": now.month, "Year": now.year, "Postcode": postcode, "Uprn": ""},
        headers={"User-Agent": UA, "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        timeout=15,
    )
    if r.status_code != 200:
        log.warning("wiltshire address lookup HTTP %s", r.status_code)
        return None
    j = r.json()
    if not j.get("IsSuccess"):
        return None
    out = []
    for item in (j.get("Model") or {}).get("PostcodeAddresses") or []:
        if item.get("UPRN") and item.get("Address"):
            out.append({"uprn": str(item["UPRN"]), "address": re.sub(r"<[^>]+>", "", str(item["Address"]))})
    return out


ADAPTERS: Dict[str, Callable[[str], Optional[List[dict]]]] = {"WiltshireCouncil": wiltshire}


def has_free_lookup(module: str) -> bool:
    return module in ADAPTERS


# ---- OS Places (caller's own key) -------------------------------------------------------------
def os_places(postcode: str, key: str) -> Optional[List[dict]]:
    r = requests.get(
        "https://api.os.uk/search/places/v1/postcode",
        params={"postcode": postcode, "key": key, "dataset": "DPA,LPI"},
        headers={"User-Agent": UA},
        timeout=15,
    )
    if r.status_code != 200:
        log.warning("OS Places HTTP %s", r.status_code)  # never log the key
        return None
    out = []
    for item in r.json().get("results") or []:
        d = item.get("DPA") or item.get("LPI")
        if d and d.get("UPRN") and d.get("ADDRESS"):
            out.append({"uprn": str(d["UPRN"]), "address": d["ADDRESS"]})
    return out


# ---- optional last resort, off by default (uprn.uk blocks cloud IPs) -------------------------
def uprn_uk(postcode: str) -> Optional[List[dict]]:
    from bs4 import BeautifulSoup
    r = requests.get("https://www.uprn.uk/addresses/" + postcode.replace(" ", ""), headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
    if r.status_code != 200:
        return None
    out = []
    for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
        if "/uprn/" in a["href"]:
            m = re.search(r"\d{8,12}", a["href"])
            if m:
                out.append({"uprn": m.group(0), "address": re.sub(r"\s+", " ", a.get_text()).strip()})
    return out or None


_gap_lock = threading.Lock()
_last_call: Dict[str, float] = {}


def _polite(module: str) -> None:
    with _gap_lock:
        wait = 1.0 - (time.time() - _last_call.get(module, 0))
        _last_call[module] = time.time() + max(wait, 0)
    if wait > 0:
        time.sleep(wait)


def lookup(postcode: str, module: Optional[str], os_key: Optional[str] = None) -> dict:
    """Returns {"addresses": [...], "source": str|None, "manual": bool, "empty": bool}.
    manual=True means no source could answer and the visitor must type their UPRN."""
    answered_empty = False
    if module:
        cached = store.get_addresses(postcode, module)
        if cached and cached["data"]:
            return {"addresses": cached["data"], "source": cached["source"] + " (stored)", "manual": False, "empty": False}
        adapter = ADAPTERS.get(module)
        if adapter:
            try:
                _polite(module)
                res = adapter(postcode)
                if res:
                    store.put_addresses(postcode, module, "council", res)
                    return {"addresses": res, "source": "council", "manual": False, "empty": False}
                if res == []:
                    answered_empty = True
            except Exception as exc:
                log.warning("%s adapter failed: %s", module, exc)

    if os_key and len(os_key) >= 10:
        try:
            res = os_places(postcode, os_key)
            if res:
                return {"addresses": res, "source": "os", "manual": False, "empty": False}
            if res == []:
                answered_empty = True
        except Exception as exc:
            log.warning("OS lookup failed: %s", type(exc).__name__)

    if config.ENABLE_UPRN_UK:
        try:
            res = uprn_uk(postcode)
            if res:
                return {"addresses": res, "source": "uprn.uk", "manual": False, "empty": False}
        except Exception as exc:
            log.warning("uprn.uk failed: %s", exc)

    return {"addresses": [], "source": None, "manual": not answered_empty, "empty": answered_empty}
