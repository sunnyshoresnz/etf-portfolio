#!/usr/bin/env python3
"""Add today's NZX prices for the portfolio's ETFs to prices.json.

Runs on a schedule. Before 5 pm New Zealand time it records the price so far
("intraday"); from 5 pm it records the closing price ("close"). A quote is only
saved when its own timestamp is today's, so an out-of-date copy of a price page
is never saved as today's price. Every run writes what it found to status.json.
"""
import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NZ = ZoneInfo("Pacific/Auckland")
TICKERS = ["TWF", "FNZ"]
PRICES_FILE = "prices.json"
STATUS_FILE = "status.json"
MAX_DAILY_MOVE = 0.15          # reject a price more than 15% from the previous trading day's
CLOSE_RETRIES = int(os.environ.get("CLOSE_RETRIES", "2"))
RETRY_WAIT_SECONDS = int(os.environ.get("RETRY_WAIT_SECONDS", "600"))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def page_text(raw_html):
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw_html)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text))


def parse_nzx(text, ticker, now):
    """Return (price, quote_time) from the text of an nzx.com instrument page."""
    m = re.search(r"\(\s*" + re.escape(ticker) + r"\s*\)\s*\$\s*([0-9]+\.[0-9]+)\s*NZD", text)
    if not m:
        raise ValueError("price not found on page")
    price = float(m.group(1))
    u = re.search(r"Last Updated:?\s*(?:[A-Za-z]{3,9},?\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+"
                  r"([A-Za-z]{3})[A-Za-z]*,?\s*(\d{1,2}):(\d{2})", text)
    if not u:
        raise ValueError("'Last Updated' time not found on page")
    day, mon = int(u.group(1)), MONTHS[u.group(2).lower()]
    hh, mm = int(u.group(3)), int(u.group(4))
    when = datetime(now.year, mon, day, hh, mm, tzinfo=NZ)
    if when > now + timedelta(days=2):      # e.g. a 31 Dec quote read on 2 Jan
        when = when.replace(year=now.year - 1)
    return price, when


def nzx_quote(ticker, now):
    raw = fetch(f"https://www.nzx.com/instruments/{ticker}?refresh={int(time.time())}")
    return parse_nzx(page_text(raw), ticker, now)


def parse_yahoo(raw_json):
    meta = json.loads(raw_json)["chart"]["result"][0]["meta"]
    if meta.get("currency") not in (None, "NZD"):
        raise ValueError(f"unexpected currency {meta.get('currency')}")
    price = float(meta["regularMarketPrice"])
    when = datetime.fromtimestamp(int(meta["regularMarketTime"]), NZ)
    return price, when


def yahoo_quote(ticker):
    last_error = None
    for host in ("query1", "query2"):
        try:
            raw = fetch(f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}.NZ"
                        f"?range=5d&interval=1d&refresh={int(time.time())}")
            return parse_yahoo(raw)
        except Exception as exc:  # try the other host
            last_error = exc
    raise last_error


def pick(ticker, now, closing, nzx, yahoo):
    """Choose today's price from the quotes, or None if neither is today's."""
    today = now.date()
    after_close = lambda t: (t.hour, t.minute) >= (16, 45)
    in_session = lambda t: t.hour >= 10   # NZX opens at 10 am; earlier stamps carry yesterday's price
    if closing:
        if nzx and nzx[1].date() == today and after_close(nzx[1]):
            return nzx[0], nzx[1], "close", "nzx.com"
        if yahoo and yahoo[1].date() == today and after_close(yahoo[1]):
            return yahoo[0], yahoo[1], "close", "Yahoo Finance"
        return None
    if yahoo and yahoo[1].date() == today and in_session(yahoo[1]):
        return yahoo[0], yahoo[1], "intraday", "Yahoo Finance"
    if nzx and nzx[1].date() == today and in_session(nzx[1]):
        return nzx[0], nzx[1], "intraday", "nzx.com"
    return None


def previous_price(rows, ticker, today_iso):
    earlier = sorted((r for r in rows if r["ticker"] == ticker and r["date"] < today_iso),
                     key=lambda r: r["date"])
    return float(earlier[-1]["price"]) if earlier else None


def merge(rows, ticker, today_iso, price, when, status, source):
    """Add or replace today's row for ticker. Returns (rows, changed, note)."""
    prev = previous_price(rows, ticker, today_iso)
    if prev and abs(price / prev - 1) > MAX_DAILY_MOVE:
        return rows, False, f"rejected {price}: more than 15% from previous {prev}"
    existing = next((r for r in rows if r["ticker"] == ticker and r["date"] == today_iso), None)
    if existing and existing.get("status") == "close" and status == "intraday":
        return rows, False, "kept today's closing price"
    new = {"date": today_iso, "ticker": ticker, "price": round(price, 4), "status": status,
           "as_of": when.isoformat(timespec="seconds"), "source": source}
    if existing == new:
        return rows, False, "unchanged"
    rows = [r for r in rows if not (r["ticker"] == ticker and r["date"] == today_iso)] + [new]
    rows.sort(key=lambda r: (r["date"], r["ticker"]))
    return rows, True, "saved"


def describe(quote):
    if isinstance(quote, Exception):
        return {"error": f"{type(quote).__name__}: {quote}"[:300]}
    return {"price": quote[0], "time": quote[1].isoformat(timespec="minutes")}


def main():
    now = datetime.now(NZ)
    status = {"run_at": now.isoformat(timespec="seconds"), "results": {}}
    with open(PRICES_FILE) as f:
        rows = json.load(f)

    if now.weekday() >= 5:
        status["mode"] = "weekend: nothing to do"
        write_status(status)
        return

    closing = now.hour >= 17
    status["mode"] = "close" if closing else "intraday"
    today_iso = now.date().isoformat()
    pending = list(TICKERS)
    attempts = 0
    changed_any = False

    while True:
        attempts += 1
        now = datetime.now(NZ)
        for ticker in list(pending):
            try:
                nzx = nzx_quote(ticker, now)
            except Exception as exc:
                nzx = exc
            try:
                yahoo = yahoo_quote(ticker)
            except Exception as exc:
                yahoo = exc
            chosen = pick(ticker, now, closing,
                          None if isinstance(nzx, Exception) else nzx,
                          None if isinstance(yahoo, Exception) else yahoo)
            result = {"nzx": describe(nzx), "yahoo": describe(yahoo), "attempt": attempts}
            if chosen:
                rows, changed, note = merge(rows, ticker, today_iso, *chosen)
                changed_any |= changed
                result.update({"used": chosen[3], "status": chosen[2], "price": chosen[0], "note": note})
                pending.remove(ticker)
            else:
                result["note"] = "no quote from today yet"
            status["results"][ticker] = result
        if not pending or not closing or attempts > CLOSE_RETRIES:
            break
        time.sleep(RETRY_WAIT_SECONDS)

    if changed_any:
        with open(PRICES_FILE, "w") as f:
            f.write("[\n" + ",\n".join("  " + json.dumps(r) for r in rows) + "\n]\n")
    status["prices_changed"] = changed_any
    write_status(status)


def write_status(status):
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)
        f.write("\n")
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    sys.exit(main())
