"""Council registry built from the repo's own input.json and scraper sources."""
import json
import os
import re
from typing import Dict, List, Optional

from . import config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_JSON = os.path.join(REPO_ROOT, "uk_bin_collection", "tests", "input.json")
COUNCILS_DIR = os.path.join(REPO_ROOT, "uk_bin_collection", "uk_bin_collection", "councils")
FIELD_KEYS = ("uprn", "postcode", "house_number")


def _pretty(module: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", module)


def _norm_name(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"\b(the|council|councils|city|borough|district|metropolitan|county|royal|london|of|and|unitary|authority|mbc|bc|cc|dc)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


class Council:
    def __init__(self, module: str, entry: dict, selenium: bool):
        self.module = module
        self.name = entry.get("wiki_name") or _pretty(module)
        self.display = _pretty(module)
        self.url = entry.get("url") or ""
        self.skip_get_url = bool(entry.get("skip_get_url"))
        self.selenium = selenium
        lads = []
        if entry.get("LAD24CD"):
            lads.append(entry["LAD24CD"])
        lads.extend(entry.get("supported_councils_LAD24CD") or [])
        self.lads = lads
        self.wiki_note = entry.get("wiki_note") or ""
        self.fields = [k for k in FIELD_KEYS if k in entry or (k == "house_number" and "paon" in entry)]
        # What the visitor has to supply. "uprn": pick an address. "details": postcode / house number.
        # "none": the input.json has no per-property inputs (calendar feeds, council-specific URLs).
        if "uprn" in self.fields:
            self.mode = "uprn"
        elif self.fields:
            self.mode = "details"
        else:
            self.mode = "none"

    @property
    def supported(self) -> bool:
        return self.reason is None

    @property
    def reason(self) -> Optional[str]:
        if self.mode == "none":
            return "needs_council_link"
        if self.selenium and not config.SELENIUM_URL:
            return "needs_browser"
        return None

    def public(self) -> dict:
        from .addresses import has_free_lookup
        return {
            "module": self.module,
            "name": self.name,
            "display": self.display,
            "lad": self.lads,
            "mode": self.mode,
            "fields": self.fields,
            "selenium": self.selenium,
            "supported": self.supported,
            "reason": self.reason,
            "free_address_lookup": has_free_lookup(self.module),
        }


def _uses_selenium(module: str) -> bool:
    path = os.path.join(COUNCILS_DIR, module + ".py")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False
    return "create_webdriver" in text or "from selenium" in text or "import selenium" in text


class Registry:
    def __init__(self, input_json: str = INPUT_JSON, councils_dir: str = COUNCILS_DIR):
        with open(input_json, encoding="utf-8") as fh:
            data = json.load(fh)
        have = set()
        if os.path.isdir(councils_dir):
            have = {f[:-3] for f in os.listdir(councils_dir) if f.endswith(".py") and not f.startswith("__")}
        self.councils: Dict[str, Council] = {}
        for module, entry in data.items():
            if have and module not in have:
                continue
            selenium = "web_driver" in entry or _uses_selenium(module)
            self.councils[module] = Council(module, entry, selenium)
        self.by_lad: Dict[str, List[Council]] = {}
        for c in self.councils.values():
            for lad in c.lads:
                self.by_lad.setdefault(lad, []).append(c)
        self._by_name = {_norm_name(c.name): c for c in self.councils.values()}
        self._by_name.update({_norm_name(c.display): c for c in self.councils.values()})
        self._lower = {m.lower(): c for m, c in self.councils.items()}

    def get(self, module: str) -> Optional[Council]:
        """Accepts 'WiltshireCouncil', 'Wiltshire Council' or any case."""
        key = re.sub(r"\s+", "", module or "").lower()
        return self._lower.get(key)

    def match_area(self, lad_code: Optional[str], district: Optional[str]) -> Optional[Council]:
        if lad_code and lad_code in self.by_lad:
            cands = self.by_lad[lad_code]
            # Prefer the council dedicated to this area over multi-area ones (e.g. a shared calendar scraper)
            cands = sorted(cands, key=lambda c: (len(c.lads), c.mode == "none"))
            return cands[0]
        if district:
            hit = self._by_name.get(_norm_name(district))
            if hit:
                return hit
        return None

    def all_public(self) -> List[dict]:
        return sorted((c.public() for c in self.councils.values()), key=lambda c: c["name"].lower())
