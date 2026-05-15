#!/usr/bin/env python3
"""
Valuatio bulk PRICE HISTORY fetcher — yfinance edition.

Pulls 5 years of daily closing prices for every ticker in data/tickers.txt
and writes them in wide format to data/prices.csv (also data/prices.json).

Wide format example:
    date,AAPL,MSFT,NVDA
    2020-05-12,72.50,184.91,8.95
    2020-05-13,71.13,182.69,8.81
    ...

This matches the format that Valuatio's existing sheet parser already
understands (Layout 3 — Date in col A, tickers across row 1).

Incremental updates:
  - First run: pulls 5 years for every ticker
  - Later runs: only fetches NEW dates (since last update) for existing
    tickers. New tickers get a full 5-year backfill on first sight.
  - Result: most runs are quick (a few new days per ticker) and stay
    well under any rate limits.
"""

import csv
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed. Run: pip install yfinance")
    sys.exit(1)

ROOT = Path(__file__).parent
TICKERS_FILE = ROOT / "data" / "tickers.txt"
OUTPUT_CSV = ROOT / "data" / "prices.csv"
OUTPUT_JSON = ROOT / "data" / "prices.json"

# How far back to pull on first sight of a new ticker
HISTORY_YEARS = 5

# Batch tickers per yf.download() call. yfinance handles multi-ticker downloads
# more efficiently than per-ticker history() calls.
BATCH_SIZE = 50
DELAY_BETWEEN_BATCHES = 1.5  # seconds


def log(*args):
    print("[fetch_history]", *args, flush=True)


def load_tickers():
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


def load_existing_prices():
    """Load existing prices.csv into a dict-of-dicts: {date: {ticker: price}}.
    Returns ({}, set()) if no file yet.
    """
    if not OUTPUT_CSV.exists():
        return {}, set()
    prices = {}  # {date: {ticker: float}}
    tickers_seen = set()
    with OUTPUT_CSV.open("r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or header[0].lower() != "date":
            log("⚠ Existing prices.csv has unexpected format, ignoring it")
            return {}, set()
        col_tickers = header[1:]
        tickers_seen.update(col_tickers)
        for row in reader:
            if not row:
                continue
            date = row[0]
            if not date:
                continue
            prices[date] = {}
            for i, ticker in enumerate(col_tickers, start=1):
                if i < len(row) and row[i].strip():
                    try:
                        prices[date][ticker] = float(row[i])
                    except ValueError:
                        pass
    return prices, tickers_seen


def determine_start_date(ticker, existing_prices, tickers_seen):
    """Decide how far back to fetch for a given ticker.
    - If new ticker: 5 years back
    - If existing ticker: from last seen date forward (small incremental fetch)
    """
    five_years_ago = (datetime.now(timezone.utc).replace(tzinfo=None)
                      - timedelta(days=HISTORY_YEARS * 365 + 5)).date().isoformat()
    if ticker not in tickers_seen:
        return five_years_ago  # New ticker — full backfill

    # Find the most recent date this ticker has data for
    latest = None
    for date_iso, prices_by_tic in existing_prices.items():
        if ticker in prices_by_tic:
            if latest is None or date_iso > latest:
                latest = date_iso
    if latest is None:
        return five_years_ago

    # Resume from the day after the last known date
    latest_dt = datetime.fromisoformat(latest)
    next_dt = latest_dt + timedelta(days=1)
    return next_dt.date().isoformat()


def fetch_history_batch(tickers, start_date):
    """Fetch daily history for a batch of tickers from start_date to today.
    Returns dict: {ticker: {date_iso: close_price}}
    """
    if not tickers:
        return {}
    end_date = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)).date().isoformat()

    try:
        # yf.download with group_by='ticker' returns a hierarchical DataFrame
        # auto_adjust=True applies split/dividend adjustments (cleaner for chart math)
        df = yf.download(
            tickers=" ".join(tickers),
            start=start_date,
            end=end_date,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        log(f"  yf.download failed: {type(e).__name__}: {str(e)[:120]}")
        return {}

    out = {}
    if df is None or df.empty:
        return {t: {} for t in tickers}

    # Handle the two shapes yfinance returns depending on ticker count
    if len(tickers) == 1:
        # Single-ticker: flat columns
        t = tickers[0]
        out[t] = {}
        if "Close" in df.columns:
            for idx, row in df.iterrows():
                d = idx.strftime("%Y-%m-%d")
                close = row["Close"]
                if close is not None and not (isinstance(close, float) and (close != close)):  # NaN check
                    out[t][d] = float(close)
        return out

    # Multi-ticker: hierarchical (ticker, field) columns
    for t in tickers:
        out[t] = {}
        try:
            # Try ticker as outermost level
            sub = df[t] if t in df.columns.get_level_values(0) else None
            if sub is None or "Close" not in sub.columns:
                continue
            for idx, val in sub["Close"].items():
                if val is None:
                    continue
                if isinstance(val, float) and val != val:  # NaN
                    continue
                d = idx.strftime("%Y-%m-%d")
                out[t][d] = float(val)
        except (KeyError, AttributeError) as e:
            log(f"  {t}: failed to extract: {e}")
    return out


def merge_into_existing(existing, new_data):
    """Merge new fetched data into the existing prices dict.
    existing: {date: {ticker: price}}
    new_data: {ticker: {date: price}}
    """
    for ticker, by_date in new_data.items():
        for date_iso, price in by_date.items():
            if date_iso not in existing:
                existing[date_iso] = {}
            existing[date_iso][ticker] = price
    return existing


def write_outputs(all_prices, all_tickers):
    """Write prices.csv (wide format) and prices.json."""
    sorted_dates = sorted(all_prices.keys())
    sorted_tickers = sorted(all_tickers)

    # CSV (wide format — matches Valuatio's existing Layout 3 parser)
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date"] + sorted_tickers)
        for date_iso in sorted_dates:
            row = [date_iso]
            row_prices = all_prices[date_iso]
            for t in sorted_tickers:
                p = row_prices.get(t)
                # 4 decimal places balances precision against file size
                row.append(f"{p:.4f}" if p is not None else "")
            writer.writerow(row)
    log(f"✓ Wrote {len(sorted_dates)} dates × {len(sorted_tickers)} tickers to {OUTPUT_CSV}")

    # JSON — keyed by ticker for fast lookup in the app.
    # Shape: {ticker: [{date: "YYYY-MM-DD", price: float}, ...]}
    json_out = {}
    for t in sorted_tickers:
        series = []
        for date_iso in sorted_dates:
            p = all_prices[date_iso].get(t)
            if p is not None:
                series.append({"date": date_iso, "price": round(p, 4)})
        if series:
            json_out[t] = series
    OUTPUT_JSON.write_text(json.dumps(json_out, separators=(",", ":")))
    sz_kb = OUTPUT_JSON.stat().st_size // 1024
    log(f"✓ Wrote {len(json_out)} ticker series to {OUTPUT_JSON} ({sz_kb} KB)")


def main():
    log("Valuatio history fetcher · yfinance edition")
    tickers = load_tickers()
    log(f"Loaded {len(tickers)} tickers")

    existing_prices, tickers_seen = load_existing_prices()
    n_existing_dates = len(existing_prices)
    log(f"Existing prices.csv: {n_existing_dates} dates, {len(tickers_seen)} tickers")

    # Decide start date per ticker
    new_tickers = [t for t in tickers if t not in tickers_seen]
    existing_tickers = [t for t in tickers if t in tickers_seen]
    log(f"  New tickers (5yr backfill):  {len(new_tickers)}")
    log(f"  Existing (incremental):      {len(existing_tickers)}")

    # ── Phase 1: incremental for existing tickers ──
    if existing_tickers:
        # Group by start date so we can batch tickers that need the same range
        # For simplicity: use the EARLIEST resume date across all existing tickers
        # (yfinance handles per-ticker date alignment internally)
        resume_dates = [determine_start_date(t, existing_prices, tickers_seen) for t in existing_tickers]
        earliest_resume = min(resume_dates) if resume_dates else None
        today_iso = datetime.now(timezone.utc).date().isoformat()
        if earliest_resume and earliest_resume <= today_iso:
            log(f"Incremental fetch from {earliest_resume} → today for {len(existing_tickers)} tickers")
            batches = [existing_tickers[i:i + BATCH_SIZE] for i in range(0, len(existing_tickers), BATCH_SIZE)]
            for i, batch in enumerate(batches, 1):
                log(f"  Batch {i}/{len(batches)} ({len(batch)} tickers)")
                result = fetch_history_batch(batch, earliest_resume)
                total_new = sum(len(v) for v in result.values())
                log(f"    Got {total_new} new data points")
                merge_into_existing(existing_prices, result)
                if i < len(batches):
                    time.sleep(DELAY_BETWEEN_BATCHES)
        else:
            log("Existing tickers all up-to-date — skipping incremental")

    # ── Phase 2: full backfill for new tickers ──
    if new_tickers:
        five_years_ago = (datetime.now(timezone.utc).replace(tzinfo=None)
                          - timedelta(days=HISTORY_YEARS * 365 + 5)).date().isoformat()
        log(f"Backfilling {len(new_tickers)} new tickers from {five_years_ago}")
        batches = [new_tickers[i:i + BATCH_SIZE] for i in range(0, len(new_tickers), BATCH_SIZE)]
        for i, batch in enumerate(batches, 1):
            log(f"  Backfill batch {i}/{len(batches)} ({len(batch)} tickers)")
            result = fetch_history_batch(batch, five_years_ago)
            total_new = sum(len(v) for v in result.values())
            log(f"    Got {total_new} data points")
            merge_into_existing(existing_prices, result)
            if i < len(batches):
                time.sleep(DELAY_BETWEEN_BATCHES)

    # ── Write outputs ──
    all_tickers_to_write = set(tickers) | tickers_seen
    write_outputs(existing_prices, all_tickers_to_write)

    # ── Summary ──
    coverage = {}
    for t in tickers:
        count = sum(1 for d in existing_prices if t in existing_prices[d])
        coverage[t] = count
    avg_coverage = sum(coverage.values()) / len(coverage) if coverage else 0
    missing = [t for t, c in coverage.items() if c == 0]
    log("")
    log("Summary:")
    log(f"  Total tickers:         {len(tickers)}")
    log(f"  Total dates:           {len(existing_prices)}")
    log(f"  Avg history per tic:   {avg_coverage:.0f} bars")
    log(f"  Tickers with no data:  {len(missing)}")
    if missing:
        log(f"    Missing: {', '.join(missing[:10])}{'…' if len(missing) > 10 else ''}")


if __name__ == "__main__":
    main()
