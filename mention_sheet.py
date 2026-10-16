"""Weekly spreadsheet of every Kalshi earnings-call mention strike deciding in the next 2 weeks.

Kalshi market data is public (no login). Pulls all open KXEARNINGSMENTION* events,
keeps those whose call date (from the event ticker, e.g. -26OCT29) falls within
--days of today, and writes one Excel file:

  Strikes  - one row per word, prices in cents
  Events   - one row per earnings call (date, strike count, volume)

Output: exports/earnings_mentions_<YYYY-MM-DD>.xlsx (git-ignored)

  python mention_sheet.py            # next 14 days
  python mention_sheet.py --days 21
  python mention_sheet.py --out site/data/earnings_mentions.xlsx   # dashboard download (CI)
"""
import argparse
import datetime as dt
import pathlib
import re
import time

import requests
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
OUT_DIR = pathlib.Path(__file__).parent / "exports"
TICKER_DATE = re.compile(r"-(\d{2}[A-Z]{3}\d{2})$")


def get_json(url, params, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** i)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2 ** i)
    raise RuntimeError(f"gave up on {url}")


def cents(m, key):
    """Kalshi prices: newer `<key>_dollars` string, else legacy integer cents."""
    d = m.get(f"{key}_dollars")
    if d not in (None, ""):
        return round(float(d) * 100)
    v = m.get(key)
    return v if isinstance(v, (int, float)) else None


def qty(m, key):
    v = m.get(f"{key}_fp", m.get(key))
    return float(v) if v not in (None, "") else None


def event_date(ticker):
    m = TICKER_DATE.search(ticker)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%y%b%d").date()
    except ValueError:
        return None


def company_name(e):
    t = re.match(r"What will (.+?) say", e.get("title", ""))
    if t:
        return t.group(1)
    src = e.get("settlement_sources") or []
    if src and src[0].get("name"):
        return re.sub(r"\s+Investor (Events|Relations)$", "", src[0]["name"])
    return e.get("title", "")


def fetch_events():
    events, cursor = [], None
    while True:
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get_json(f"{KALSHI}/events", params)
        events += [e for e in data.get("events", [])
                   if e.get("event_ticker", "").startswith("KXEARNINGSMENTION")]
        cursor = data.get("cursor")
        if not cursor:
            return events
        time.sleep(0.1)


def autosize(ws, widths=None):
    for i, col in enumerate(ws.columns, 1):
        letter = get_column_letter(i)
        if widths and letter in widths:
            ws.column_dimensions[letter].width = widths[letter]
            continue
        longest = max(len(str(c.value)) if c.value is not None else 0 for c in col)
        ws.column_dimensions[letter].width = min(max(10, longest + 2), 50)


def style_header(ws):
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="305496")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--out", help="output path (default exports/earnings_mentions_<date>.xlsx)")
    args = ap.parse_args()

    today = dt.date.today()
    horizon = today + dt.timedelta(days=args.days)
    events = fetch_events()

    strikes, calls = [], []
    for e in events:
        day = event_date(e["event_ticker"])
        if day is None or not (today <= day <= horizon):
            continue
        company = company_name(e)
        url = f"https://kalshi.com/markets/{e.get('series_ticker', '').lower()}"
        markets = [m for m in e.get("markets", []) if m.get("status") in ("active", "open", None)]
        for m in markets:
            yes_bid, yes_ask = cents(m, "yes_bid"), cents(m, "yes_ask")
            strikes.append([
                day, company, m.get("yes_sub_title") or m.get("subtitle") or "",
                yes_bid, yes_ask, cents(m, "no_bid"), cents(m, "no_ask"), cents(m, "last_price"),
                (yes_ask - yes_bid) if yes_bid is not None and yes_ask is not None else None,
                qty(m, "volume"), qty(m, "volume_24h"), qty(m, "open_interest"),
                m.get("ticker"), url,
            ])
        calls.append([day, company, e["event_ticker"], len(markets),
                      sum(qty(m, "volume") or 0 for m in markets), url])

    strikes.sort(key=lambda r: (r[0], r[1], r[2].lower()))
    calls.sort(key=lambda r: (r[0], r[1]))

    wb = Workbook()
    ws = wb.active
    ws.title = "Strikes"
    ws.append(["Call date", "Company", "Word", "Yes bid ¢", "Yes ask ¢", "No bid ¢", "No ask ¢",
               "Last ¢", "Spread ¢", "Volume", "Volume 24h", "Open interest", "Ticker", "Kalshi link"])
    for r in strikes:
        ws.append(r)
    style_header(ws)
    autosize(ws, {"N": 45})

    ws2 = wb.create_sheet("Events")
    ws2.append(["Call date", "Company", "Event ticker", "Strikes", "Total volume", "Kalshi link"])
    for r in calls:
        ws2.append(r)
    style_header(ws2)
    autosize(ws2, {"F": 45})

    for sheet in (ws, ws2):
        for (cell,) in sheet.iter_rows(min_row=2, max_col=1):
            cell.number_format = "ddd mmm d"
        sheet.cell(row=1, column=1).number_format = "General"

    out = pathlib.Path(args.out) if args.out else OUT_DIR / f"earnings_mentions_{today.isoformat()}.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(f"{len(strikes)} strikes across {len(calls)} earnings calls "
          f"({today} to {horizon}) -> {out}")


if __name__ == "__main__":
    main()
