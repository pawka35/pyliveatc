"""HTTP transport for pyliveatc.

LiveATC.net is Cloudflare-protected. Use FlareSolverr for full access:

  PYLIVEATC_MODE=flaresolverr PYLIVEATC_FLARESOLVERR=http://host:8191 python ...

Environment variables (prefix PYLIVEATC_):
  PYLIVEATC_MODE        : "flaresolverr" (recommended) | "curl_cffi" | "requests" | "wayback"
  PYLIVEATC_FLARESOLVERR: FlareSolverr base URL, e.g. http://192.168.1.116:8191
                          (do NOT include /v1 — appended automatically by unblock_requests)
  PYLIVEATC_ANON        : "1" to enable IP rotation via anon_requests
  PYLIVEATC_DELAY       : float seconds between requests (default 1.5; for
                          flaresolverr mode defaults to 0 unless explicitly set)

Access notes:
  - Scraping (search, feedindex, topfeeds): works via flaresolverr or wayback.
  - In flaresolverr mode, the first request solves CF via FlareSolverr; later
    requests reuse ``cf_clearance`` through curl_cffi (much faster).
  - Live streams (d.liveatc.net): require a valid cf_clearance cookie from
    www.liveatc.net — FlareSolverr solves this; use get_stream_url() then
    stream_bytes() with the returned session/cookies.
  - Archive downloads: LiveATC now issues short-lived signed URLs via a
    Turnstile-protected form (archive-response.php). Direct CDN links are
    rejected with "Link Required". Automation requires a full browser session
    that can solve the Turnstile widget and submit the archive form.
"""
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

import requests
from requests.models import PreparedRequest, Response
from requests.structures import CaseInsensitiveDict

BASE_URL = "https://www.liveatc.net"
ARCHIVE_BASE = "https://archive.liveatc.net"
STREAM_BASE = "https://d.liveatc.net"

_ENV = "PYLIVEATC_"
_DELAY_EXPLICIT = f"{_ENV}DELAY" in os.environ
_DEFAULT_DELAY = float(os.environ.get(f"{_ENV}DELAY", "1.5"))

_last_request: float = 0.0
_min_delay: float = _DEFAULT_DELAY
_throttle_lock = threading.Lock()
_default_transport: Optional["Transport"] = None

# Process-wide CF clearance shared across Transport clones / worker threads.
_cf_solve_lock = threading.RLock()
_cf_cookies: Dict[str, str] = {}
_cf_user_agent: Optional[str] = None


def _strip_v1(url: str) -> str:
    """Normalise FlareSolverr URL — strip trailing /v1 if present.

    unblock_requests appends /v1 itself; passing it twice causes 404.
    """
    return url.rstrip("/").removesuffix("/v1")


def set_delay(seconds: float) -> None:
    global _min_delay
    _min_delay = max(0.0, seconds)


def clear_cf_clearance() -> None:
    """Drop cached Cloudflare cookies (force the next request through FlareSolverr)."""
    global _cf_cookies, _cf_user_agent
    with _cf_solve_lock:
        _cf_cookies = {}
        _cf_user_agent = None


def _throttle(*, delay: Optional[float] = None) -> None:
    """Rate-limit requests. Sleep runs *outside* the lock so workers do not serialize."""
    global _last_request
    wait_for = 0.0
    with _throttle_lock:
        use_delay = _min_delay if delay is None else max(0.0, delay)
        now = time.monotonic()
        gap = now - _last_request
        if gap < use_delay:
            wait_for = use_delay - gap
        else:
            _last_request = now
            return
    if wait_for > 0:
        time.sleep(wait_for)
    with _throttle_lock:
        _last_request = time.monotonic()


def _full_url(url: str, params: Any) -> str:
    if not params:
        return url
    pr = PreparedRequest()
    pr.prepare_url(url, params)
    return pr.url or url


def _is_challenge(text: str) -> bool:
    try:
        from unblock_requests.session import is_challenge
        return is_challenge(text)
    except Exception:
        head = (text or "")[:1500].lower()
        return ("just a moment" in head or "cf-mitigated" in head
                or "challenge-platform" in head or "cf_chl_opt" in head)


def _make_response(url: str, *, content: bytes, status: int = 200,
                   headers: Optional[dict] = None) -> Response:
    r = Response()
    r.status_code = status
    r._content = content
    r.url = url
    r.reason = "OK"
    r.encoding = "utf-8"
    r.headers = CaseInsensitiveDict(
        headers or {"Content-Type": "text/html; charset=utf-8"}
    )
    return r


def _cookies_from_flaresolverr(solution: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in solution.get("cookies") or []:
        if isinstance(item, dict) and item.get("name"):
            out[str(item["name"])] = str(item.get("value") or "")
    return out


def _snapshot_cf() -> Tuple[Dict[str, str], Optional[str]]:
    with _cf_solve_lock:
        return dict(_cf_cookies), _cf_user_agent


def _store_cf(cookies: Dict[str, str], user_agent: Optional[str]) -> None:
    global _cf_cookies, _cf_user_agent
    with _cf_solve_lock:
        _cf_cookies = dict(cookies)
        if user_agent:
            _cf_user_agent = user_agent


class Transport:
    """Requests-compatible session wrapper with CF bypass and throttling."""

    def __init__(self, mode: Optional[str] = None, anon: bool = False,
                 flaresolverr_url: Optional[str] = None):
        self._mode = mode or os.environ.get(f"{_ENV}MODE", "curl_cffi")
        self._anon = anon or bool(os.environ.get(f"{_ENV}ANON"))
        raw_fs = flaresolverr_url or os.environ.get(f"{_ENV}FLARESOLVERR", "http://localhost:8191")
        self._flaresolverr_url = _strip_v1(raw_fs)
        self._session = None
        self._fs_timeout_ms = 120_000

    @property
    def session(self):
        if self._session is None:
            self._session = self._make_session()
        return self._session

    def _make_session(self):
        if self._mode == "wayback":
            from unblock_requests import CloudflareSession
            return CloudflareSession(mode="wayback")

        if self._mode == "flaresolverr":
            # Session used only as fallback; hot path is curl + cached clearance.
            from unblock_requests import CloudflareSession
            return CloudflareSession(mode="flaresolverr",
                                     flaresolverr_url=self._flaresolverr_url,
                                     flaresolverr_timeout_ms=self._fs_timeout_ms)

        try:
            from unblock_requests import CloudflareSession
            inner = CloudflareSession(mode=self._mode)
        except Exception:
            inner = requests.Session()

        if self._anon:
            try:
                from anon_requests import RotatingProxySession, ProxyType
                return RotatingProxySession(
                    proxy_type=ProxyType.SOCKS5,
                    session_factory=lambda: inner,
                )
            except ImportError:
                pass

        return inner

    def _flaresolverr_delay(self) -> float:
        # Cookie-reuse path is chatty; default to no delay unless user set DELAY.
        if _DELAY_EXPLICIT:
            return _min_delay
        return 0.0

    def _flaresolverr_solve(self, url: str) -> Tuple[str, Dict[str, str], Optional[str]]:
        """Solve CF for *url* via FlareSolverr; return (html, cookies, user_agent)."""
        endpoint = self._flaresolverr_url.rstrip("/")
        timeout_ms = self._fs_timeout_ms
        payload = {"cmd": "request.get", "url": url, "maxTimeout": timeout_ms}
        resp = requests.post(
            f"{endpoint}/v1", json=payload, timeout=timeout_ms / 1000 + 30
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            raise RuntimeError(
                f"FlareSolverr error: {data.get('message') or data.get('status')}"
            )
        solution = data.get("solution") or {}
        html = solution.get("response") or ""
        cookies = _cookies_from_flaresolverr(solution)
        ua = solution.get("userAgent")
        return html, cookies, ua

    def _curl_get(self, url: str, *, cookies: Dict[str, str],
                  user_agent: Optional[str], **kwargs) -> Optional[Response]:
        try:
            from curl_cffi.requests import Session as CurlSession
        except ImportError:
            return None
        headers = dict(kwargs.pop("headers", None) or {})
        if user_agent:
            headers.setdefault("User-Agent", user_agent)
        try:
            with CurlSession(impersonate="chrome") as cs:
                if headers:
                    cs.headers.update(headers)
                cr = cs.get(url, cookies=cookies, **{
                    k: v for k, v in kwargs.items()
                    if k in {"params", "timeout", "allow_redirects", "proxies", "data", "json"}
                })
            return _make_response(
                getattr(cr, "url", url),
                content=cr.content,
                status=cr.status_code,
                headers=dict(cr.headers),
            )
        except Exception:
            return None

    def _get_flaresolverr_reusing(self, url: str, params=None, **kwargs) -> Response:
        """FlareSolverr once for clearance, then curl_cffi with cached cookies."""
        full = _full_url(url, params)
        delay = self._flaresolverr_delay()

        for _ in range(2):
            cookies, ua = _snapshot_cf()
            if cookies:
                _throttle(delay=delay)
                fast = self._curl_get(full, cookies=cookies, user_agent=ua, **kwargs)
                if fast is not None and not _is_challenge(fast.text):
                    return fast
                if fast is not None and _is_challenge(fast.text):
                    clear_cf_clearance()

            with _cf_solve_lock:
                # Another worker may have populated the cache while we waited.
                cookies, ua = _snapshot_cf()
                if cookies:
                    continue
                _throttle(delay=delay)
                html, new_cookies, new_ua = self._flaresolverr_solve(full)
                if new_cookies:
                    _store_cf(new_cookies, new_ua)
                if _is_challenge(html):
                    raise RuntimeError("Cloudflare challenge served")
                return _make_response(full, content=html.encode("utf-8"))

        raise RuntimeError("Cloudflare challenge served")

    def get(self, path: str, params=None, base: str = BASE_URL, **kwargs):
        url = base + path if path.startswith("/") else path
        if self._mode == "flaresolverr":
            return self._get_flaresolverr_reusing(url, params=params, **kwargs)
        _throttle()
        return self.session.get(url, params=params, **kwargs)

    def get_text(self, path: str, params=None, base: str = BASE_URL, **kwargs) -> str:
        r = self.get(path, params=params, base=base, **kwargs)
        r.raise_for_status()
        return r.text

    def get_bytes(self, url: str, **kwargs) -> bytes:
        if self._mode == "flaresolverr":
            r = self._get_flaresolverr_reusing(url, **kwargs)
            r.raise_for_status()
            return r.content
        _throttle()
        r = self.session.get(url, **kwargs)
        r.raise_for_status()
        return r.content

    def get_cf_cookies(self) -> dict:
        """Return cached Cloudflare clearance cookies (flaresolverr reuse path)."""
        cookies, _ua = _snapshot_cf()
        if cookies:
            return cookies
        try:
            jar = self.session.cookies
            return {c.name: c.value for c in jar} if hasattr(jar, "__iter__") else {}
        except Exception:
            return {}

    def stream_bytes(self, url: str, nbytes: int = 65536, **kwargs) -> bytes:
        """Read ``nbytes`` from a streaming URL (e.g. a live Icecast stream).

        Uses curl_cffi (Chrome impersonation) which bypasses Cloudflare natively
        on d.liveatc.net.  Falls back to the configured session if curl_cffi is
        unavailable.
        """
        try:
            from curl_cffi.requests import Session as CurlSession
            _throttle(delay=self._flaresolverr_delay() if self._mode == "flaresolverr" else None)
            cookies, ua = _snapshot_cf()
            with CurlSession(impersonate="chrome") as cs:
                if ua:
                    cs.headers["User-Agent"] = ua
                r = cs.get(url, stream=True, cookies=cookies or None, **kwargs)
                r.raise_for_status()
                chunks = []
                got = 0
                for chunk in r.iter_content(chunk_size=min(nbytes, 8192)):
                    chunks.append(chunk)
                    got += len(chunk)
                    if got >= nbytes:
                        break
                r.close()
                return b"".join(chunks)[:nbytes]
        except ImportError:
            pass
        _throttle()
        r = self.session.get(url, stream=True, **kwargs)
        r.raise_for_status()
        chunks = []
        got = 0
        for chunk in r.iter_content(chunk_size=min(nbytes, 8192)):
            chunks.append(chunk)
            got += len(chunk)
            if got >= nbytes:
                break
        r.close()
        return b"".join(chunks)[:nbytes]

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None


def default_transport() -> Transport:
    global _default_transport
    if _default_transport is None:
        _default_transport = Transport()
    return _default_transport


def reset_default_transport() -> None:
    global _default_transport
    if _default_transport is not None:
        _default_transport.close()
        _default_transport = None
    clear_cf_clearance()
