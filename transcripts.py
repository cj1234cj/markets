#!/usr/bin/env python3
"""
Count how often each open earnings-mention word was said on past earnings calls,
using transcripts you save locally (e.g. from Seeking Alpha).

  transcripts/<CODE>/<YYYY-MM-DD>.txt     one file per call, the call date as the name
                                          CODE = Kalshi's company code (AAPL, TSLA, SBUX...),
                                          listed in transcripts/NEEDED.md

Transcripts stay on your machine (the folder is git-ignored: the repo is public and
transcripts are copyrighted). Only the per-call results are written, to
data/transcript_history.json, which the daily scrape uses as the history for the
dashboard's "History-backed" fair value.

Counting follows Kalshi's earnings-mention rules as far as text allows:
  - only company representatives and the operator count, not analysts
    (needs the "Company Participants" / "Conference Call Participants" lists that
    Seeking Alpha transcripts start with; without them the whole text is counted)
  - "Fold / Folding / Foldable" = any of those forms; plurals and possessives count
  - "Tariff (3+ times)" = said at least 3 times

Usage:
  python transcripts.py            # refresh NEEDED.md + data/transcript_history.json
  python transcripts.py --push     # ...and commit + push the results file
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
from collections import defaultdict

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
TRANSCRIPTS = os.path.join(ROOT, "transcripts")
OUT = os.path.join(ROOT, "data", "transcript_history.json")
LIVE = "https://cj1234cj.github.io/markets/data/latest.json"
PREFIX = "KXEARNINGSMENTION"
THRESHOLD = re.compile(r"\((\d+)\+\s*times?\)", re.I)
FILE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def norm(word):
    return " ".join((word or "").lower().split())


# ------------------------------------------------------------ open markets
def open_markets():
    """{code: {"company": title, "next": decision date, "words": {outcome, ...}}}"""
    data = requests.get(LIVE, params={"t": dt.datetime.now().timestamp()}, timeout=30).json()
    rows = [dict(zip(data["fields"], r)) for r in data["rows"]]
    out = {}
    for r in rows:
        series = str(r["market_id"]).split("-")[0]
        if r["source"] != "kalshi" or not series.startswith(PREFIX):
            continue
        code = series[len(PREFIX):]
        m = out.setdefault(code, {"series": series, "company": "", "next": "", "words": set()})
        name = re.match(r"What will (.+?) say during", r.get("title") or "")
        m["company"] = name.group(1) if name else r.get("title") or code
        m["next"] = min(filter(None, [m["next"], (r.get("decision_time") or "")[:10]]))
        if r.get("outcome"):
            m["words"].add(r["outcome"])
    return out


# ------------------------------------------------------------ transcript parsing
def company_text(raw):
    """Speech by company participants + operator. Seeking Alpha transcripts list
    'Company Participants' and 'Conference Call Participants' (analysts) up top,
    then each turn starts with the speaker's name on its own line."""
    lines = [l.strip() for l in raw.splitlines()]
    low = [l.lower() for l in lines]
    try:
        c0 = low.index("company participants")
        a0 = low.index("conference call participants")
    except ValueError:
        return raw, False

    def names(block):
        out = set()
        for l in block:
            if not l:
                continue
            n = re.split(r"\s+[-–—]\s+", l)[0].strip()
            if n and len(n) < 60:
                out.add(n.lower())
        return out

    company = names(lines[c0 + 1:a0])
    # the analyst list runs until the first turn: an "Operator" line, or else the
    # name line just before the first long (speech) line
    end = a0 + 1
    while end < len(lines) and low[end] != "operator" and len(lines[end]) < 80:
        end += 1
    if end < len(lines) and low[end] != "operator":
        end -= 1
        while end > a0 and not lines[end]:
            end -= 1
    analysts = names(lines[a0 + 1:end]) - company
    speakers = company | analysts | {"operator"}

    keep, current = [], "operator"
    for l in lines[end:]:
        if l.lower() in speakers:
            current = l.lower()
            continue
        if current not in analysts:
            keep.append(l)
    return "\n".join(keep), True


def word_pattern(outcome):
    """'Fold / Folding / Foldable (3+ times)' -> (regex, 3)"""
    m = THRESHOLD.search(outcome)
    need = int(m.group(1)) if m else 1
    forms = [f.strip() for f in THRESHOLD.sub("", outcome).split("/") if f.strip()]
    parts = []
    for f in forms:
        words = [re.escape(w) for w in f.split()]
        parts.append(r"[\s\-]+".join(words) + r"(?:s|es|'s|’s|s'|s’)?")
    return re.compile(r"(?<![\w])(?:" + "|".join(parts) + r")(?![\w])", re.I), need


# ------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--push", action="store_true", help="commit and push data/transcript_history.json")
    args = ap.parse_args()

    markets = open_markets()
    os.makedirs(TRANSCRIPTS, exist_ok=True)
    result, needed = {}, []
    for code, m in sorted(markets.items(), key=lambda kv: kv[1]["next"]):
        folder = os.path.join(TRANSCRIPTS, code)
        files = sorted((f for f in os.listdir(folder) if FILE_DATE.search(f)), reverse=True) \
            if os.path.isdir(folder) else []
        needed.append((m["next"], code, m["company"], len(files), len(m["words"])))
        if not files:
            continue
        calls, unattributed = [], []
        for f in files:
            date = FILE_DATE.search(f).group(1)
            with open(os.path.join(folder, f), encoding="utf-8", errors="replace") as fh:
                text, ok = company_text(fh.read())
            if not ok:
                unattributed.append(f)
            calls.append((date, text))
        words = {}
        for w in sorted(m["words"]):
            if norm(w) == "event does not qualify":
                continue
            pat, need = word_pattern(w)
            words[norm(w)] = [[d, len(pat.findall(t)), need] for d, t in calls]
        result[m["series"]] = {"code": code, "company": m["company"],
                               "calls": [d for d, _ in calls], "words": words}
        if unattributed:
            print(f"  ! {code}: no participant lists in {', '.join(unattributed)} "
                  f"(counted the whole text, analysts included)")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "series": result}, fh, indent=1, ensure_ascii=False)

    with open(os.path.join(TRANSCRIPTS, "NEEDED.md"), "w", encoding="utf-8") as fh:
        fh.write("# Transcripts needed\n\nSave each past call as `transcripts/<CODE>/<YYYY-MM-DD>.txt` "
                 "(the date of the call). Aim for the last 8 calls per company.\n\n"
                 "| Next call by | Code | Company | Open words | Transcripts saved |\n|---|---|---|---|---|\n")
        today = dt.date.today().isoformat()
        for nxt, code, company, n, nw in needed:
            if nxt and nxt < today:   # call already happened; market just hasn't settled
                continue
            fh.write(f"| {nxt} | {code} | {company} | {nw} | {n} |\n")

    have = sum(1 for *_, n, _ in needed if n)
    print(f"{len(needed)} companies with open markets; {have} have transcripts. "
          f"Wrote {os.path.relpath(OUT, ROOT)} and transcripts/NEEDED.md")

    if args.push:
        rel = os.path.relpath(OUT, ROOT)
        subprocess.run(["git", "add", rel], cwd=ROOT, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:
            subprocess.run(["git", "commit", "-m", "Update transcript history"], cwd=ROOT, check=True)
            subprocess.run(["git", "pull", "--rebase"], cwd=ROOT, check=True)
            subprocess.run(["git", "push"], cwd=ROOT, check=True)
            print("pushed; the next scrape run will use it")
        else:
            print("no changes to push")


if __name__ == "__main__":
    main()
