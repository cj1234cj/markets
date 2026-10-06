"""
Past results for Kalshi mention markets, so each open market can be priced
from how often the same word came up at earlier events in the same series
(e.g. did Starbucks say "Pumpkin Spice" on its last 4 earnings calls?).

Kalshi keeps settled markets in two places: /markets?status=settled for
recent ones and /historical/markets for anything settled before the
cutoff (~2 months back). Both are public, no key needed.

Every past result here is a real settled market, so it can be checked
against the transcript of that event.
"""
import json
import logging
import os
import re
import time
from collections import defaultdict

log = logging.getLogger("scraper")

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
# per-call word counts from transcripts.py (transcripts themselves stay local)
TRANSCRIPT_HISTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "transcript_history.json")
SKIP_WORDS = {"event does not qualify"}
TICKER_DATE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})?$")


def norm(word):
    return " ".join((word or "").lower().split())


def series_of(ticker):
    return str(ticker).split("-")[0]


def event_date(event_ticker):
    """'KXTRUMPMENTION-26OCT05' -> '2026-10-05' (or '2026-10' for month-only tickers)."""
    parts = str(event_ticker).split("-")
    m = TICKER_DATE.match(parts[1]) if len(parts) > 1 else None
    if not m:
        return ""
    yy, mon, dd = m.groups()
    months = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
    if mon not in months:
        return ""
    return f"20{yy}-{months.index(mon) + 1:02d}" + (f"-{dd}" if dd else "")


def fetch_settled(get_json, series):
    """All settled markets in a series, recent + archived, de-duplicated by ticker."""
    seen = {}
    for path, params in (("/markets", {"status": "settled"}), ("/historical/markets", {})):
        cursor = None
        for _ in range(50):
            p = {"series_ticker": series, "limit": 1000, **params}
            if cursor:
                p["cursor"] = cursor
            data = get_json(KALSHI + path, p)
            for m in data.get("markets", []):
                seen[m["ticker"]] = m
            cursor = data.get("cursor")
            if not cursor:
                break
            time.sleep(0.1)
    return list(seen.values())


def attach_history(rows, get_json):
    """Sets r['hist'] = [(date, 'yes'|'no', event_ticker), ...] newest first, on Kalshi
    rows whose word has settled before in the same series."""
    kalshi = [r for r in rows if r["source"] == "kalshi"]
    by_series = defaultdict(list)
    for r in kalshi:
        by_series[series_of(r["market_id"])].append(r)

    for i, (series, open_rows) in enumerate(sorted(by_series.items())):
        try:
            settled = fetch_settled(get_json, series)
        except Exception as e:  # history is a bonus; never fail the scrape over it
            log.warning("history for %s failed: %s", series, e)
            continue
        past = defaultdict(dict)  # word -> {event_ticker: (date, result)}
        for m in settled:
            word, result = norm(m.get("yes_sub_title")), m.get("result")
            if word in SKIP_WORDS or result not in ("yes", "no"):
                continue
            # A settled mention market closes when the event happens, so close_time is
            # the real event date; ticker dates can be placeholders (e.g. "-26JUN30"
            # events that were really the January earnings calls).
            date = (m.get("close_time") or "")[:10] or event_date(m["event_ticker"])
            past[word][m["event_ticker"]] = (date, result)
        for r in open_rows:
            events = past.get(norm(r.get("outcome")), {})
            r["hist"] = sorted(((d, res, ev) for ev, (d, res) in events.items()
                                if ev != r.get("event_id")), reverse=True)
            r["hist_src"] = "Kalshi results"
        time.sleep(0.1)

    # Transcript counts (transcripts.py, run locally) cover more calls than Kalshi has
    # listed; use them whenever they do.
    tx = load_transcript_history()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    used = 0
    for r in kalshi:
        calls = tx.get(series_of(r["market_id"]), {}).get("words", {}).get(norm(r.get("outcome")))
        if not calls:
            continue
        hist = sorted(((d, "yes" if n >= need else "no", "transcript") for d, n, need in calls
                       if d < today), reverse=True)
        if len(hist) >= len(r.get("hist") or []):
            r["hist"], r["hist_src"] = hist, "transcripts"
            used += 1
    with_hist = sum(1 for r in kalshi if r.get("hist"))
    log.info("history: %d series, %d/%d Kalshi markets have past results (%d from transcripts)",
             len(by_series), with_hist, len(kalshi), used)


def load_transcript_history(path=TRANSCRIPT_HISTORY):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("series", {})
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("couldn't read %s: %s", path, e)
        return {}
