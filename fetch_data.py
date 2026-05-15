#!/usr/bin/env python3
"""
TRAPP2 — Quote + fundamentals fetcher.

Pulls live (or 15-min delayed) quotes for every ticker in data/tickers.txt via
yfinance, plus the fundamentals snapshot. Writes:
  data/master.csv   — flat table for the app
  data/master.json  — same data, JSON shape

Run frequently (every 15 min during market hours via intraday workflow).
The expensive 5-year history fetch lives in fetch_history.py and runs nightly.

Columns produced (lowercase headers, matches what the app expects):
  ticker, name, price, marketcap, volume, volumeavg, priceopen, low, high, close,
  change, changepct, closeyest, date, high52, low52, beta, shares, pe, eps,
  sector, industry, description, exchange, ceo, country, ipodate, isetf, isfund,
  isactive, web_url, image, currency, employees, city, state, phone, address,
  dividend_yield, fetched_at, profile_fetched_at
"""
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TICKERS_FILE = DATA / "tickers.txt"
MASTER_CSV = DATA / "master.csv"
MASTER_JSON = DATA / "master.json"

# Field profile cache — fundamentals don't change intraday so we re-fetch
# the slow .info dict at most once every 24h per ticker. Quotes refresh every run.
PROFILE_CACHE = DATA / ".profile_cache.json"
PROFILE_TTL_HOURS = 24

# Columns in deterministic order for master.csv
COLUMNS = [
    "ticker", "name", "price", "marketcap", "volume", "volumeavg",
    "priceopen", "low", "high", "close", "change", "changepct", "closeyest",
    "date", "high52", "low52", "beta", "shares", "pe", "eps",
    "sector", "industry", "description", "exchange", "ceo", "country",
    "ipodate", "isetf", "isfund", "isactive", "web_url", "image",
    "currency", "employees", "city", "state", "phone", "address",
    "dividend_yield", "fetched_at", "profile_fetched_at",
]


def log(*args):
    print("[fetch_data]", *args, flush=True)


def load_tickers():
    if not TICKERS_FILE.exists():
        log(f"⚠ {TICKERS_FILE} missing — create it with one ticker per line")
        return []
    tickers = []
    for line in TICKERS_FILE.read_text().splitlines():
        t = line.strip().split("#")[0].strip().upper()
        if t:
            tickers.append(t)
    seen = set()
    out = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def load_profile_cache():
    if not PROFILE_CACHE.exists():
        return {}
    try:
        return json.loads(PROFILE_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_profile_cache(cache):
    PROFILE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_CACHE.write_text(json.dumps(cache, separators=(",", ":")))


def profile_is_fresh(cache_entry):
    if not cache_entry or "fetched_at" not in cache_entry:
        return False
    try:
        fetched = datetime.fromisoformat(cache_entry["fetched_at"])
    except (ValueError, TypeError):
        return False
    age_hours = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
    return age_hours < PROFILE_TTL_HOURS


def safe(d, *keys, default=""):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d if d is not None else default


def fmt_num(v):
    if v is None or v == "" or v == "N/A":
        return ""
    try:
        return f"{float(v):.6f}".rstrip("0").rstrip(".") if "." in str(v) else str(v)
    except (ValueError, TypeError):
        return str(v)


def fetch_quote(ticker, profile_cache):
    """Pull current quote + fundamentals for one ticker. Returns row dict."""
    t = yf.Ticker(ticker)

    # Fast price path: yfinance .fast_info is light. Falls back to history if missing.
    fast = {}
    try:
        fast = dict(t.fast_info) if t.fast_info else {}
    except Exception:
        fast = {}

    price = fast.get("last_price") or fast.get("regular_market_price")
    prev_close = fast.get("previous_close") or fast.get("regular_market_previous_close")
    open_p = fast.get("open")
    day_high = fast.get("day_high")
    day_low = fast.get("day_low")
    high52 = fast.get("year_high")
    low52 = fast.get("year_low")
    vol = fast.get("last_volume") or fast.get("regular_market_volume")

    # Backstop: pull last 2 bars via history if fast_info lacked anything
    if price is None or prev_close is None:
        try:
            hist = t.history(period="5d", interval="1d", auto_adjust=False)
            if len(hist) >= 1:
                price = price or float(hist["Close"].iloc[-1])
                if len(hist) >= 2:
                    prev_close = prev_close or float(hist["Close"].iloc[-2])
                open_p = open_p or float(hist["Open"].iloc[-1])
                day_high = day_high or float(hist["High"].iloc[-1])
                day_low = day_low or float(hist["Low"].iloc[-1])
                vol = vol or float(hist["Volume"].iloc[-1])
        except Exception as e:
            log(f"  ✗ {ticker} history backstop failed: {e}")

    if price is None:
        return None

    change = (price - prev_close) if prev_close else None
    changepct = (change / prev_close) if change is not None and prev_close else None

    # Fundamentals — heavy call; cache it 24h
    cached = profile_cache.get(ticker, {})
    info = cached.get("info") if profile_is_fresh(cached) else None
    profile_fetched_at = cached.get("fetched_at", "")
    if info is None:
        try:
            info = t.info or {}
            profile_fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            profile_cache[ticker] = {"info": info, "fetched_at": profile_fetched_at}
        except Exception as e:
            log(f"  ⚠ {ticker} .info failed: {e}")
            info = cached.get("info") or {}

    row = {
        "ticker": ticker,
        "name": safe(info, "longName") or safe(info, "shortName"),
        "price": fmt_num(price),
        "marketcap": fmt_num(safe(info, "marketCap")),
        "volume": fmt_num(vol),
        "volumeavg": fmt_num(safe(info, "averageVolume")),
        "priceopen": fmt_num(open_p),
        "low": fmt_num(day_low),
        "high": fmt_num(day_high),
        "close": fmt_num(price),
        "change": fmt_num(change),
        "changepct": fmt_num(changepct * 100 if changepct is not None else ""),
        "closeyest": fmt_num(prev_close),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "high52": fmt_num(high52),
        "low52": fmt_num(low52),
        "beta": fmt_num(safe(info, "beta")),
        "shares": fmt_num(safe(info, "sharesOutstanding")),
        "pe": fmt_num(safe(info, "trailingPE")),
        "eps": fmt_num(safe(info, "trailingEps")),
        "sector": safe(info, "sector"),
        "industry": safe(info, "industry"),
        "description": (safe(info, "longBusinessSummary") or "")[:2000],
        "exchange": safe(info, "exchange"),
        "ceo": (info.get("companyOfficers") or [{}])[0].get("name", "") if isinstance(info.get("companyOfficers"), list) and info.get("companyOfficers") else "",
        "country": safe(info, "country"),
        "ipodate": safe(info, "ipoExpectedDate") or safe(info, "firstTradeDateEpochUtc"),
        "isetf": "true" if safe(info, "quoteType") == "ETF" else "false",
        "isfund": "true" if safe(info, "quoteType") in ("MUTUALFUND", "FUND") else "false",
        "isactive": "true",
        "web_url": safe(info, "website"),
        "image": "",  # yfinance dropped logo_url. App synthesizes via Clearbit using web_url.
        "currency": safe(info, "currency"),
        "employees": fmt_num(safe(info, "fullTimeEmployees")),
        "city": safe(info, "city"),
        "state": safe(info, "state"),
        "phone": safe(info, "phone"),
        "address": safe(info, "address1") or safe(info, "address"),
        "dividend_yield": fmt_num(safe(info, "dividendYield")),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile_fetched_at": profile_fetched_at,
    }
    return row


def main():
    tickers = load_tickers()
    if not tickers:
        log("No tickers to fetch. Exiting.")
        return 1
    log(f"Fetching {len(tickers)} tickers via yfinance…")

    profile_cache = load_profile_cache()
    rows = []
    n_ok = 0
    for i, tic in enumerate(tickers, 1):
        try:
            row = fetch_quote(tic, profile_cache)
            if row:
                rows.append(row)
                n_ok += 1
            else:
                log(f"  ✗ {tic}: no price data")
        except Exception as e:
            log(f"  ✗ {tic}: {e}")
        if i % 25 == 0:
            log(f"  … {i}/{len(tickers)} ({n_ok} OK)")
            save_profile_cache(profile_cache)
        time.sleep(0.05)

    save_profile_cache(profile_cache)

    DATA.mkdir(parents=True, exist_ok=True)
    with MASTER_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    MASTER_JSON.write_text(json.dumps(rows, separators=(",", ":")))

    log(f"✓ Wrote {n_ok}/{len(tickers)} rows to {MASTER_CSV.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
