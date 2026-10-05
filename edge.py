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
import math
import re
from collections import defaultdict

# ---------- assumptions you can tune
LONGSHOT_MAX = 0.15     # YES price at or below this counts as a longshot
LONGSHOT_BIAS = 0.20    # assume longshots are overpriced by 20% of their price
MATCH_MIN_SIMILARITY = 0.55
MATCH_MAX_CLOSE_GAP_DAYS = 10
CONF = {"arb": 1.0, "cross": 0.75, "cross_play": 0.35, "bracket": 0.6, "longshot": 0.3, "thin": 0.2}
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
    return r.get("yes_ask") is not None and r["yes_ask"] < 1


def mid(r):
    if has_bid(r) and has_ask(r) and r["yes_ask"] >= r["yes_bid"]:
        return (r["yes_ask"] + r["yes_bid"]) / 2
    return r.get("yes_price")


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


def offer(r, side, fair, basis, ref="", ref_url="", conf=None):
    e = edge_for(r, side, fair)
    if e is None or e <= 0:
        return
    c = CONF[basis] if conf is None else conf
    if e * c > r.get("_score", -1):
        r.update(edge=round(e, 4), side=side, basis=basis.split("_")[0], conf=c,
                 ref=ref, ref_url=ref_url, _score=e * c)


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


def cross_venue(rows):
    live = [r for r in rows if mid(r) is not None]
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
            sim = len(r["_t"] & o["_t"]) / len(r["_t"] | o["_t"])
            if sim >= MATCH_MIN_SIMILARITY and sim > best.get(o["source"], (0, None))[0]:
                best[o["source"]] = (sim, o)

        for src, (sim, o) in best.items():
            fair, play = mid(o), not o.get("real_money")
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
    groups = defaultdict(list)
    for r in rows:
        if r["source"] == "kalshi" and r.get("strike_type") in ("greater", "greater_or_equal") and r.get("floor_strike") is not None:
            groups[r["event_id"]].append(r)
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
            groups[(r["source"], r["event_id"])].append(r)
    for legs in groups.values():
        if len(legs) < 2:
            continue
        total = sum(mid(r) for r in legs)
        bids = sum(r.get("yes_bid") or 0 for r in legs)
        if total <= 1.0:
            continue        # can't tell which leg is cheap if the list may be incomplete
        for r in legs:
            fair = mid(r) / total
            label = f"{len(legs)} outcomes add up to {total * 100:.0f}%; fair after removing the excess ≈ {cents(fair)}"
            if bids > 1.0:
                offer(r, "NO", fair, "arb", f"Buy NO on every outcome: bids sum to {bids * 100:.0f}%. " + label)
            offer(r, "NO", fair, "bracket", label)


# ---------- 4. longshots, 5. thin markets
def longshots_and_thin(rows):
    for r in rows:
        m = mid(r)
        if m is None:
            continue
        if 0 < m <= LONGSHOT_MAX and r.get("real_money"):
            offer(r, "NO", m * (1 - LONGSHOT_BIAS), "longshot",
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
    for r in rows:
        r["_score"] = -1
    cross_venue(rows)
    ladders(rows)
    brackets(rows)
    longshots_and_thin(rows)
    for r in rows:
        r.pop("_score", None)
        r.setdefault("edge", None); r.setdefault("side", ""); r.setdefault("basis", "")
        r.setdefault("conf", None); r.setdefault("ref", ""); r.setdefault("ref_url", "")
    return rows
