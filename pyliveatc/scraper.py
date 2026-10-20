"""HTML scrapers for liveatc.net pages."""
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs

from bs4 import BeautifulSoup

from .models import Feed, Frequency, TopFeed
from .transport import Transport, default_transport

FEED_TYPES = [
    "all",
    "class-b",
    "class-d",
    "us-artcc",
    "international-eu",
    "international-as",
    "hf",
    "canada",
]

# Concrete index categories (excludes the browse landing page ``all``).
FEED_INDEX_TYPES = [t for t in FEED_TYPES if t != "all"]

_MOUNT_RE = re.compile(r"/archive\.php\?m=([a-zA-Z0-9_]+)")
_ICAO_RE = re.compile(r"^([a-zA-Z]{4})", re.IGNORECASE)
_DIRECT_STREAM_RE = re.compile(r"myDirectStream\('([^']+)'\)")
_HTML5_POPUP_RE = re.compile(r"myHTML5Popup\('([^']+)','([^']*)'\)")
_GEO_PARAM_KEYS = ("state", "province", "center", "country")
_FREQ_LINE_RE = re.compile(r"^(.+?)\s*:\s*(\d{2,3}\.\d{1,3})\s*$")
_STATUS_RE = re.compile(r"Feed Status:\s*(UP|DOWN)", re.IGNORECASE)


def _icao_from_mount(mount_id: str) -> str:
    m = _ICAO_RE.match(mount_id)
    return m.group(1).upper() if m else ""


def _parse_freq_table(table) -> tuple:
    rows = table.find_all("tr")[1:]  # skip header
    freqs = []
    for row in rows:
        cols = row.find_all("td")
        if len(cols) >= 2:
            freqs.append(Frequency(title=cols[0].get_text(strip=True),
                                   frequency=cols[1].get_text(strip=True)))
    return tuple(freqs)


def _extract_mount(tag) -> Optional[str]:
    link = tag.find("a", href=_MOUNT_RE)
    if not link:
        # try any href containing archive.php?m=
        link = tag.find("a", href=lambda h: h and "/archive.php?m=" in h)
    if not link:
        return None
    m = _MOUNT_RE.search(link["href"])
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Top feeds
# ---------------------------------------------------------------------------

_ONCLICK_MOUNT_RE = re.compile(r"myHTML5Popup\('([^']+)'")


def _extract_topfeed_mount(tag) -> Optional[str]:
    """Extract the mount id from a topfeeds row.

    The live topfeeds.php page (as of 2026) does not link to archive.php
    from the ranking table — it embeds the mount id as the first argument
    of the ``onclick="myHTML5Popup('mount_id','icao')"`` handler on the
    "Tune In" play button. Older/alternate page layouts linked directly to
    ``archive.php?m=...``, so that form is tried first for back-compat.
    """
    mount_id = _extract_mount(tag)
    if mount_id:
        return mount_id
    link = tag.find("a", onclick=_ONCLICK_MOUNT_RE)
    if not link:
        return None
    m = _ONCLICK_MOUNT_RE.search(link["onclick"])
    return m.group(1) if m else None


def parse_topfeeds_html(html: str) -> List[TopFeed]:
    """Parse /topfeeds.php → list of TopFeed (ranked by listeners)."""
    soup = BeautifulSoup(html, "lxml")
    results = []

    # Top feeds page uses a table with rows: rank | listeners | title/link | ...
    table = soup.find("table", class_="topTable")
    tables = [table] if table is not None else soup.find_all("table")
    for tbl in tables:
        rows = tbl.find_all("tr")
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            # Attempt to identify rank column (numeric)
            rank_text = cols[0].get_text(strip=True)
            if not rank_text.isdigit():
                continue

            rank = int(rank_text)
            listeners_text = cols[1].get_text(strip=True).replace(",", "")
            title_col = cols[2]
            title = title_col.get_text(strip=True)

            if not listeners_text.isdigit():
                # Older layout: listeners/title columns are swapped.
                listeners_text, title_col = cols[2].get_text(strip=True).replace(",", ""), cols[1]
                title = title_col.get_text(strip=True)

            mount_id = _extract_topfeed_mount(title_col) or _extract_topfeed_mount(row)
            if not mount_id:
                continue

            try:
                listener_count = int(listeners_text)
            except ValueError:
                listener_count = 0

            # Status — look for UP/DOWN text (older layout only; the live
            # page carries no per-row status, so default to up).
            up = True
            for col in cols[3:]:
                txt = col.get_text(strip=True).upper()
                if txt in ("UP", "DOWN"):
                    up = txt == "UP"
                    break

            results.append(TopFeed(
                rank=rank,
                mount_id=mount_id,
                title=title,
                listener_count=listener_count,
                up=up,
                frequencies=(),
                icao=_icao_from_mount(mount_id),
            ))

    return sorted(results, key=lambda f: f.rank)


def fetch_topfeeds(transport: Optional[Transport] = None) -> List[TopFeed]:
    t = transport or default_transport()
    html = t.get_text("/topfeeds.php")
    return parse_topfeeds_html(html)


# ---------------------------------------------------------------------------
# Search by ICAO
# ---------------------------------------------------------------------------

def parse_search_html(html: str) -> List[Feed]:
    """Parse /search/?icao=XXXX → list of Feed."""
    soup = BeautifulSoup(html, "lxml")
    stations = soup.find_all("table", class_="body")
    freq_tables = soup.find_all("table", class_="freqTable")

    results = []
    freq_idx = 0
    for station in stations:
        strong = station.find("strong")
        if not strong:
            continue
        title = strong.get_text(strip=True)

        mount_id = _extract_mount(station)
        if not mount_id:
            continue

        font = station.find("font")
        up = font.get_text(strip=True).upper() == "UP" if font else False

        freqs = ()
        if freq_idx < len(freq_tables):
            freqs = _parse_freq_table(freq_tables[freq_idx])
        freq_idx += 1

        results.append(Feed(
            mount_id=mount_id,
            title=title,
            up=up,
            frequencies=freqs,
            icao=_icao_from_mount(mount_id),
        ))

    return results


def search(icao: str, transport: Optional[Transport] = None) -> List[Feed]:
    t = transport or default_transport()
    html = t.get_text("/search/", params={"icao": icao.upper()})
    return parse_search_html(html)


# ---------------------------------------------------------------------------
# Feed index
# ---------------------------------------------------------------------------

def parse_feedindex_geo_links(html: str) -> List[Dict[str, str]]:
    """Parse a feedindex drill-down page into query-param dicts.

    LiveATC now gates many categories behind a second selector:
    ``state`` (Class B/D), ``province`` (Canada), ``center`` (ARTCC),
    or ``country``. Returns ``[]`` when the page already lists feeds.
    """
    soup = BeautifulSoup(html, "lxml")
    results: List[Dict[str, str]] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not any(f"{key}=" in href for key in _GEO_PARAM_KEYS):
            continue
        query = href.split("?", 1)[-1] if "?" in href else ""
        if not query:
            continue
        parsed = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=False).items()}
        if "type" not in parsed:
            continue
        if not any(k in parsed for k in _GEO_PARAM_KEYS):
            continue
        key = tuple(sorted(parsed.items()))
        if key in seen:
            continue
        seen.add(key)
        results.append(parsed)
    return results


def _parse_feedindex_freq_cell(td) -> tuple:
    """Parse the right-hand frequency cell (``Title: 124.050`` lines)."""
    if td is None:
        return ()
    freqs = []
    for line in td.stripped_strings:
        m = _FREQ_LINE_RE.match(line)
        if m:
            freqs.append(Frequency(title=m.group(1).strip(), frequency=m.group(2)))
    return tuple(freqs)


def _icao_from_feed_row(row, mount_id: str) -> str:
    """Prefer the myHTML5Popup ICAO arg, then ``<a name>``, then mount prefix."""
    popup = row.find("a", onclick=_HTML5_POPUP_RE)
    if popup:
        m = _HTML5_POPUP_RE.search(popup.get("onclick", ""))
        if m:
            candidate = m.group(2).strip()
            if re.fullmatch(r"[A-Za-z]{4}", candidate):
                return candidate.upper()
    named = row.find("a", attrs={"name": True})
    if named:
        candidate = (named.get("name") or "").strip()
        if re.fullmatch(r"[A-Za-z]{4}", candidate):
            return candidate.upper()
    return _icao_from_mount(mount_id)


def _parse_feedindex_modern(soup) -> List[Feed]:
    """Parse the current feedindex list layout (post-2024).

    Each feed is a ``<tr>`` with an ``archive.php?m=`` link, a
    ``myDirectStream('mount')`` title, Feed Status UP/DOWN, and an
    optional frequency cell in the second ``<td>``.
    """
    results: List[Feed] = []
    seen = set()
    for link in soup.find_all("a", href=_MOUNT_RE):
        m = _MOUNT_RE.search(link.get("href", ""))
        if not m:
            continue
        mount_id = m.group(1)
        if mount_id in seen:
            continue

        row = link.find_parent("tr")
        if row is None:
            continue

        title = ""
        stream_link = row.find("a", onclick=_DIRECT_STREAM_RE)
        if stream_link:
            title = stream_link.get_text(strip=True)
        if not title:
            strong = row.find("strong")
            title = strong.get_text(strip=True) if strong else ""
        if not title:
            continue

        seen.add(mount_id)

        status_m = _STATUS_RE.search(row.get_text(" ", strip=True))
        up = status_m.group(1).upper() == "UP" if status_m else False

        tds = row.find_all("td", recursive=False)
        freqs = _parse_feedindex_freq_cell(tds[1] if len(tds) >= 2 else None)

        results.append(Feed(
            mount_id=mount_id,
            title=title,
            up=up,
            frequencies=freqs,
            icao=_icao_from_feed_row(row, mount_id),
        ))
    return results


def _parse_feedindex_legacy(soup) -> List[Feed]:
    """Parse the older search-like ``table.body`` / ``freqTable`` layout."""
    stations = soup.find_all("table", class_="body")
    freq_tables = soup.find_all("table", class_="freqTable")

    results = []
    freq_idx = 0
    for station in stations:
        strong = station.find("strong")
        if not strong:
            continue
        title = strong.get_text(strip=True)

        mount_id = _extract_mount(station)
        if not mount_id:
            continue

        font = station.find("font")
        up = font.get_text(strip=True).upper() == "UP" if font else False

        freqs = ()
        if freq_idx < len(freq_tables):
            freqs = _parse_freq_table(freq_tables[freq_idx])
        freq_idx += 1

        results.append(Feed(
            mount_id=mount_id,
            title=title,
            up=up,
            frequencies=freqs,
            icao=_icao_from_mount(mount_id),
        ))
    return results


def parse_feedindex_html(html: str) -> List[Feed]:
    """Parse /feedindex.php?type=... → list of Feed.

    Supports the current list layout and the legacy ``table.body`` pages
    (same structure as ``/search/``). Geo selector pages yield an empty
    list — use :func:`parse_feedindex_geo_links` + :func:`fetch_feedindex`
    to crawl them.
    """
    soup = BeautifulSoup(html, "lxml")
    modern = _parse_feedindex_modern(soup)
    if modern:
        return modern
    return _parse_feedindex_legacy(soup)


def _clone_transport(base: Optional[Transport]) -> Transport:
    """Build a per-thread Transport from *base* settings (or defaults)."""
    if base is None:
        return Transport()
    if type(base) is Transport:
        return Transport(
            mode=base._mode,
            anon=base._anon,
            flaresolverr_url=base._flaresolverr_url,
        )
    # Custom/stub transports are reused as-is (caller must be thread-safe).
    return base  # type: ignore[return-value]


def _fetch_feedindex_html(params: Dict[str, str],
                          transport: Optional[Transport],
                          *,
                          own_transport: bool) -> str:
    t = _clone_transport(transport) if own_transport else (transport or default_transport())
    try:
        return t.get_text("/feedindex.php", params=params)
    finally:
        if own_transport and type(t) is Transport:
            t.close()


def _fetch_feedindex_html_many(
    pages: Sequence[Dict[str, str]],
    transport: Optional[Transport],
    workers: int,
) -> List[Tuple[Dict[str, str], str]]:
    """GET many feedindex URLs; parallel when ``workers > 1``."""
    if not pages:
        return []
    workers = max(1, int(workers))
    if workers == 1 or len(pages) == 1:
        return [
            (params, _fetch_feedindex_html(params, transport, own_transport=False))
            for params in pages
        ]

    results: List[Tuple[Dict[str, str], str]] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(pages))) as pool:
        futs = {
            pool.submit(
                _fetch_feedindex_html, params, transport, own_transport=True
            ): params
            for params in pages
        }
        for fut in as_completed(futs):
            params = futs[fut]
            results.append((params, fut.result()))
    return results


def _dedupe_feeds(feeds: Iterable[Feed]) -> List[Feed]:
    seen = set()
    out: List[Feed] = []
    for feed in feeds:
        if feed.mount_id in seen:
            continue
        seen.add(feed.mount_id)
        out.append(feed)
    return out


def fetch_feedindex(feed_type: str = "all",
                    transport: Optional[Transport] = None,
                    workers: int = 1) -> List[Feed]:
    """Fetch feeds for a feedindex category.

    ``feed_type="all"`` unions every concrete category in
    :data:`FEED_INDEX_TYPES`. Categories that show a state/province/center
    selector are crawled automatically.

    ``workers`` > 1 fetches landing and geo pages concurrently via a
    thread pool (each worker uses its own :class:`Transport`). Useful with
    FlareSolverr, where each page solve is slow but independent.
    """
    workers = max(1, int(workers))
    t = transport or default_transport()
    types = list(FEED_INDEX_TYPES) if feed_type == "all" else [feed_type]

    landing_pages = [{"type": ft} for ft in types]
    landings = _fetch_feedindex_html_many(landing_pages, t, workers)

    leaf_pages: List[Dict[str, str]] = []
    feeds: List[Feed] = []
    for _params, html in landings:
        geos = parse_feedindex_geo_links(html)
        if geos:
            leaf_pages.extend(geos)
        else:
            feeds.extend(parse_feedindex_html(html))

    if leaf_pages:
        for _params, html in _fetch_feedindex_html_many(leaf_pages, t, workers):
            feeds.extend(parse_feedindex_html(html))

    return _dedupe_feeds(feeds)


def iter_feedindex(feed_type: str = "all",
                   transport: Optional[Transport] = None,
                   workers: int = 1) -> Iterator[Feed]:
    """Yield all feeds for the given type. Alias for fetch_feedindex for API symmetry."""
    yield from fetch_feedindex(feed_type=feed_type, transport=transport, workers=workers)


# Capture full <audio src>, including ?nocache=… query (needed for direct play).
_AUDIO_SRC_RE = re.compile(
    r'<audio[^>]+src="(https?://[^"]+liveatc\.net/[^"]+)"',
    re.IGNORECASE,
)


def get_stream_url(mount_id: str, icao: str = "",
                   transport: Optional[Transport] = None) -> Optional[str]:
    """Return the playable live stream URL for a mount point.

    LiveATC embeds the real edge URL (e.g.
    ``https://s1-bos.liveatc.net/kjfk_gnd?nocache=…``) inside the
    ``hlisten.php`` popup player page.  Prefer this over the
    ``http://d.liveatc.net/{mount}`` gateway when you need a URL that
    opens directly in a player.

    Returns the stream URL string, or None if the page was unreachable /
    unparseable.
    """
    t = transport or default_transport()
    try:
        html = t.get_text(
            "/hlisten.php",
            params={"mount": mount_id, "icao": icao or mount_id[:4]},
        )
    except Exception:
        return None
    m = _AUDIO_SRC_RE.search(html)
    if m:
        return m.group(1)
    return None


def iter_all_feeds(transport: Optional[Transport] = None,
                   workers: int = 1) -> Iterator[Feed]:
    """Yield every feed across all known feed types (deduped by mount_id).

    ``workers`` is passed to :func:`fetch_feedindex` for parallel page fetches.
    """
    try:
        yield from fetch_feedindex("all", transport=transport, workers=workers)
        return
    except Exception:
        pass
    # Per-type fallback so one broken category does not abort the whole crawl.
    seen = set()
    for feed_type in FEED_INDEX_TYPES:
        try:
            for feed in fetch_feedindex(
                feed_type, transport=transport, workers=workers
            ):
                if feed.mount_id not in seen:
                    seen.add(feed.mount_id)
                    yield feed
        except Exception:
            continue
