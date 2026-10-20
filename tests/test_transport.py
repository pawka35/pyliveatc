"""Transport clearance-reuse and throttle behaviour."""
import threading
import time

import pyliveatc.transport as tr
from pyliveatc.transport import Transport, clear_cf_clearance, set_delay


def setup_function():
    clear_cf_clearance()
    set_delay(0.0)


def teardown_function():
    clear_cf_clearance()
    set_delay(0.0)


def test_throttle_does_not_hold_lock_during_sleep():
    """Other threads must be able to enter _throttle while one is sleeping."""
    set_delay(0.2)
    entered = []
    barrier = threading.Barrier(2)

    def worker(name):
        barrier.wait()
        tr._throttle()
        entered.append((name, time.monotonic()))

    t0 = time.monotonic()
    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    elapsed = time.monotonic() - t0

    assert len(entered) == 2
    # If sleep were inside the lock, total wall time would be ~0.4s serialized
    # start gaps. With sleep outside, both can proceed with overlap — wall
    # clock should stay well under 0.55s for 0.2s delay.
    assert elapsed < 0.55


def test_flaresolverr_reuses_cached_cookies(monkeypatch):
    clear_cf_clearance()
    solves = {"n": 0}
    curls = {"n": 0}

    def fake_solve(self, url):
        solves["n"] += 1
        return (
            "<html><body>ok page</body></html>",
            {"cf_clearance": "tok", "other": "1"},
            "Mozilla/5.0 TestUA",
        )

    def fake_curl(self, url, *, cookies, user_agent, **kwargs):
        curls["n"] += 1
        assert cookies.get("cf_clearance") == "tok"
        assert user_agent == "Mozilla/5.0 TestUA"
        return tr._make_response(url, content=b"<html><body>fast</body></html>")

    monkeypatch.setattr(Transport, "_flaresolverr_solve", fake_solve)
    monkeypatch.setattr(Transport, "_curl_get", fake_curl)

    t = Transport(mode="flaresolverr", flaresolverr_url="http://localhost:8191")
    assert "ok page" in t.get_text("/feedindex.php", params={"type": "hf"})
    assert solves["n"] == 1
    assert curls["n"] == 0  # first response comes from FlareSolverr HTML

    assert "fast" in t.get_text("/feedindex.php", params={"type": "hf"})
    assert solves["n"] == 1
    assert curls["n"] == 1

    t2 = Transport(mode="flaresolverr", flaresolverr_url="http://localhost:8191")
    assert "fast" in t2.get_text("/topfeeds.php")
    assert solves["n"] == 1
    assert curls["n"] == 2


def test_stale_clearance_triggers_resolve(monkeypatch):
    clear_cf_clearance()
    solves = {"n": 0}

    def fake_solve(self, url):
        solves["n"] += 1
        return (
            f"<html>solved-{solves['n']}</html>",
            {"cf_clearance": f"tok{solves['n']}"},
            "UA",
        )

    def fake_curl(self, url, *, cookies, user_agent, **kwargs):
        # First cached attempt looks like a CF interstitial.
        if cookies.get("cf_clearance") == "tok1":
            return tr._make_response(
                url, content=b"<html>Just a moment... challenge-platform</html>"
            )
        return tr._make_response(url, content=b"<html>ok</html>")

    monkeypatch.setattr(Transport, "_flaresolverr_solve", fake_solve)
    monkeypatch.setattr(Transport, "_curl_get", fake_curl)

    t = Transport(mode="flaresolverr")
    assert "solved-1" in t.get_text("/search/", params={"icao": "KJFK"})
    # Stale clearance → challenge on curl → second FlareSolverr solve.
    assert "solved-2" in t.get_text("/search/", params={"icao": "KJFK"})
    assert solves["n"] == 2
