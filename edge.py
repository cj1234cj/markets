"""
Edge estimates for every market, so the dashboard can rank by "most mispriced".

Edge = expected profit per $1 contract if you take the suggested side at the
current executable price (ask), minus estimated fees. 0.05 means 5 cents.

Signals, strongest first. Each market keeps whichever signal gives the highest
confidence-weighted edge (edge x confidence), and the page ranks by that.

  arb       Guaranteed-profit combinations (ignoring settlement-rule differences):
            crossed strike ladders, brackets whose YES bids sum above $1, or
            buying here below the bid on another real-money venue.
  ladder    (folded into arb) Kalshi "above X" markets priced out of order.
  history   Mention markets: fair value = how often the same word was said at
            past settled events in the same Kalshi series (needs 3+ events).
            Checkable: each past event can be verified against its transcript.
  cross     Same question on another venue at a different price. Fair value =
            the other venue's mid price. Match is fuzzy: always check the
            linked market is truly the same question and resolution date.
  bracket   Mutually exclusive outcomes whose prices add up to more than 100%.
            Fair value = each price scaled down so the set sums to 100%.
  longshot  Cheap YES contracts tend to be overpriced on retail-heavy venues
            (favorite-longshot bias). Size of the bias below is an ASSUMPTION.
  thin      No fair value available. Score = how much room there is for the
            price to be wrong (wide spread, little trading). This is research
            potential, not a measured edge, and has no suggested side.

Tune the constants below once you have resolved results to check against.
"""
import json
import math
import os
import re
from collections import defaultdict

# ---------- assumptions you can tune
LONGSHOT_MAX = 0.15     # YES price at or below this counts as a longshot
LONGSHOT_BIAS = 0.20    # assume longshots are overpriced by 20% of their price
MATCH_MIN_SIMILARITY = 0.7
CROSS_MIN_HOURS = 24      # skip cross-venue checks on markets deciding sooner than this
BRACKET_MAX_TOTAL = 1.25  # exclusive outcomes priced above this in total aren't really exclusive
NEGATION = re.compile(r"\b(no|not|never|won't|isn't|doesn't|fail|fails|without)\b", re.I)
MATCH_MAX_CLOSE_GAP_DAYS = 10
HISTORY_MIN_EVENTS = 3  # past settled events needed before trusting a hit rate
HISTORY_DECAY = 0.85    # weight of each older event vs the next newer one
HISTORY_RECENT = 3      # the edge must also hold on just the last few events
# Series where every event has the same format, so past results are comparable and
# checkable against transcripts: earnings calls, FOMC press conferences, and
# "say it during this week/month" windows. Other mention series (rallies, interviews,
# debates, speeches) mix event types and Kalshi picks words to fit each event.
SAME_FORMAT = re.compile(r"^KX(EARNINGSMENTION|FEDMENTION|TRUMPSAY)")
CONF = {"arb": 1.0, "history": 0.8, "history_mixed": 0.45, "cross": 0.75, "cross_play": 0.35, "bracket": 0.6, "longshot": 0.3, "thin": 0.2}
VENUE = {"kalshi": "Kalshi", "polymarket": "Polymarket", "predictit": "PredictIt", "manifold": "Manifold"}

STOP = set("""the a an of in on at by to for will be is are and or vs before after during with what who which
how many much than more less does do did market price yes no this that it its as from his her their
""".split())
YEAR = re.compile(r"^20[2-3]\d$")


# ---------- prices and fees
def fee(source, price):
    """Rough fee per contract at this price."""
    if price is None:
        return 0.0
    if source == "kalshi":
        return 0.07 * price * (1 - price)          # Kalshi standard taker fee
    if source == "predictit":
        return 0.10 * price * (1 - price)          # 10% of profit, expected at fair ~ price
    return 0.0                                      # most Polymarket markets; Manifold is play money


def has_bid(r):
    return r.get("yes_bid") is not None and r["yes_bid"] > 0


def has_ask(r):
    # Kalshi reports ask 0 when nobody is selling; that's no offer, not a free contract
    return r.get("yes_ask") is not None and 0 < r["yes_ask"] < 1


def mid(r):
    if has_bid(r) and has_ask(r) and r["yes_ask"] >= r["yes_bid"]:
        return (r["yes_ask"] + r["yes_bid"]) / 2
    return r.get("yes_price")


TIGHT_SPREAD = 0.10


def tight_mid(r):
    """Mid price only when both sides are quoted within TIGHT_SPREAD; a midpoint of a
    1c/99c quote (or a stale last trade) says nothing about the real price."""
    if has_bid(r) and has_ask(r) and 0 <= r["yes_ask"] - r["yes_bid"] <= TIGHT_SPREAD:
        return (r["yes_ask"] + r["yes_bid"]) / 2
    return None


def yes_cost(r):
    """Price to buy YES now. None if the venue shows an order book with no sellers."""
    if r.get("yes_ask") is None:
        return r.get("yes_price")
    return r["yes_ask"] if has_ask(r) else None


def no_cost(r):
    """Price to buy NO now (= 1 - best YES bid)."""
    if r.get("yes_bid") is None:
        p = r.get("yes_price")
        return None if p is None else 1 - p
    return 1 - r["yes_bid"] if has_bid(r) else None


def edge_for(r, side, fair):
    """Net expected profit per contract buying `side` when true P(YES) = fair."""
    if fair is None:
        return None
    if side == "YES":
        c = yes_cost(r)
        return None if c is None or c >= 1 else fair - c - fee(r["source"], c)
    c = no_cost(r)
    return None if c is None or c >= 1 else (1 - fair) - c - fee(r["source"], c)


def load_calibration():
    """Per signal type: how much of its predicted edge it has actually delivered
    (ledger.py, from settled calls). 1.0 = no adjustment yet."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "calibration.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return {k: v["factor"] for k, v in json.load(fh).get("basis", {}).items()}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


CALIBRATION = {}


def offer(r, side, fair, basis, ref="", ref_url="", conf=None):
    e = edge_for(r, side, fair)
    if e is None or e <= 0:
        return
    short = basis.split("_")[0]
    c = (CONF[basis] if conf is None else conf) * CALIBRATION.get(short, 1.0)
    if e * c > r.get("_score", -1):
        r.update(edge=round(e, 4), side=side, basis=short, conf=round(c, 3),
                 ref=ref, ref_url=ref_url, _score=e * c,
                 # for the track record (ledger.py)
                 fair=round(fair, 4), price=yes_cost(r) if side == "YES" else no_cost(r),
                 market_prob=mid(r))


def cents(x):
    return "–" if x is None else f"{x * 100:.0f}¢" if x * 100 >= 1 else f"{x * 100:.1f}¢"


# ---------- 1. cross-venue matching
def tokens(r):
    text = f"{r.get('title') or ''} {r.get('outcome') or ''}".lower()
    text = re.sub(r"[^a-z0-9.%$ ]", " ", text)
    return {w.strip(".") for w in text.split() if w.strip(".") and w.strip(".") not in STOP and len(w.strip(".")) > 1}


def key_numbers(ts):
    """Numbers that change a question's meaning (strikes, counts), ignoring years."""
    return {t for t in ts if any(ch.isdigit() for ch in t) and not YEAR.match(t)}


def close_days(r):
    t = r.get("close_time")
    if not t:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp() / 86400
    except ValueError:
        return None


def outcome_tokens(r):
    o = (r.get("outcome") or "").lower()
    if not o or o in ("yes", "no"):
        return None
    return {w for w in re.sub(r"[^a-z0-9.]", " ", o).split() if w not in STOP and len(w) > 1}


def soon(r, hours=CROSS_MIN_HOURS):
    """Deciding within `hours`: live games and same-day prices move faster than the
    gap between scraping one venue and the next, so their prices aren't comparable."""
    from datetime import datetime, timezone
    t = r.get("decision_time") or r.get("close_time")
    try:
        t = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (t - datetime.now(timezone.utc)).total_seconds() < hours * 3600


def cross_venue(rows):
    # only markets with a real two-sided price, not deciding in the next day
    live = [r for r in rows if tight_mid(r) is not None and not soon(r)]
    for r in live:
        r["_t"] = tokens(r)
        r["_n"] = key_numbers(r["_t"])
        r["_d"] = close_days(r)
    df = defaultdict(int)
    for r in live:
        for t in r["_t"]:
            df[t] += 1
    index = defaultdict(list)
    for i, r in enumerate(live):
        for t in r["_t"]:
            if df[t] <= 200:                       # common words don't help matching
                index[t].append(i)

    for i, r in enumerate(live):
        rare = sorted((t for t in r["_t"] if t in index), key=lambda t: df[t])[:3]
        best = {}
        for j in {j for t in rare for j in index[t]}:
            o = live[j]
            if o["source"] == r["source"] or o["_n"] != r["_n"]:
                continue
            if r["_d"] and o["_d"] and abs(r["_d"] - o["_d"]) > MATCH_MAX_CLOSE_GAP_DAYS:
                continue
            # named outcomes must be the same thing ("Vukic" vs "Kotov" is the other side)
            ra, oa_ = outcome_tokens(r), outcome_tokens(o)
            if ra is not None and oa_ is not None and not (ra & oa_):
                continue
            # "Will there be NO Gemini release" is the opposite of "Will Google release"
            if bool(NEGATION.search(r.get("title") or "")) != bool(NEGATION.search(o.get("title") or "")):
                continue
            sim = len(r["_t"] & o["_t"]) / len(r["_t"] | o["_t"])
            if sim >= MATCH_MIN_SIMILARITY and sim > best.get(o["source"], (0, None))[0]:
                best[o["source"]] = (sim, o)

        for src, (sim, o) in best.items():
            fair, play = tight_mid(o), not o.get("real_money")
            label = f"{VENUE[src]} at {cents(fair)}: {o.get('title')}" + (f" ({o.get('outcome')})" if o.get("outcome") and o["outcome"].lower() != "yes" else "")
            for side in ("YES", "NO"):
                offer(r, side, fair, "cross_play" if play else "cross", label, o.get("url", ""))
            # executable on both venues -> arbitrage
            if not play:
                ob, oa = o.get("yes_bid"), o.get("yes_ask")
                name = o.get("title") + (f" ({o.get('outcome')})" if o.get("outcome") and o["outcome"].lower() != "yes" else "")
                if has_bid(o):  # buy YES here, buy NO there at 1 - their bid
                    offer(r, "YES", ob - fee(src, 1 - ob), "arb",
                          f"Also buy NO on {VENUE[src]} at {cents(1 - ob)}: {name}", o.get("url", ""))
                if has_ask(o):  # buy NO here, buy YES there at their ask
                    offer(r, "NO", oa + fee(src, oa), "arb",
                          f"Also buy YES on {VENUE[src]} at {cents(oa)}: {name}", o.get("url", ""))
    for r in live:
        for k in ("_t", "_n", "_d"):
            r.pop(k, None)


# ---------- 2. Kalshi strike ladders out of order
def ladders(rows):
    # Only the same question at different strikes is a ladder: one event can hold several
    # (Benin vs Argentina margins, rain in each city), so key on the wording minus numbers.
    groups = defaultdict(list)
    for r in rows:
        if r["source"] == "kalshi" and r.get("strike_type") in ("greater", "greater_or_equal") and r.get("floor_strike") is not None:
            template = re.sub(r"\d[\d,.]*", "#", f"{r.get('title') or ''}|{r.get('outcome') or ''}".lower())
            groups[(r["event_id"], template)].append(r)
    for legs in groups.values():
        legs.sort(key=lambda r: r["floor_strike"])
        for i, lo in enumerate(legs):            # P(above lo) must be >= P(above hi)
            for hi in legs[i + 1:]:
                if has_ask(lo) and has_bid(hi) and hi["yes_bid"] > lo["yes_ask"]:
                    label = f"Higher strike {hi.get('outcome') or hi['floor_strike']} bids {cents(hi['yes_bid'])}"
                    offer(lo, "YES", hi["yes_bid"] - fee("kalshi", 1 - hi["yes_bid"]), "arb", label, hi.get("url", ""))


# ---------- 3. mutually exclusive brackets that sum above 100%
def brackets(rows):
    groups = defaultdict(list)
    for r in rows:
        if r.get("exclusive") and r.get("event_id") and mid(r) is not None:
            groups[(r["source"], r.get("group_id") or r["event_id"])].append(r)
    for legs in groups.values():
        if len(legs) < 2:
            continue
        bids = sum(r.get("yes_bid") or 0 for r in legs)
        if bids > 1.0:      # executable whatever the spreads: NO on every leg pays n-1
            for r in legs:
                offer(r, "NO", r["yes_bid"] if has_bid(r) else 0, "arb",
                      f"Buy NO on every outcome: the {len(legs)} YES bids sum to {bids * 100:.0f}%")
        mids = [tight_mid(r) for r in legs]
        if any(m is None for m in mids):
            continue        # a leg without a real two-sided price makes the total meaningless
        total = sum(mids)
        if total <= 1.0:
            continue        # can't tell which leg is cheap if the list may be incomplete
        if total > BRACKET_MAX_TOTAL:
            continue        # real overpricing is a few %; this much means the outcomes aren't exclusive
        for r, m in zip(legs, mids):
            fair = m / total
            offer(r, "NO", fair, "bracket",
                  f"{len(legs)} outcomes add up to {total * 100:.0f}%; fair after removing the excess ≈ {cents(fair)}")


# ---------- 3b. mention markets priced from past results (mention_history.py)
def history(rows):
    """Fair = how often this word came up at past events in the same series.
    Recent events count more (speakers change habits, e.g. a new Fed chair), and
    one pseudo-event pulls toward 50%: (sum w*yes + 0.5) / (sum w + 1).

    Skipped: events already over (the outcome is known, the market just hasn't
    settled) and markets already priced as decided (bid >= 95c / ask <= 3c),
    which usually means the word was already said in an in-progress window."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    short = lambda d: f"{months[int(d[5:7]) - 1]} {int(d[8:10])}" if len(d) == 10 else d
    for r in rows:
        hist = r.get("hist") or []
        if len(hist) < HISTORY_MIN_EVENTS:
            continue
        try:
            if datetime.fromisoformat(str(r.get("decision_time")).replace("Z", "+00:00")) < now:
                continue
        except ValueError:
            continue
        if (r.get("yes_bid") or 0) >= 0.95 or (has_ask(r) and r["yes_ask"] <= 0.03):
            continue
        weights = [HISTORY_DECAY ** i for i in range(len(hist))]   # hist is newest first
        hits = sum(w for w, (_, res, _) in zip(weights, hist) if res == "yes")
        fair = (hits + 0.5) / (sum(weights) + 1)
        recent = hist[:HISTORY_RECENT]
        fair_recent = (sum(1 for _, res, _ in recent if res == "yes") + 0.5) / (len(recent) + 1)
        yes = sum(1 for _, res, _ in hist if res == "yes")
        shown = ", ".join(f"{short(d)}{'' if d[:4] == str(now.year) else ' ' + chr(39) + d[2:4]} "
                          f"{'✓' if res == 'yes' else '✗'}" for d, res, _ in hist[:8])
        more = f" +{len(hist) - 8} more" if len(hist) > 8 else ""
        series = str(r["market_id"]).split("-")[0]
        same = bool(SAME_FORMAT.match(series))
        events = "earnings calls" if series.startswith("KXEARNINGSMENTION") else "events"
        label = (f"Said on {yes} of the last {len(hist)} {events} ({r.get('hist_src') or 'Kalshi results'}): {shown}{more}. "
                 f"Fair ≈ {cents(fair)} (recent weighted more; last {len(recent)}: {cents(fair_recent)})"
                 + ("" if same else ". Past events vary in type, so check this event's topic"))
        series_url = f"https://kalshi.com/markets/{series.lower()}"
        basis = "history" if same else "history_mixed"
        # conservative: the edge has to hold on the long-run rate AND the last few events
        offer(r, "YES", min(fair, fair_recent), basis, label, series_url)
        offer(r, "NO", max(fair, fair_recent), basis, label, series_url)


# ---------- 4. longshots, 5. thin markets
def longshots_and_thin(rows):
    for r in rows:
        m = mid(r)
        if m is None:
            continue
        tm = tight_mid(r)
        if tm is not None and 0 < tm <= LONGSHOT_MAX and r.get("real_money"):
            offer(r, "NO", tm * (1 - LONGSHOT_BIAS), "longshot",
                  f"Cheap YES contracts tend to be overpriced; assumes about {LONGSHOT_BIAS:.0%} too high")
        if r.get("_score", -1) > 0:
            continue
        b, a = r.get("yes_bid"), r.get("yes_ask")
        spread = (a - b) if (a is not None and b is not None and a >= b) else 0.10
        vol = r.get("volume_24h") or 0
        potential = spread / 2 + 0.04 / (1 + math.log10(1 + vol))
        potential = min(potential, 0.25)
        r.update(edge=round(potential, 4), side="", basis="thin", conf=CONF["thin"],
                 ref="No fair value to compare against. Wide spread or little trading leaves room for the price to be wrong.",
                 ref_url="", _score=potential * CONF["thin"])


def compute_edges(rows):
    CALIBRATION.clear()
    CALIBRATION.update(load_calibration())
    for r in rows:
        r["_score"] = -1
    cross_venue(rows)
    ladders(rows)
    brackets(rows)
    history(rows)
    longshots_and_thin(rows)
    for r in rows:
        r.pop("_score", None)
        r.setdefault("edge", None); r.setdefault("side", ""); r.setdefault("basis", "")
        r.setdefault("conf", None); r.setdefault("ref", ""); r.setdefault("ref_url", "")
    return rows
