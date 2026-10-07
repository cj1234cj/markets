"""
Track record: every call the edge model makes, and how it turned out.

Each daily run:
  1. record()  - logs each market that has a real signal (not "thin") with edge >= 5c,
                 the FIRST time it is flagged: fair value, side, entry price, edge.
                 Later runs never overwrite it, so there is no hindsight.
  2. settle()  - looks up the result of logged markets whose decision date has passed.
  3. summarize() - realized profit vs predicted edge, by signal type, category,
                 venue and edge size, plus Brier scores (model vs market price).
                 Writes the dashboard's Track record data and data/calibration.json,
                 which edge.py reads to trust each signal type as much as it has earned.

Everything is per $1 contract: pnl = payout - entry price - fees.
"""
import csv
import datetime as dt
import json
import logging
import math
import os
import time
from collections import defaultdict

from edge import fee

log = logging.getLogger("scraper")

ROOT = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(ROOT, "data", "ledger.csv")
CALIBRATION = os.path.join(ROOT, "data", "calibration.json")
MIN_LOG_EDGE = 0.05
CALIB_PRIOR = 30      # resolved calls needed before track record outweighs the default
MAX_SETTLE_PER_RUN = 600

FIELDS = ["logged_utc", "source", "market_id", "event_id", "title", "outcome", "category",
          "basis", "signal", "side", "fair", "market_prob", "price", "edge", "conf",
          "fair_raw", "edge_raw", "sources", "word_type", "event_key", "decision_time",
          "url", "status", "result", "settled_utc", "pnl"]


def load():
    try:
        with open(LEDGER, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def save(entries):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(entries)


def key(e):
    return f"{e['source']}|{e['market_id']}|{e['side']}"


def f(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 1. record
def record(entries, rows, snap, category_of):
    have = {key(e) for e in entries}
    added = 0
    for r in rows:
        if r.get("basis") in (None, "", "thin") or not r.get("side") or (r.get("edge") or 0) < MIN_LOG_EDGE:
            continue
        if r["source"] not in ("kalshi", "polymarket", "manifold"):   # no results API wired up yet
            continue
        if r.get("signal") == "arb_cross":   # two-leg trade; one leg's P&L would mislead
            continue
        e = {"logged_utc": snap, "source": r["source"], "market_id": str(r["market_id"]),
             "event_id": r.get("event_id") or "", "title": r.get("title") or "",
             "outcome": r.get("outcome") or "", "category": category_of(r),
             "basis": r["basis"], "signal": r.get("signal") or r["basis"],
             "side": r["side"], "fair": r.get("fair"),
             "market_prob": r.get("market_prob"), "price": r.get("price"),
             "edge": r.get("edge"), "conf": r.get("conf"),
             "fair_raw": r.get("fair_raw", r.get("fair")), "edge_raw": r.get("edge_raw", r.get("edge")),
             "sources": json.dumps({k: round(v, 4) for k, v in r["est"].items()}) if r.get("est") else "",
             "word_type": r.get("word_type") or "", "event_key": r.get("event_key") or r.get("event_id") or "",
             "decision_time": r.get("decision_time") or r.get("close_time") or "",
             "url": r.get("url") or "", "status": "open", "result": "", "settled_utc": "", "pnl": ""}
        if key(e) in have:
            continue
        entries.append(e)
        have.add(key(e))
        added += 1
    log.info("ledger: logged %d new calls (%d total)", added, len(entries))


# ---------------------------------------------------------------- 2. settle
def _get(session, url):
    """One GET without the scraper's long retry loop (a 404 is an answer here)."""
    for attempt in range(3):
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(2 ** attempt)
    raise RuntimeError(url)


def result_for(session, e):
    """'yes' / 'no' / 'void' once resolved, None while still pending."""
    src, mid = e["source"], e["market_id"]
    if src == "kalshi":
        base = "https://api.elections.kalshi.com/trade-api/v2"
        data = _get(session, f"{base}/markets/{mid}") or _get(session, f"{base}/historical/markets/{mid}")
        m = (data or {}).get("market") or {}
        res = (m.get("result") or "").lower()
        if res in ("yes", "no"):
            return res
        if res in ("void", "all_no", "all_yes") or m.get("status") == "voided":
            return "void"
        return None
    if src == "polymarket":
        m = _get(session, f"https://gamma-api.polymarket.com/markets/{mid}")
        if not m or not m.get("closed"):
            return None
        try:
            p = json.loads(m.get("outcomePrices") or "[]")
            yes = float(p[0])
        except (ValueError, IndexError, TypeError):
            return None
        return "yes" if yes >= 0.99 else "no" if yes <= 0.01 else "void"
    if src == "manifold":
        m = _get(session, f"https://api.manifold.markets/v0/market/{mid}")
        if not m or not m.get("isResolved"):
            return None
        res = (m.get("resolution") or "").upper()
        return "yes" if res == "YES" else "no" if res == "NO" else "void"
    return None


def settle(entries, session):
    now = dt.datetime.now(dt.timezone.utc)
    due = []
    for e in entries:
        if e["status"] != "open":
            continue
        try:
            t = dt.datetime.fromisoformat(e["decision_time"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        if t < now:
            due.append(e)
    due.sort(key=lambda e: e["decision_time"])
    done = 0
    for e in due[:MAX_SETTLE_PER_RUN]:
        try:
            res = result_for(session, e)
        except Exception as ex:
            log.warning("ledger: couldn't check %s %s (%s)", e["source"], e["market_id"], ex)
            continue
        if res is None:
            continue
        e["result"], e["settled_utc"] = res, now.isoformat(timespec="seconds")
        if res == "void":
            e["status"], e["pnl"] = "void", 0
        else:
            price = f(e["price"]) or 0
            won = (res == "yes") == (e["side"] == "YES")
            e["status"] = "settled"
            e["pnl"] = round((1 if won else 0) - price - fee(e["source"], price), 4)
        done += 1
        time.sleep(0.1)
    log.info("ledger: %d calls due, %d resolved this run", len(due), done)


# ---------------------------------------------------------------- 3. summarize
def bucket(edge):
    return "5–10¢" if edge < 0.10 else "10–20¢" if edge < 0.20 else "20–35¢" if edge < 0.35 else "35¢+"


def stats(group):
    n = len(group)
    pred = sum(f(e["edge"]) or 0 for e in group) / n
    pnl = sum(f(e["pnl"]) or 0 for e in group) / n
    wins = sum(1 for e in group if (f(e["pnl"]) or 0) > 0) / n
    # Brier: model fair vs market's own probability, on the YES outcome
    ys = [1.0 if e["result"] == "yes" else 0.0 for e in group]
    fm = [(f(e["fair"]), f(e["market_prob"]), y) for e, y in zip(group, ys)]
    fm = [(a, b, y) for a, b, y in fm if a is not None and b is not None]
    brier_model = sum((a - y) ** 2 for a, _, y in fm) / len(fm) if fm else None
    brier_market = sum((b - y) ** 2 for _, b, y in fm) / len(fm) if fm else None
    # ROI: profit / money put in (each call = one $1-payout contract at its entry price)
    staked = sum(f(e["price"]) or 0 for e in group)
    return {"n": n, "pred_edge": round(pred, 4), "pnl": round(pnl, 4), "total": round(pnl * n, 2),
            "win_rate": round(wins, 3), "staked": round(staked, 2),
            "roi": round(pnl * n / staked, 4) if staked > 0 else None,
            "brier_model": None if brier_model is None else round(brier_model, 4),
            "brier_market": None if brier_market is None else round(brier_market, 4)}


def signal(e):
    """Full signal name. Rows logged before the field existed: mixed-event history
    calls were the ones with confidence below 0.5."""
    if e.get("signal"):
        return e["signal"]
    if e["basis"] == "history" and (f(e.get("conf")) or 1) < 0.5:
        return "history_mixed"
    return e["basis"]


# ---------------------------------------------------------------- sources: scorecard + weights
WEIGHTS = os.path.join(ROOT, "data", "weights.json")
HALF_LIFE_DAYS = 120     # older settled calls count less
WEIGHT_PRIOR_EVENTS = 30 # settled events before fitted weights outweigh the priors
MIN_FIT_EVENTS = 10


def scored_rows(settled, now):
    """Settled calls that logged source estimates, each weighted so one event (one
    earnings call) counts once in total, and older ones fade."""
    rows = []
    for e in settled:
        try:
            est = json.loads(e.get("sources") or "{}")
        except ValueError:
            continue
        if not est or "market" not in est:
            continue
        rows.append((e, est, 1.0 if e["result"] == "yes" else 0.0))
    per_event = defaultdict(int)
    for e, _, _ in rows:
        per_event[e.get("event_key") or e["market_id"]] += 1
    out = []
    for e, est, y in rows:
        try:
            age = (now - dt.datetime.fromisoformat(e["settled_utc"])).days
        except ValueError:
            age = 0
        w = 0.5 ** (age / HALF_LIFE_DAYS) / per_event[e.get("event_key") or e["market_id"]]
        out.append((e, est, y, w))
    return out


def _loss(p, y):
    p = min(max(p, 0.01), 0.99)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def source_scorecard(rows):
    """Per source: weighted Brier score vs the market's on the same calls, events
    scored, and which word types it does best on."""
    card = {}
    names = sorted({k for _, est, _, _ in rows for k in est})
    for k in names:
        sub = [(est[k], est["market"], y, w, e) for e, est, y, w in rows if k in est]
        W = sum(w for *_, w, _ in sub)
        if not sub or W == 0:
            continue
        brier = sum(w * (p - y) ** 2 for p, _, y, w, _ in sub) / W
        brier_mkt = sum(w * (m - y) ** 2 for _, m, y, w, _ in sub) / W
        by_type = defaultdict(lambda: [0.0, 0.0, 0.0])
        for p, m, y, w, e in sub:
            t = by_type[e.get("word_type") or "?"]
            t[0] += w * (p - y) ** 2; t[1] += w * (m - y) ** 2; t[2] += w
        card[k] = {"events": len({e.get("event_key") or e["market_id"] for *_, e in sub}), "calls": len(sub),
                   "brier": round(brier, 4), "brier_market": round(brier_mkt, 4),
                   "by_type": {t: {"brier": round(a / c, 4), "brier_market": round(b / c, 4)}
                               for t, (a, b, c) in by_type.items() if c}}
    return card


def fit_weights(rows, prior):
    """Weights (>= 0) and a temperature for the log-odds pool that minimise weighted log
    loss on settled calls, with a pull toward the priors; then blended with the priors
    by how many events have settled. Plain projected gradient descent."""
    from sources import pool
    names = list(prior)
    n_events = len({e.get("event_key") or e["market_id"] for e, *_ in rows})
    if n_events < MIN_FIT_EVENTS:
        return dict(prior), 1.0, n_events, False

    def objective(vec):
        w = dict(zip(names, vec[:-1]))
        total = sum(wt * _loss(pool(est, w, vec[-1]) or est["market"], y) for _, est, y, wt in rows)
        reg = sum((vec[i] - prior[n]) ** 2 for i, n in enumerate(names)) + (vec[-1] - 1) ** 2
        return total / sum(wt for *_, wt in rows) + 0.02 * reg

    vec = [prior[n] for n in names] + [1.0]
    step, h = 0.3, 1e-4
    for _ in range(400):
        base = objective(vec)
        grad = []
        for i in range(len(vec)):
            v = vec[:]; v[i] += h
            grad.append((objective(v) - base) / h)
        vec = [max(0.0, x - step * g) for x, g in zip(vec, grad)]
        vec[-1] = min(max(vec[-1], 0.5), 2.0)
    blend = n_events / (n_events + WEIGHT_PRIOR_EVENTS)
    weights = {n: round(prior[n] + blend * (vec[i] - prior[n]), 4) for i, n in enumerate(names)}
    temp = round(1 + blend * (vec[-1] - 1), 4)
    return weights, temp, n_events, True


def update_weights(settled, snap):
    from sources import PRIOR_WEIGHTS
    now = dt.datetime.now(dt.timezone.utc)
    rows = scored_rows(settled, now)
    weights, temp, n_events, fitted = fit_weights(rows, PRIOR_WEIGHTS)
    card = source_scorecard(rows)
    with open(WEIGHTS, "w", encoding="utf-8") as fh:
        json.dump({"generated": snap, "fitted": fitted, "events": n_events, "temperature": temp,
                   "weights": weights, "prior": PRIOR_WEIGHTS}, fh, indent=1)
    return {"weights": weights, "prior": PRIOR_WEIGHTS, "temperature": temp, "events": n_events,
            "fitted": fitted, "min_events": MIN_FIT_EVENTS, "scorecard": card}


def summarize(entries, out_path, snap):
    settled = [e for e in entries if e["status"] == "settled"]
    groups = {}
    for name, keyf in (("basis", signal), ("category", lambda e: e["category"]),
                       ("source", lambda e: e["source"]),
                       ("edge size", lambda e: bucket(f(e["edge"]) or 0))):
        g = defaultdict(list)
        for e in settled:
            g[keyf(e)].append(e)
        groups[name] = {k: stats(v) for k, v in sorted(g.items())}

    # calibration for edge.py: how much of the predicted edge each signal type actually
    # delivered, shrunk toward 1.0 (no change) until it has CALIB_PRIOR resolved calls
    # measured against the model's RAW edge (before any track-record adjustment), so
    # the factor doesn't compound run after run
    raw_pred = defaultdict(list)
    for e in settled:
        raw_pred[signal(e)].append(f(e.get("edge_raw")) or f(e["edge"]) or 0)
    calib = {}
    for basis, s in groups["basis"].items():
        pred = sum(raw_pred[basis]) / len(raw_pred[basis])
        ratio = max(0.0, min(1.5, s["pnl"] / pred)) if pred > 0 else 1.0
        w = s["n"] / (s["n"] + CALIB_PRIOR)
        calib[basis] = {"n": s["n"], "delivered": round(ratio, 3), "factor": round(w * ratio + (1 - w), 3)}
    with open(CALIBRATION, "w", encoding="utf-8") as fh:
        json.dump({"generated": snap, "basis": calib}, fh, indent=1)

    fields = ["logged_utc", "source", "title", "outcome", "category", "basis", "side", "fair",
              "price", "edge", "decision_time", "url", "status", "result", "pnl"]
    recent = sorted(settled, key=lambda e: e["settled_utc"], reverse=True)[:300]
    pending = sorted((e for e in entries if e["status"] == "open"), key=lambda e: e["decision_time"])[:300]
    payload = {
        "generated": snap,
        "totals": stats(settled) if settled else {"n": 0},
        "open": sum(1 for e in entries if e["status"] == "open"),
        "void": sum(1 for e in entries if e["status"] == "void"),
        "groups": groups, "calibration": calib, "fields": fields,
        "sources": update_weights(settled, snap),
        "settled": [[e.get(k) for k in fields] for e in recent],
        "pending": [[e.get(k) for k in fields] for e in pending],
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    log.info("ledger: %d settled, %d open, %d void", len(settled), payload["open"], payload["void"])

