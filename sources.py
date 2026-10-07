"""
Independent probability estimates ("sources") for each rated mention market, so the
fair value is a weighted combination of several views instead of one model, and each
view can be scored on its own as markets settle (ledger.py -> data/weights.json).

For "Will company X say word W on its next earnings call?":

  market          Kalshi's own price (mid of a real two-sided quote)
  polymarket      the same company + word on Polymarket
  kalshi_history  how often W was said at X's past events (recency-weighted)
  kalshi_recent3  ...on just the last 3 events (catches habit changes)
  peers_season    how often W was said on OTHER companies' calls in the last 45 days
                  (topical words - tariff, shutdown, World Cup - spread through a season)
  news            share of the last 30 days of news headlines about X that mention W
                  (GDELT; fetched once per company per day, cached)

Every estimate is a probability that W is said. Missing sources are simply left out.
"""
import datetime as dt
import json
import logging
import math
import os
import re
import time
from collections import defaultdict

log = logging.getLogger("scraper")

ROOT = os.path.dirname(os.path.abspath(__file__))
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
NEWS_CACHE = os.path.join(ROOT, "data", "news_cache.json")
PEER_DAYS = 45
NEWS_DAYS = 30
THRESHOLD = re.compile(r"\((\d+)\+\s*times?\)", re.I)
SUFFIX = re.compile(r",?\s+(inc\.?|incorporated|corporation|corp\.?|company|co\.?|group|holdings?|"
                    r"plc|s\.a\.|n\.v\.|ltd\.?|limited|the)$", re.I)

LABELS = {"market": "Market", "polymarket": "Polymarket", "kalshi_history": "Past calls",
          "kalshi_recent3": "Last 3 calls", "peers_season": "Peers this season", "news": "News"}


# ---------------------------------------------------------------- word keys
def word_key(outcome):
    """'Fold / Folding / Foldable (3+ times)' -> (frozenset of forms, 3)"""
    m = THRESHOLD.search(outcome or "")
    need = int(m.group(1)) if m else 1
    forms = frozenset(" ".join(f.lower().split()) for f in THRESHOLD.sub("", outcome or "").split("/") if f.strip())
    return forms, need


def same_word(a, b):
    return a[1] == b[1] and bool(a[0] & b[0])


def rate(results, decay=0.85):
    """Recency-weighted share of 'yes', with one 50/50 pseudo-event; newest first."""
    w = [decay ** i for i in range(len(results))]
    hits = sum(wi for wi, res in zip(w, results) if res == "yes")
    return (hits + 0.5) / (sum(w) + 1)


def company_name(title):
    m = re.match(r"What will (.+?) say during", title or "")
    name = m.group(1) if m else ""
    for _ in range(3):
        name = SUFFIX.sub("", name.strip())
    return re.sub(r"^the\s+", "", name.strip(" ,&"), flags=re.I)


# ---------------------------------------------------------------- peers
def fetch_peer_results(get_json, now):
    """{word_key: [(series, date, result), ...]} for every Kalshi earnings-mention market
    settled in the last PEER_DAYS days, across all ~180 companies."""
    data = get_json(f"{KALSHI}/series", {"category": "Mentions"})
    series = [s["ticker"] for s in data.get("series", []) if s["ticker"].startswith("KXEARNINGSMENTION")]
    since = int((now - dt.timedelta(days=PEER_DAYS)).timestamp())
    out = defaultdict(list)
    for s in series:
        try:
            d = get_json(f"{KALSHI}/markets", {"series_ticker": s, "status": "settled",
                                                "min_close_ts": since, "limit": 1000})
        except Exception as e:
            log.warning("peers: %s failed (%s)", s, e)
            continue
        for m in d.get("markets", []):
            if m.get("result") in ("yes", "no") and (m.get("yes_sub_title") or "").lower() != "event does not qualify":
                out[word_key(m.get("yes_sub_title"))].append((s, (m.get("close_time") or "")[:10], m["result"]))
        time.sleep(0.05)
    log.info("peers: %d series, %d settled words in the last %d days", len(series),
             sum(len(v) for v in out.values()), PEER_DAYS)
    return out


def peer_estimate(peers, key, series):
    hits = []
    for k, rows in peers.items():
        if same_word(k, key):
            hits += [r for r in rows if r[0] != series]
    calls = {(s, d): res for s, d, res in hits}          # one result per peer call
    if len(calls) < 2:
        return None, 0
    yes = sum(1 for v in calls.values() if v == "yes")
    return (yes + 0.5) / (len(calls) + 1), len(calls)


# ---------------------------------------------------------------- news
def load_news_cache():
    try:
        with open(NEWS_CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def news_headlines(session, company, cache, today):
    """Up to 250 headlines about the company from the last NEWS_DAYS days, cached per day."""
    c = cache.get(company)
    if c and c.get("day") == today:
        return c["titles"]
    try:
        r = session.get(GDELT, params={"query": f'"{company}"', "mode": "artlist", "maxrecords": 250,
                                       "timespan": f"{NEWS_DAYS}d", "format": "json", "sourcelang": "english"},
                        timeout=60)
        time.sleep(5.5)                                     # GDELT asks for <= 1 request / 5s
        if r.status_code != 200 or not r.text.startswith("{"):
            log.warning("news: %s -> HTTP %s", company, r.status_code)
            return c["titles"] if c else None
        titles = [a.get("title", "") for a in r.json().get("articles", [])]
    except Exception as e:
        log.warning("news: %s failed (%s)", company, e)
        return c["titles"] if c else None
    cache[company] = {"day": today, "titles": titles}
    return titles


def news_estimate(titles, key):
    """Feature, not a calibrated probability: 15% floor plus 4x the share of headlines
    mentioning the word. The ensemble's learned weight decides how much it matters."""
    if not titles or len(titles) < 10:
        return None, 0
    pats = [re.compile(r"(?<!\w)" + r"[\s\-]+".join(map(re.escape, f.split())) + r"(?:s|es|'s)?(?!\w)", re.I)
            for f in key[0]]
    n = sum(1 for t in titles if any(p.search(t) for p in pats))
    return min(0.95, 0.15 + 4 * n / len(titles)), n


# ---------------------------------------------------------------- polymarket
def poly_index(rows):
    """Polymarket earnings-mention markets by company-name token, with their quoted words."""
    idx = defaultdict(list)
    for r in rows:
        if r["source"] != "polymarket" or "earnings" not in (r.get("title") or "").lower():
            continue
        m = re.match(r"Will (.+?) say ", r.get("title") or "")
        if not m:
            continue
        words = re.findall(r'["“]([^"”]+)["”]', r["title"])
        need = THRESHOLD.search(r["title"]) or re.search(r"(\d+)\+\s*times", r["title"])
        key = (frozenset(" ".join(w.lower().split()) for w in words), int(need.group(1)) if need else 1)
        idx[company_name(f"What will {m.group(1)} say during").split()[0].lower()].append((key, r))
    return idx


# ---------------------------------------------------------------- word type (for the scorecard)
def word_type(key, has_history, peer_n):
    if key[1] > 1:
        return "count threshold"
    if not has_history:
        return "new word"
    return "cross-company topic" if peer_n >= 3 else "company-specific"


# ---------------------------------------------------------------- main entry
def attach_sources(rows, all_rows, get_json, session, mid_of, now=None):
    """Sets r['est'] = {source: probability}, r['est_detail'] = {source: short text},
    r['word_type'] and r['event_key'] on each rated Kalshi mention row."""
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    try:
        peers = fetch_peer_results(get_json, now)
    except Exception as e:
        log.warning("peers unavailable (%s)", e)
        peers = {}
    poly = poly_index(all_rows)
    cache = load_news_cache()
    news_by_company = {}
    fetched = 0
    for r in rows:
        if r["source"] != "kalshi":
            continue
        series = str(r["market_id"]).split("-")[0]
        key = word_key(r.get("outcome"))
        est, detail = {}, {}
        m = mid_of(r)
        if m is not None:
            est["market"] = m
        hist = r.get("hist") or []
        results = [res for _, res, _ in hist]
        if len(results) >= 2:
            est["kalshi_history"] = rate(results)
            detail["kalshi_history"] = f"{sum(x == 'yes' for x in results)} of {len(results)} {r.get('hist_src') or 'Kalshi results'}"
        if len(results) >= 3:
            est["kalshi_recent3"] = (sum(x == "yes" for x in results[:3]) + 0.5) / 4
        p, peer_n = peer_estimate(peers, key, series)
        if p is not None:
            est["peers_season"] = p
            detail["peers_season"] = f"{peer_n} peer calls"
        if series.startswith("KXEARNINGSMENTION"):
            company = company_name(r.get("title"))
            if company:
                if company not in news_by_company:
                    news_by_company[company] = news_headlines(session, company, cache, today)
                    fetched += 1
                p, n = news_estimate(news_by_company[company], key)
                if p is not None:
                    est["news"] = p
                    detail["news"] = f"{n} of {len(news_by_company[company])} headlines"
                for pkey, pr in poly.get(company.split()[0].lower(), []):
                    if same_word(pkey, key) and mid_of(pr) is not None:
                        est["polymarket"] = mid_of(pr)
                        break
        r["est"], r["est_detail"] = est, detail
        r["word_type"] = word_type(key, bool(results), peer_n)
        r["event_key"] = r.get("event_id") or series
    try:
        os.makedirs(os.path.dirname(NEWS_CACHE), exist_ok=True)
        with open(NEWS_CACHE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    except OSError:
        pass
    have = defaultdict(int)
    for r in rows:
        for k in r.get("est", {}):
            have[k] += 1
    log.info("sources: %s (news for %d companies)", dict(have), fetched)


# ---------------------------------------------------------------- combining
PRIOR_WEIGHTS = {"market": 0.50, "polymarket": 0.15, "kalshi_history": 0.12,
                 "kalshi_recent3": 0.08, "peers_season": 0.10, "news": 0.05}


def logit(p):
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def pool(est, weights, temperature=1.0):
    """Weighted average in log-odds of the sources that are present."""
    num = den = 0.0
    for k, p in est.items():
        w = weights.get(k, 0.0)
        if w > 0 and p is not None:
            num += w * logit(p)
            den += w
    if den == 0:
        return None
    z = temperature * num / den
    return 1 / (1 + math.exp(-z))
