#!/usr/bin/env python3
"""
Valuatio bulk data fetcher — yfinance edition.

Pulls live + fundamental data from Yahoo Finance via the open-source `yfinance`
library and writes everything to data/master.csv + data/master.json.

Why yfinance:
  - Free, unlimited, no API key
  - Truly unrestricted free tier (unlike FMP which gated everything in Aug 2025)
  - Industry standard for free stock data
  - Drawback: scrapes Yahoo's web data — can break occasionally if Yahoo
    changes their internal endpoints, but yfinance is actively maintained

Designed for GitHub Actions. Reads tickers from data/tickers.txt.
No API key required — just runs.
"""

import csv
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed. Run: pip install yfinance")
    sys.exit(1)

ROOT = Path(__file__).parent
TICKERS_FILE = ROOT / "data" / "tickers.txt"
OUTPUT_CSV = ROOT / "data" / "master.csv"
OUTPUT_JSON = ROOT / "data" / "master.json"

# Yahoo can be aggressive about rate-limiting if you slam it. Be polite.
BATCH_SIZE = 50              # tickers per yf.Tickers() call
DELAY_BETWEEN_BATCHES = 1.5  # seconds


def log(*args):
    print("[fetch_data]", *args, flush=True)


def load_tickers():
    """Read tickers from data/tickers.txt, one per line."""
    if not TICKERS_FILE.exists():
        log(f"❌ {TICKERS_FILE} not found")
        sys.exit(1)
    tickers = []
    for line in TICKERS_FILE.read_text().splitlines():
        t = line.strip().upper()
        if not t or t.startswith("#"):
            continue
        tickers.append(t)
    return tickers


def load_existing():
    """Preserve fields from prior run for tickers that fail this run."""
    if not OUTPUT_CSV.exists():
        return {}
    out = {}
    with OUTPUT_CSV.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tic = row.get("ticker", "").strip().upper()
            if tic:
                out[tic] = row
    return out


def now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


# Columns matching the Valuatio app's expected schema
OUTPUT_COLUMNS = [
    "ticker",
    "name",
    "price",
    "marketcap",
    "volume",
    "volumeavg",
    "priceopen",
    "low",
    "high",
    "close",
    "change",
    "changepct",
    "closeyest",
    "date",
    "high52",
    "low52",
    "beta",
    "shares",
    "pe",
    "eps",
    "sector",
    "industry",
    "description",
    "exchange",
    "ceo",
    "country",
    "ipodate",
    "isEtf",
    "isFund",
    "isActive",
    "web_url",
    "image",
    "currency",
    "employees",
    "city",
    "state",
    "phone",
    "address",
    "dividend_yield",
    "fetched_at",
]


def safe_get(d, *keys, default=""):
    """Return first present non-None/non-empty value from a dict, trying keys in order."""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "N/A"):
            return v
    return default


def extract_ceo(info):
    """yfinance returns company officers as a list of dicts. Find the CEO."""
    officers = info.get("companyOfficers") or []
    for o in officers:
        title = str(o.get("title", "")).lower()
        if "ceo" in title or "chief executive" in title:
            return o.get("name", "")
    return ""


def fmt_ipo_date(info):
    """Some tickers have firstTradeDateEpochUtc. Convert to YYYY-MM-DD."""
    ts = info.get("firstTradeDateEpochUtc")
    if not ts:
        return ""
    try:
        return datetime.utcfromtimestamp(int(ts)).date().isoformat()
    except (TypeError, ValueError):
        return ""


def build_row(ticker, info, existing_row):
    """Map yfinance Ticker.info dict → our CSV schema."""
    row = {col: (existing_row.get(col, "") if existing_row else "") for col in OUTPUT_COLUMNS}
    row["ticker"] = ticker
    if not info:
        return row

    quote_type = (info.get("quoteType") or "").upper()
    is_etf = quote_type == "ETF"
    is_fund = quote_type in ("MUTUALFUND", "FUND")

    price = safe_get(info, "regularMarketPrice", "currentPrice", "previousClose", "navPrice")
    prev_close = safe_get(info, "regularMarketPreviousClose", "previousClose")
    change = None
    change_pct = None
    if isinstance(price, (int, float)) and isinstance(prev_close, (int, float)) and prev_close > 0:
        change = price - prev_close
        change_pct = (change / prev_close) * 100  # percent units, matches Valuatio's parsePctSheet

    row["name"]       = safe_get(info, "longName", "shortName", "displayName")
    row["price"]      = price or ""
    row["marketcap"]  = safe_get(info, "marketCap", "totalAssets")
    row["volume"]     = safe_get(info, "regularMarketVolume", "volume")
    row["volumeavg"]  = safe_get(info, "averageVolume", "averageDailyVolume10Day", "averageVolume10days")
    row["priceopen"]  = safe_get(info, "regularMarketOpen", "open")
    row["low"]        = safe_get(info, "regularMarketDayLow", "dayLow")
    row["high"]       = safe_get(info, "regularMarketDayHigh", "dayHigh")
    row["close"]      = price or ""
    row["change"]     = change if change is not None else ""
    row["changepct"]  = round(change_pct, 4) if change_pct is not None else ""
    row["closeyest"]  = prev_close or ""
    row["date"]       = now_iso()[:10]
    row["high52"]     = safe_get(info, "fiftyTwoWeekHigh")
    row["low52"]      = safe_get(info, "fiftyTwoWeekLow")
    row["beta"]       = safe_get(info, "beta", "beta3Year")
    row["shares"]     = safe_get(info, "sharesOutstanding", "impliedSharesOutstanding")
    row["pe"]         = safe_get(info, "trailingPE", "forwardPE")
    row["eps"]        = safe_get(info, "trailingEps", "forwardEps")
    row["sector"]     = safe_get(info, "sector")
    row["industry"]   = safe_get(info, "industry")
    desc = safe_get(info, "longBusinessSummary")
    if desc:
        row["description"] = desc[:1000]
    row["exchange"]   = safe_get(info, "exchange", "fullExchangeName")
    row["country"]    = safe_get(info, "country")
    row["currency"]   = safe_get(info, "currency", "financialCurrency")
    row["employees"]  = safe_get(info, "fullTimeEmployees")
    row["city"]       = safe_get(info, "city")
    row["state"]      = safe_get(info, "state")
    row["phone"]      = safe_get(info, "phone")
    row["address"]    = safe_get(info, "address1", "address2")
    row["web_url"]    = safe_get(info, "website")
    row["image"]      = safe_get(info, "logo_url")
    row["ipodate"]    = fmt_ipo_date(info)
    row["ceo"]        = extract_ceo(info)
    div_y = safe_get(info, "dividendYield", "trailingAnnualDividendYield", "yield")
    if isinstance(div_y, (int, float)):
        row["dividend_yield"] = round(div_y, 6)

    row["isEtf"]    = "TRUE" if is_etf else "FALSE"
    row["isFund"]   = "TRUE" if is_fund else "FALSE"
    row["isActive"] = "TRUE"
    row["fetched_at"] = now_iso()

    return row


def fetch_batch(tickers):
    """Fetch info for a batch of tickers via yf.Tickers().
    Returns dict: {TICKER: info_dict_or_None}
    """
    if not tickers:
        return {}
    try:
        tickers_obj = yf.Tickers(" ".join(tickers))
    except Exception as e:
        log(f"  Batch init failed: {e}")
        return {t: None for t in tickers}

    out = {}
    for t in tickers:
        try:
            tk = tickers_obj.tickers.get(t) or tickers_obj.tickers.get(t.upper())
            if tk is None:
                out[t] = None
                continue
            info = tk.info
            # Empty / unrecognized ticker check
            if not info or (not info.get("symbol") and not info.get("longName") and not info.get("shortName")):
                out[t] = None
            else:
                out[t] = info
        except Exception as e:
            log(f"  {t}: {type(e).__name__}: {str(e)[:100]}")
            out[t] = None
    return out


def main():
    log("Valuatio fetcher · yfinance edition")

    tickers = load_tickers()
    log(f"Loaded {len(tickers)} tickers")

    existing = load_existing()
    log(f"Found {len(existing)} existing rows in master.csv")

    all_info = {}
    batches = [tickers[i:i + BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
    log(f"Fetching in {len(batches)} batches of up to {BATCH_SIZE}…")

    for i, batch in enumerate(batches, 1):
        log(f"  Batch {i}/{len(batches)} ({len(batch)} tickers)")
        results = fetch_batch(batch)
        success = sum(1 for v in results.values() if v)
        log(f"    Got info for {success}/{len(batch)}")
        all_info.update(results)
        if i < len(batches):
            time.sleep(DELAY_BETWEEN_BATCHES)

    rows = []
    for tic in tickers:
        info = all_info.get(tic)
        row = build_row(tic, info, existing.get(tic))
        rows.append(row)

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    log(f"✓ Wrote {len(rows)} rows to {OUTPUT_CSV}")

    json_rows = [{k: v for k, v in r.items() if v not in ("", None)} for r in rows]
    OUTPUT_JSON.write_text(json.dumps(json_rows, separators=(",", ":")))
    log(f"✓ Wrote {len(json_rows)} rows to {OUTPUT_JSON}")

    with_price = sum(1 for r in rows if r.get("price"))
    with_name = sum(1 for r in rows if r.get("name"))
    with_sector = sum(1 for r in rows if r.get("sector"))
    log("")
    log("Summary:")
    log(f"  Total tickers:    {len(rows)}")
    log(f"  With live price:  {with_price}")
    log(f"  With name:        {with_name}")
    log(f"  With sector:      {with_sector}")
    log(f"  Failed this run:  {len(rows) - with_price}")
    if with_price == 0:
        log("")
        log("⚠ No prices returned. Possible causes:")
        log("  - Yahoo Finance temporarily rate-limited the runner")
        log("  - yfinance broke due to a Yahoo Finance change (check pypi.org/project/yfinance for updates)")
        log("  - All tickers in tickers.txt are invalid")
        sys.exit(1)


if __name__ == "__main__":
    main()
