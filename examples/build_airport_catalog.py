"""Build a JSON catalog of all airports with their feeds and frequencies.

Useful as a static snapshot for offline use, chatbots, or building
a searchable airport database. Writes airports.json.

By default each feed's ``stream_url`` is the *playable* edge URL from
``hlisten.php`` (e.g. ``https://s1-bos.liveatc.net/unnt?nocache=…``),
not the ``d.liveatc.net`` gateway. The gateway is kept as ``gateway_url``.

Catalog schema:
    {
      "KJFK": {
        "icao": "KJFK",
        "feeds": [
          {"mount_id": "kjfk9_s", "title": "...", "up": true,
           "stream_url": "https://s1-bos.liveatc.net/kjfk9_s?nocache=…",
           "gateway_url": "http://d.liveatc.net/kjfk9_s",
           "frequencies": [...]}
        ]
      }, ...
    }

Requires FlareSolverr for liveatc.net::

    export PYLIVEATC_MODE=flaresolverr
    export PYLIVEATC_FLARESOLVERR=http://localhost:8191
    python examples/build_airport_catalog.py --workers 8
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional

from pyliveatc import Feed, get_stream_url, iter_all_feeds
from pyliveatc.transport import Transport, default_transport, reset_default_transport


def _feed_row(feed: Feed, listen_url: Optional[str] = None) -> dict:
    row = feed.to_dict()
    row["gateway_url"] = row["stream_url"]  # http://d.liveatc.net/{mount}
    if listen_url:
        row["stream_url"] = listen_url
    return row


def _resolve_one(feed: Feed, transport: Transport) -> tuple[str, Optional[str]]:
    url = get_stream_url(feed.mount_id, icao=feed.icao or "", transport=transport)
    return feed.mount_id, url


def resolve_listen_urls(
    feeds: List[Feed],
    *,
    workers: int = 8,
    transport: Optional[Transport] = None,
) -> Dict[str, str]:
    """Map mount_id → playable sN-*.liveatc.net URL (parallel hlisten fetches)."""
    t = transport or default_transport()
    workers = max(1, int(workers))
    out: Dict[str, str] = {}
    total = len(feeds)
    done = 0
    ok = 0

    def _one(feed: Feed):
        # Per-thread Transport clone shares process-wide CF cookie cache.
        local = Transport(
            mode=t._mode,
            anon=t._anon,
            flaresolverr_url=t._flaresolverr_url,
        )
        try:
            return _resolve_one(feed, local)
        finally:
            local.close()

    print(f"Resolving playable stream URLs for {total} feeds (workers={workers})…")
    if workers == 1:
        for feed in feeds:
            mid, url = _resolve_one(feed, t)
            done += 1
            if url:
                out[mid] = url
                ok += 1
            if done % 25 == 0 or done == total:
                print(f"  {done}/{total}  resolved={ok}", flush=True)
        return out

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one, f): f for f in feeds}
        for fut in as_completed(futs):
            mid, url = fut.result()
            done += 1
            if url:
                out[mid] = url
                ok += 1
            if done % 25 == 0 or done == total:
                print(f"  {done}/{total}  resolved={ok}", flush=True)
    return out


def build_catalog(
    out_path: str = "airports.json",
    workers: int = 8,
    resolve_streams: bool = True,
) -> dict:
    reset_default_transport()
    feeds = list(iter_all_feeds(workers=workers))
    print(f"Collected {len(feeds)} feeds from feedindex")

    listen: Dict[str, str] = {}
    if resolve_streams and feeds:
        listen = resolve_listen_urls(feeds, workers=workers)

    airports: dict = defaultdict(lambda: {"icao": "", "feeds": []})
    for feed in feeds:
        icao = feed.icao or feed.mount_id[:4].upper()
        airports[icao]["icao"] = icao
        airports[icao]["feeds"].append(
            _feed_row(feed, listen.get(feed.mount_id))
        )

    catalog = dict(sorted(airports.items()))
    Path(out_path).write_text(json.dumps(catalog, indent=2, ensure_ascii=False))
    n_listen = sum(
        1
        for a in catalog.values()
        for f in a["feeds"]
        if f.get("stream_url", "").startswith("https://")
        and "d.liveatc.net" not in f.get("stream_url", "")
    )
    print(
        f"Catalog: {len(catalog)} airports, "
        f"{sum(len(v['feeds']) for v in catalog.values())} feeds, "
        f"{n_listen} playable stream_url"
    )
    print(f"Saved to {out_path}")
    return catalog


def enrich_existing(
    in_path: str,
    out_path: str,
    workers: int = 8,
) -> dict:
    """Re-resolve stream_url on an existing airports.json without re-crawling."""
    reset_default_transport()
    catalog = json.loads(Path(in_path).read_text(encoding="utf-8"))
    feeds: List[Feed] = []
    for data in catalog.values():
        for row in data.get("feeds", []):
            feeds.append(
                Feed(
                    mount_id=row["mount_id"],
                    title=row.get("title", ""),
                    up=bool(row.get("up", False)),
                    icao=row.get("icao") or data.get("icao") or "",
                    frequencies=(),  # not needed for hlisten
                )
            )
    # Dedupe by mount_id preserving first
    seen = set()
    unique: List[Feed] = []
    for f in feeds:
        if f.mount_id in seen:
            continue
        seen.add(f.mount_id)
        unique.append(f)

    listen = resolve_listen_urls(unique, workers=workers)
    for data in catalog.values():
        for row in data.get("feeds", []):
            if "gateway_url" not in row:
                row["gateway_url"] = row.get("stream_url") or (
                    f"http://d.liveatc.net/{row['mount_id']}"
                )
            url = listen.get(row["mount_id"])
            if url:
                row["stream_url"] = url

    Path(out_path).write_text(json.dumps(catalog, indent=2, ensure_ascii=False))
    n_listen = sum(
        1
        for a in catalog.values()
        for f in a["feeds"]
        if "d.liveatc.net" not in f.get("stream_url", "")
        and f.get("stream_url", "").startswith("https://")
    )
    print(f"Updated {n_listen} playable URLs → {out_path}")
    return catalog


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="airports.json")
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel page fetches (default: 8)",
    )
    parser.add_argument(
        "--resolve-streams",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resolve playable sN-*.liveatc.net URLs via hlisten.php (default: on)",
    )
    parser.add_argument(
        "--from-json",
        default=None,
        help="Enrich an existing airports.json (skip feedindex crawl)",
    )
    args = parser.parse_args()

    if args.from_json:
        catalog = enrich_existing(args.from_json, args.out, workers=args.workers)
    else:
        catalog = build_catalog(
            args.out,
            workers=args.workers,
            resolve_streams=args.resolve_streams,
        )

    by_feeds = sorted(
        catalog.items(), key=lambda kv: len(kv[1]["feeds"]), reverse=True
    )
    print("\nTop 10 airports by feed count:")
    for icao, data in by_feeds[:10]:
        online = sum(1 for f in data["feeds"] if f["up"])
        sample = next(
            (
                f["stream_url"]
                for f in data["feeds"]
                if "d.liveatc.net" not in f.get("stream_url", "")
            ),
            data["feeds"][0]["stream_url"] if data["feeds"] else "",
        )
        print(f"  {icao}  {len(data['feeds'])} feeds ({online} online)  e.g. {sample}")
