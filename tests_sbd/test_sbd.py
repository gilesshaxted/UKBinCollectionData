import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SBD_DATA_DIR"] = "/tmp/sbd-test"

from fastapi.testclient import TestClient  # noqa: E402

from sbd import addresses, config, resolve, store  # noqa: E402
import sbd_server  # noqa: E402

config.MODULE_GAP = 0
BINS = [{"bin": "Household Waste", "date": "20/10/2026"}, {"bin": "Recycling", "date": "27/10/2026"}]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    store.reset()
    store.connect(":memory:")
    monkeypatch.setattr(config, "SELENIUM_URL", "")
    sbd_server.engine._last_scrape.clear()
    sbd_server._hits.clear()
    calls = []

    def fake(council, spec):
        calls.append((council.module, spec.uprn, spec.postcode, spec.number))
        return {"bins": BINS}

    monkeypatch.setattr(sbd_server.engine, "run_scraper", fake)
    monkeypatch.setattr(sbd_server.engine, "enqueue", lambda spec: calls.append(("ENQ", spec.module)))
    yield calls


client = TestClient(sbd_server.app)


class Resp:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self._b = body

    def json(self):
        return self._b


def test_registry_counts():
    r = sbd_server.registry
    assert len(r.councils) >= 300
    w = r.get("Wiltshire Council")
    assert w.module == "WiltshireCouncil" and w.mode == "uprn" and "postcode" in w.fields and w.supported
    assert r.get("wiltshirecouncil") is w


def test_selenium_flagged_and_unsupported():
    leeds = sbd_server.registry.get("LeedsCityCouncil")
    assert leeds.selenium and leeds.reason == "needs_browser"
    r = client.post("/bins", json={"module": "LeedsCityCouncil", "postcode": "LS6 2SE", "house_number": "1", "uprn": "72506983"})
    assert r.status_code == 501


def test_selenium_enabled_when_grid_configured(monkeypatch):
    monkeypatch.setattr(config, "SELENIUM_URL", "http://selenium:4444")
    assert sbd_server.registry.get("LeedsCityCouncil").supported


def test_resolve_wiltshire(monkeypatch):
    monkeypatch.setattr(resolve.requests, "get", lambda *a, **k: Resp(200, {"result": {"admin_district": "Wiltshire", "codes": {"admin_district": "E06000054"}}}))
    j = client.get("/resolve", params={"postcode": "sn81ra"}).json()
    assert j["postcode"] == "SN8 1RA" and j["council"]["module"] == "WiltshireCouncil" and j["council"]["supported"]
    # second call served from the store, no HTTP
    monkeypatch.setattr(resolve.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should be cached")))
    assert client.get("/resolve", params={"postcode": "SN8 1RA"}).json()["council"]["module"] == "WiltshireCouncil"


def test_resolve_name_fallback_and_errors(monkeypatch):
    monkeypatch.setattr(resolve.requests, "get", lambda *a, **k: Resp(200, {"result": {"admin_district": "Bath and North East Somerset", "codes": {"admin_district": "ZZZ"}}}))
    assert client.get("/resolve", params={"postcode": "BA1 1AA"}).json()["council"]["module"] == "BathAndNorthEastSomersetCouncil"
    monkeypatch.setattr(resolve.requests, "get", lambda *a, **k: Resp(404, {}))
    assert client.get("/resolve", params={"postcode": "ZZ9 9ZZ"}).status_code == 404
    assert client.get("/resolve", params={"postcode": "nope"}).status_code == 400


def test_every_lad_resolves_to_a_council():
    r = sbd_server.registry
    assert len(r.by_lad) > 250
    for lad in list(r.by_lad)[:50]:
        assert r.match_area(lad, None) is not None


WILTS = {"IsSuccess": True, "Model": {"PostcodeAddresses": [{"UPRN": "100120981455", "Address": "2 Bray Street, Avebury. SN8 1RA"}, {"UPRN": "", "Address": "bad"}]}}


def test_addresses_council_then_store(monkeypatch):
    n = {"c": 0}

    def post(*a, **k):
        n["c"] += 1
        return Resp(200, WILTS)

    monkeypatch.setattr(addresses.requests, "post", post)
    j = client.post("/addresses", json={"postcode": "SN8 1RA", "module": "Wiltshire Council"}).json()
    assert j["source"] == "council" and j["addresses"] == [{"uprn": "100120981455", "address": "2 Bray Street, Avebury. SN8 1RA"}]
    j2 = client.post("/addresses", json={"postcode": "SN8 1RA", "module": "WiltshireCouncil"}).json()
    assert "stored" in j2["source"] and n["c"] == 1


def test_addresses_os_key_not_stored(monkeypatch):
    monkeypatch.setattr(addresses.requests, "get", lambda *a, **k: Resp(200, {"results": [{"DPA": {"UPRN": "1234567890", "ADDRESS": "1 TEST ROAD"}}]}))
    body = {"postcode": "LS6 2SE", "module": "LeedsCityCouncil", "os_api_key": "abcdefghij12"}
    assert client.post("/addresses", json=body).json()["source"] == "os"
    assert store.get_addresses("LS6 2SE", "LeedsCityCouncil") is None  # OS data is not kept on the server
    monkeypatch.setattr(addresses.requests, "get", lambda *a, **k: Resp(401, {}))
    j = client.post("/addresses", json={"postcode": "LS6 2SE", "module": "LeedsCityCouncil"}).json()
    assert j["manual"] is True and j["addresses"] == []


def test_adapter_failure_falls_back_to_manual(monkeypatch):
    def boom(*a, **k):
        raise addresses.requests.ConnectionError("down")

    monkeypatch.setattr(addresses.requests, "post", boom)
    j = client.post("/addresses", json={"postcode": "SN8 1RA", "module": "WiltshireCouncil"}).json()
    assert j["manual"] is True


def test_bins_fresh_cache_and_validation(fresh):
    body = {"module": "WiltshireCouncil", "uprn": "100120981455", "postcode": "SN8 1RA"}
    j = client.post("/bins", json=body).json()
    assert j["bins"] == BINS and j["meta"]["status"] == "fresh"
    j2 = client.post("/bins", json=body).json()
    assert j2["meta"]["status"] == "fresh"
    assert [c for c in fresh if c[0] == "WiltshireCouncil"] == [("WiltshireCouncil", "100120981455", "SN8 1RA", "")]  # scraped once
    assert client.post("/bins", json={"module": "WiltshireCouncil", "uprn": "100120981455"}).status_code == 400
    assert client.post("/bins", json={"module": "NopeCouncil"}).status_code == 400
    assert client.post("/bins", json={"module": "WiltshireCouncil; rm -rf /", "uprn": "1"}).status_code == 400


def test_stale_while_revalidate_and_error_fallback(fresh, monkeypatch):
    body = {"module": "WiltshireCouncil", "uprn": "100120981455", "postcode": "SN8 1RA"}
    client.post("/bins", json=body)
    key = [r for r in store._q("SELECT key FROM bins")][0]["key"]
    store._x("UPDATE bins SET fetched=?", (time.time() - 3 * 86400,))
    j = client.post("/bins", json=body).json()
    assert j["meta"]["status"] == "stale" and ("ENQ", "WiltshireCouncil") in fresh
    # very old and council now failing: last good data still returned
    store._x("UPDATE bins SET fetched=?", (time.time() - 30 * 86400,))
    from sbd.engine import ScrapeError

    def bad(c, s):
        raise ScrapeError("site down", "failed")

    monkeypatch.setattr(sbd_server.engine, "run_scraper", bad)
    j = client.post("/bins", json=body).json()
    assert j["meta"]["status"] == "stale-error" and j["bins"] == BINS
    row = store.get_bins_row(key)
    assert row["fail_count"] == 1 and row["next_refresh"] > time.time() + 3600  # backed off


def test_no_data_and_failure_gives_error(monkeypatch):
    from sbd.engine import ScrapeError

    def bad(c, s):
        raise ScrapeError("The council system did not recognise this address.", "not_found")

    monkeypatch.setattr(sbd_server.engine, "run_scraper", bad)
    r = client.post("/bins", json={"module": "WiltshireCouncil", "uprn": "1", "postcode": "SN8 1RA"})
    assert r.status_code == 404


def test_empty_result_does_not_overwrite_good_data(fresh, monkeypatch):
    body = {"module": "WiltshireCouncil", "uprn": "100120981455", "postcode": "SN8 1RA"}
    client.post("/bins", json=body)
    store._x("UPDATE bins SET fetched=?", (time.time() - 30 * 86400,))
    monkeypatch.setattr(sbd_server.engine, "run_scraper", lambda c, s: {"bins": []})
    j = client.post("/bins", json=body).json()
    assert j["bins"] == BINS and j["meta"]["status"] == "stale-error"


def test_change_detection(fresh, monkeypatch):
    body = {"module": "WiltshireCouncil", "uprn": "100120981455", "postcode": "SN8 1RA"}
    client.post("/bins", json=body)
    first = client.post("/bins", json=body).json()["meta"]["changed_at"]
    time.sleep(1.1)
    monkeypatch.setattr(sbd_server.engine, "run_scraper", lambda c, s: {"bins": [{"bin": "Household Waste", "date": "21/10/2026"}]})
    j = client.post("/bins", json={**body, "refresh": True}).json()
    assert j["meta"]["changed_at"] > first


def test_details_council_needs_number(fresh):
    r = sbd_server.registry
    c = next(c for c in r.councils.values() if c.mode == "details" and not c.selenium and "house_number" in c.fields and "postcode" in c.fields)
    assert client.post("/bins", json={"module": c.module, "postcode": "SN8 1RA"}).status_code == 400
    ok = client.post("/bins", json={"module": c.module, "postcode": "SN8 1RA", "house_number": "12A"})
    assert ok.status_code == 200 and fresh[-1] == (c.module, "", "SN8 1RA", "12A")


def test_legacy_get_bins(fresh):
    j = client.post("/get_bins", json={"module": "Wiltshire Council", "address_data": "100120981455 SN8 1RA"}).json()
    assert j["bins"] == BINS
    assert fresh[-1] == ("WiltshireCouncil", "100120981455", "SN8 1RA", "")


def test_scheduler_refreshes_due_and_expires_idle(fresh, monkeypatch):
    monkeypatch.setattr(sbd_server.engine, "enqueue", lambda spec: fresh.append(("ENQ", spec.uprn)))
    client.post("/bins", json={"module": "WiltshireCouncil", "uprn": "100120981455", "postcode": "SN8 1RA"})
    assert sbd_server.engine.tick() == 0  # not due yet
    store._x("UPDATE bins SET next_refresh=?", (time.time() - 5,))
    assert sbd_server.engine.tick() == 1 and ("ENQ", "100120981455") in fresh
    store._x("UPDATE bins SET last_requested=?", (time.time() - 40 * 86400,))
    assert sbd_server.engine.tick() == 0
    assert store.stats()["subscribed"] == 0  # nobody asked for 30 days, so refreshing stops


def test_background_worker_end_to_end(fresh, monkeypatch):
    # un-patch enqueue so the real worker thread runs
    import importlib
    from sbd.engine import Engine
    eng = Engine(sbd_server.registry, runner=lambda c, s: {"bins": BINS})
    from sbd.engine import Spec
    spec = Spec("WiltshireCouncil", "100120981455", "SN8 1RA")
    store.touch_request(spec.key, spec.module, spec.uprn, spec.postcode, "")
    eng.enqueue(spec)
    for _ in range(50):
        row = store.get_bins_row(spec.key)
        if row and row["data"]:
            break
        time.sleep(0.1)
    assert json.loads(store.get_bins_row(spec.key)["data"]) == BINS


def test_ics_feed_cache_only(fresh):
    q = {"module": "WiltshireCouncil", "uprn": "100120981455", "postcode": "SN8 1RA"}
    empty = client.get("/feed.ics", params=q).text
    assert "Bin dates are being fetched" in empty and ("ENQ", "WiltshireCouncil") in fresh  # no scrape on the feed request
    client.post("/bins", json={k: v for k, v in q.items()})
    ics = client.get("/feed.ics", params=q).text
    assert ics.count("BEGIN:VEVENT") == 2 and "DTSTART;VALUE=DATE:20261020" in ics and "SUMMARY:Household Waste" in ics


def test_token_and_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "s3cret")
    assert client.get("/councils").status_code == 401
    assert client.get("/councils", headers={"x-sbd-token": "s3cret"}).status_code == 200
    assert client.get("/health").status_code == 200
    monkeypatch.setattr(config, "API_TOKEN", "")
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 3)
    codes = [client.get("/councils").status_code for _ in range(5)]
    assert codes[-1] == 429


def test_status_and_councils_listing():
    s = client.get("/status").json()
    assert s["councils"] >= 300 and s["needs_browser"] > 50 and s["selenium_configured"] is False
    names = client.get("/get_councils").json()["councils"]
    assert "Wiltshire Council" in names
