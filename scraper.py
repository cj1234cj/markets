#!/usr/bin/env python3
"""
Daily snapshot of open prediction markets.

Sources (all public, no API key needed):
  kalshi      - https://api.elections.kalshi.com/trade-api/v2
  polymarket  - https://gamma-api.polymarket.com
  manifold    - https://api.manifold.markets/v0   (play money)
  predictit   - https://www.predictit.org/api/marketdata/all/

Every market/contract becomes one row in a common schema. Prices are
normalised to 0-1 (i.e. $ per $1 contract / implied probability).

Usage:
  python scraper.py                          # all sources -> ./data/YYYY-MM-DD/*.csv.gz
  python scraper.py --db markets.db          # also append to SQLite for history queries
  python scraper.py --sources kalshi polymarket
  python scraper.py --site site               # also refresh the dashboard data (default)
  python scraper.py --keyword "fed"          # keep only matching titles
  python scraper.py --all-markets --horizon-days 0   # every open market, any date

By default only *mention* markets ("What will X say...", 'Will Trump say "Y"...')
whose decision date is within --horizon-days (30) are kept.
"""
import argparse
import csv
import datetime as dt
import gzip
import json
import logging
import os
import re
import sqlite3
import time

import requests

from edge import compute_edges

log = logging.getLogger("scraper")

FIELDS = [
    "snapshot_utc", "source", "market_id", "event_id", "title", "outcome",
    "yes_price", "yes_bid", "yes_ask", "volume", "volume_24h",
    "open_interest", "liquidity", "close_time", "url", "real_money",
    # structure used by the edge model (edge.py)
    "exclusive", "strike_type", "floor_strike", "cap_strike",
    # when the outcome is actually known (Kalshi close_time can be a far-off backstop)
    "decision_time",
]

session = requests.Session()
session.headers.update({"User-Agent": "prediction-market-snapshot/1.0", "Accept": "application/json"})


# ---------------------------------------------------------------- helpers
def get_json(url, params=None, tries=5):
    """GET with exponential backoff on 429/5xx/network errors."""
    for attempt in range(tries):
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            wait = 2 ** attempt
            log.warning("%s failed (%s), retry in %ss", url, e, wait)
            time.sleep(wait)
    raise RuntimeError(f"Giving up on {url}")


def num(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def json_list(x):
    """Polymarket returns some lists as JSON-encoded strings."""
    if isinstance(x, list):
        return x
    try:
        return json.loads(x) if x else []
    except (TypeError, ValueError):
        return []


def kalshi_price(m, key):
    """Kalshi prices: newer `<key>_dollars` string, else legacy integer cents."""
    d = num(m.get(f"{key}_dollars"))
    if d is not None:
        return d
    c = num(m.get(key))
    return c / 100 if c is not None else None


def ms_to_iso(ms):
    """Epoch ms -> ISO string; None for missing or out-of-range values
    (Manifold has markets closing in year 10000+, which datetime can't represent)."""
    if not ms:
        return None
    try:
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------- mention filter
# Kalshi: mention markets live in series like KXTRUMPMENTION, KXEARNINGSMENTIONTSLA,
# KXTRUMPSAY... The title check drops series that only contain "SAY" by accident.
KALSHI_MENTION_TITLE = re.compile(r"\b(say|says|said|mention|mentions|mentioned)\b", re.I)
# Other venues: titles like 'Will Trump say "X"', '"X" be said during...', 'tweet "X"'.
# Bare "say"/"said" also matches song titles and names (e.g. "Said El Mala").
MENTION_TITLE = re.compile(
    r'\b(say|says|tweet|tweets|post|posts)\s+["“]'
    r'|["”]\s+be\s+said\b'
    r'|\bwhat will .+ say\b'
    r'|\bmention(s|ed)?\b', re.I)


def is_mention(r):
    title = r.get("title") or ""
    if r["source"] == "kalshi":
        series = str(r["market_id"]).split("-")[0]
        return ("MENTION" in series or "SAY" in series) and bool(KALSHI_MENTION_TITLE.search(title))
    return bool(MENTION_TITLE.search(title))


KALSHI_TICKER_DATE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})$")


def decision_time(r):
    """Best guess at when the outcome is known. Kalshi mention tickers embed the
    event date (KXEARNINGSMENTIONSBUX-26OCT29-...), while close_time is often a
    backstop months later; take whichever is earlier. Elsewhere: close_time."""
    close = r.get("close_time") or None
    if r["source"] != "kalshi":
        return close
    parts = str(r["market_id"]).split("-")
    m = KALSHI_TICKER_DATE.match(parts[1]) if len(parts) > 1 else None
    if not m:
        return close
    try:
        day = dt.datetime.strptime("".join(m.groups()), "%y%b%d").replace(
            hour=23, minute=59, tzinfo=dt.timezone.utc).isoformat()
    except ValueError:
        return close
    return min(day, close) if close else day


def within(iso, horizon):
    if not iso:
        return False
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t <= horizon


# ---------------------------------------------------------------- sources
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def kalshi_exclusive_events(max_pages):
    """Event tickers whose outcomes are mutually exclusive (only one can resolve YES)."""
    ex, cursor = set(), None
    try:
        for _ in range(max_pages):
            params = {"status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = get_json(f"{KALSHI}/events", params)
            for e in data.get("events", []):
                if e.get("mutually_exclusive"):
                    ex.add(e.get("event_ticker"))
            cursor = data.get("cursor")
            if not cursor:
                break
            time.sleep(0.2)
    except Exception as e:
        log.warning("kalshi events lookup failed (%s); bracket checks skipped for Kalshi", e)
    return ex


def scrape_kalshi(snap, max_pages):
    base = f"{KALSHI}/markets"
    exclusive = kalshi_exclusive_events(max_pages)
    rows, cursor = [], None
    for _ in range(max_pages):
        # mve_filter=exclude drops the auto-generated multi-leg parlay (KXMVE*) markets
        # server-side; they fill the first ~100k results and otherwise exhaust the page cap.
        params = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        data = get_json(base, params)
        for m in data.get("markets", []):
            ticker = m.get("ticker", "")
            if ticker.startswith("KXMVE"):  # belt and braces, in case mve_filter is ignored
                continue
            rows.append({
                "snapshot_utc": snap, "source": "kalshi",
                "market_id": ticker,
                "event_id": m.get("event_ticker"),
                "title": m.get("title"),
                "outcome": m.get("yes_sub_title") or m.get("subtitle") or "",
                "yes_price": kalshi_price(m, "last_price"),
                "yes_bid": kalshi_price(m, "yes_bid"),
                "yes_ask": kalshi_price(m, "yes_ask"),
                "volume": num(m.get("volume_fp") or m.get("volume")),
                "volume_24h": num(m.get("volume_24h_fp") or m.get("volume_24h")),
                "open_interest": num(m.get("open_interest_fp") or m.get("open_interest")),
                "liquidity": kalshi_price(m, "liquidity"),
                "close_time": m.get("close_time"),
                "url": f"https://kalshi.com/markets/{(m.get('event_ticker') or ticker).split('-')[0].lower()}",
                "real_money": 1,
                "exclusive": 1 if m.get("event_ticker") in exclusive else 0,
                "strike_type": m.get("strike_type") or "",
                "floor_strike": num(m.get("floor_strike")),
                "cap_strike": num(m.get("cap_strike")),
            })
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(0.25)
    return rows


def scrape_polymarket(snap, max_pages, end_max=None):
    # Offset paging on /markets is capped (pages of <=100, offset < ~5000), so walk the
    # keyset endpoint by cursor instead. end_date_min drops markets already past their
    # end date that haven't been closed yet. ~220k rows / ~2200 pages as of Oct 2026.
    base = "https://gamma-api.polymarket.com/markets/keyset"
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows, cursor = [], None
    for _ in range(max_pages):
        params = {"active": "true", "closed": "false", "limit": 100, "end_date_min": now}
        if end_max:
            params["end_date_max"] = end_max
        if cursor:
            params["after_cursor"] = cursor
        page = get_json(base, params)
        data = page.get("markets") or []
        for m in data:
            outcomes = json_list(m.get("outcomes"))
            prices = json_list(m.get("outcomePrices"))
            event = (m.get("events") or [{}])[0]
            slug = event.get("slug") or m.get("slug") or ""
            rows.append({
                "snapshot_utc": snap, "source": "polymarket",
                "market_id": m.get("id"),
                "event_id": event.get("id", ""),
                "title": m.get("question"),
                "outcome": outcomes[0] if outcomes else "",
                "yes_price": num(prices[0]) if prices else None,
                "yes_bid": num(m.get("bestBid")),
                "yes_ask": num(m.get("bestAsk")),
                "volume": num(m.get("volumeNum") or m.get("volume")),
                "volume_24h": num(m.get("volume24hr")),
                "open_interest": None,
                "liquidity": num(m.get("liquidityNum") or m.get("liquidity")),
                "close_time": m.get("endDate"),
                "url": f"https://polymarket.com/event/{slug}" if slug else "",
                "real_money": 1,
                "exclusive": 1 if m.get("negRisk") else 0,
            })
        # Short pages are normal (server caps page size); only a missing cursor ends it.
        cursor = page.get("next_cursor")
        if not cursor or not data:
            break
        time.sleep(0.1)
    return rows


def scrape_manifold(snap, max_pages):
    base = "https://api.manifold.markets/v0/markets"
    rows, before = [], None
    now_ms = time.time() * 1000
    for _ in range(max_pages):  # newest-first; each page = 1000 markets
        params = {"limit": 1000}
        if before:
            params["before"] = before
        data = get_json(base, params)
        if not data:
            break
        for m in data:
            if m.get("isResolved") or (m.get("closeTime") or 0) < now_ms:
                continue
            if m.get("outcomeType") != "BINARY":
                continue
            rows.append({
                "snapshot_utc": snap, "source": "manifold",
                "market_id": m.get("id"), "event_id": "",
                "title": m.get("question"), "outcome": "YES",
                "yes_price": num(m.get("probability")),
                "yes_bid": None, "yes_ask": None,
                "volume": num(m.get("volume")),
                "volume_24h": num(m.get("volume24Hours")),
                "open_interest": None,
                "liquidity": num(m.get("totalLiquidity")),
                "close_time": ms_to_iso(m.get("closeTime")),
                "url": m.get("url", ""),
                "real_money": 0,
            })
        before = data[-1].get("id")
        if len(data) < 1000:
            break
        time.sleep(0.25)
    return rows


def scrape_predictit(snap, _max_pages):
    data = get_json("https://www.predictit.org/api/marketdata/all/")
    rows = []
    for mk in data.get("markets", []):
        multi = 1 if len(mk.get("contracts", [])) > 1 else 0
        for c in mk.get("contracts", []):
            rows.append({
                "snapshot_utc": snap, "source": "predictit",
                "market_id": c.get("id"), "event_id": mk.get("id"),
                "title": mk.get("name"), "outcome": c.get("name"),
                "yes_price": num(c.get("lastTradePrice")),
                "yes_bid": num(c.get("bestSellYesCost")),
                "yes_ask": num(c.get("bestBuyYesCost")),
                "volume": None, "volume_24h": None,
                "open_interest": None, "liquidity": None,
                "close_time": c.get("dateEnd") if c.get("dateEnd") not in (None, "NA") else None,
                "url": mk.get("url", ""),
                "real_money": 1,
                "exclusive": multi,
            })
    return rows


SOURCES = {
    "kalshi": scrape_kalshi,
    "polymarket": scrape_polymarket,
    "manifold": scrape_manifold,
    "predictit": scrape_predictit,
}


# ---------------------------------------------------------------- output
def write_csv(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_sqlite(rows, db_path):
    con = sqlite3.connect(db_path)
    con.execute(f"CREATE TABLE IF NOT EXISTS snapshots ({', '.join(FIELDS)})")
    have = {row[1] for row in con.execute("PRAGMA table_info(snapshots)")}
    for col in FIELDS:
        if col not in have:
            con.execute(f"ALTER TABLE snapshots ADD COLUMN {col}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_mkt ON snapshots (source, market_id, snapshot_utc)")
    con.executemany(
        f"INSERT INTO snapshots ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})",
        [tuple(r.get(k) for k in FIELDS) for r in rows],
    )
    con.commit()
    con.close()


SITE_FIELDS = ["source", "market_id", "title", "outcome", "yes_price", "yes_bid",
               "yes_ask", "volume", "volume_24h", "close_time", "decision_time", "url", "prev_price",
               "edge", "side", "basis", "conf", "ref", "ref_url"]


def load_previous_prices(out_dir, today):
    """yes_price per (source, market_id) from the most recent earlier snapshot folder."""
    try:
        days = sorted(d for d in os.listdir(out_dir) if d < today and len(d) == 10)
    except FileNotFoundError:
        return {}
    if not days:
        return {}
    prev, folder = {}, os.path.join(out_dir, days[-1])
    for fn in os.listdir(folder):
        if not fn.endswith(".csv.gz"):
            continue
        with gzip.open(os.path.join(folder, fn), "rt", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                prev[(r["source"], r["market_id"])] = num(r["yes_price"])
    return prev


SITE_TOP_N = 5000  # per source, per dashboard sort order


def site_subset(rows, top_n=SITE_TOP_N):
    """Kalshi + Polymarket have 300k+ open markets: far too many for one JSON file.
    Keep, per source, the top_n rows for each way the dashboard sorts (edge score,
    edge score excluding 'thin', 24h volume, total volume). Full data stays in the CSVs."""
    score = lambda r: (r["edge"] * r["conf"]) if r.get("edge") is not None and r.get("conf") is not None else -1
    keys = [
        score,
        lambda r: score(r) if r.get("basis") != "thin" else -1,
        lambda r: r.get("volume_24h") or 0,
        lambda r: r.get("volume") or 0,
    ]
    by_source = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(r)
    keep = set()
    for group in by_source.values():
        if len(group) <= top_n:
            keep.update(map(id, group))
            continue
        for key in keys:
            keep.update(id(r) for r in sorted(group, key=key, reverse=True)[:top_n] if key(r) > 0)
    return [r for r in rows if id(r) in keep]


def write_site(rows, site_dir, out_dir, day, snap, counts, failed):
    """Compact JSON the dashboard (site/index.html) reads."""
    prev = load_previous_prices(out_dir, day)
    rnd = lambda x: round(x, 4) if isinstance(x, float) else x
    packed = []
    for r in site_subset(rows):
        r = dict(r, prev_price=prev.get((r["source"], str(r["market_id"]))))
        packed.append([rnd(r.get(k)) for k in SITE_FIELDS])
    os.makedirs(os.path.join(site_dir, "data"), exist_ok=True)
    payload = {"generated": snap, "day": day, "counts": counts, "failed": failed,
               "fields": SITE_FIELDS, "rows": packed}
    with open(os.path.join(site_dir, "data", "latest.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    log.info("wrote dashboard data: %d rows", len(packed))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", nargs="+", choices=SOURCES, default=list(SOURCES))
    ap.add_argument("--out", default="data", help="folder for daily CSV.gz files")
    ap.add_argument("--db", help="optional SQLite file to append snapshots to")
    ap.add_argument("--site", default="site", help="dashboard folder (writes data/latest.json); '' to skip")
    ap.add_argument("--max-pages", type=int, default=200, help="page cap per source")
    ap.add_argument("--all-markets", action="store_true",
                    help="keep every market, not just mention markets")
    ap.add_argument("--horizon-days", type=int, default=30,
                    help="only keep markets decided within this many days (0 = no limit)")
    ap.add_argument("--keyword", action="append", default=[],
                    help="only keep rows whose title/outcome contains this (repeatable, case-insensitive)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    now = dt.datetime.now(dt.timezone.utc)
    snap, day = now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d")
    kws = [k.lower() for k in args.keyword]
    horizon = now + dt.timedelta(days=args.horizon_days) if args.horizon_days > 0 else None
    all_rows, failed, counts = [], [], {}

    for name in args.sources:
        try:
            pages = {"manifold": 10,             # Manifold has huge long tail
                     "polymarket": args.max_pages * 20,  # 100 rows/page, ~2200 pages
                     }.get(name, args.max_pages)
            if name == "polymarket" and horizon:  # filter server-side: far fewer pages
                rows = scrape_polymarket(snap, pages, horizon.strftime("%Y-%m-%dT%H:%M:%SZ"))
            else:
                rows = SOURCES[name](snap, pages)
            for r in rows:
                r["decision_time"] = decision_time(r)
            if not args.all_markets:
                rows = [r for r in rows if is_mention(r)]
            if horizon:
                rows = [r for r in rows if within(r["decision_time"], horizon)]
            if kws:
                rows = [r for r in rows
                        if any(k in f"{r['title']} {r['outcome']}".lower() for k in kws)]
            write_csv(rows, os.path.join(args.out, day, f"{name}.csv.gz"))
            all_rows += rows
            counts[name] = len(rows)
            log.info("%-10s %6d rows", name, len(rows))
        except Exception as e:  # one source failing shouldn't kill the run
            failed.append(name)
            log.error("%s failed: %s", name, e)

    if args.db and all_rows:
        write_sqlite(all_rows, args.db)
        log.info("appended %d rows to %s", len(all_rows), args.db)

    if args.site and all_rows:
        compute_edges(all_rows)
        write_site(all_rows, args.site, args.out, day, snap, counts, failed)

    if failed and len(failed) == len(args.sources):
        raise SystemExit("all sources failed")


if __name__ == "__main__":
    main()
