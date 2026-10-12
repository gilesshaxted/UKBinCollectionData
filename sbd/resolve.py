"""Postcode -> local authority, using the free postcodes.io service (no key)."""
import logging
from typing import Optional

import requests

from . import store

log = logging.getLogger("sbd.resolve")


class PostcodeError(Exception):
    def __init__(self, msg: str, status: int):
        super().__init__(msg)
        self.status = status


def lookup_area(postcode: str) -> dict:
    """postcode must already be normalised ('SN8 1RA'). Returns {"lad", "district"}."""
    hit = store.get_postcode(postcode)
    if hit:
        return {"lad": hit["lad"], "district": hit["district"]}
    try:
        r = requests.get("https://api.postcodes.io/postcodes/" + postcode.replace(" ", ""), timeout=6,
                         headers={"User-Agent": "BinPortal/6.0"})
    except requests.RequestException as exc:
        log.warning("postcodes.io unreachable: %s", exc)
        raise PostcodeError("Could not reach the postcode service.", 503)
    if r.status_code == 404:
        raise PostcodeError("That postcode was not found.", 404)
    if r.status_code != 200:
        raise PostcodeError("The postcode service returned an error.", 503)
    res = r.json().get("result") or {}
    lad = (res.get("codes") or {}).get("admin_district")
    district = res.get("admin_district")
    store.put_postcode(postcode, lad, district)
    return {"lad": lad, "district": district}
