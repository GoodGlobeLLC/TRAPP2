#!/usr/bin/env python3
"""
TRAPP2 — 20-year daily price history fetcher (batched + resumable).

Pulls up to 20 years of daily closes for every ticker in data/tickers.txt
(plus the required reference tickers the signal engine needs). Writes one file
per ticker plus a manifest:
  data/history/<TICKER>.json   — [{date, close, volume}, ...] ascending
  data/history_manifest.json   — tickers that have history + their span

WHY BATCHED
-----------
With 250+ tickers in tickers.txt (plus ~90 reference tickers ≈ 340 total) and
20 years of daily bars each (~5,000 rows, ~120-180 KB per file), fetching and
committing them all in ONE workflow run risks Yahoo rate-limiting, a multi-hour
job, and a single enormous commit. So this script supports BATCHING:

    python fetch_history.py                      # all tickers, one pass
    python fetch_history.py --batch 0 --of 4     # tickers 0,4,8,...  (stripe 0 of 4)
    python fetch_history.py --batch 1 --of 4     # tickers 1,5,9,...  (stripe 1 of 4)
    ...

Each batch writes only its own tickers' files and updates the manifest by
MERGING (it reads the existing manifest, adds its tickers, never drops others).
So four parallel/sequential batches together cover the whole universe, and each
batch's commit is ~1/4 the size.

SKIP-IF-FRESH
-------------
A ticker whose history file already ends within --max-age-days of today is
skipped (history doesn't change retroactively; only the latest day is new, and
the nightly 5-day-tail top-up — fetch_history_tail.py, if present — handles that
cheaply). Full 20y refetch only happens for new/stale tickers. Override with
--force to refetch everything.

YEARS
-----
Default is 20 years. yfinance honors period="20y" but many tickers simply don't
have that much history (recent IPOs, new crypto, new ETFs); for those Yahoo
returns whatever exists, which is correct — we store the full available span and
record first/last date in the manifest so the app knows each ticker's real
coverage.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TICKERS_FILE = DATA / "tickers.txt"
HISTORY_DIR = DATA / "history"
MANIFEST = DATA / "history_manifest.json"

DEFAULT_YEARS = 20


def log(*args):
    print("[fetch_history]", *args, flush=True)


# Reference tickers the signal engine REQUIRES — auto-merged with tickers.txt.
REQUIRED_REFERENCE_TICKERS = [
    "SPY", "QQQ", "DIA", "IWM",
    "^VIX", "^VIX9D", "^MOVE",
    "HYG", "LQD", "JNK", "EMB",
    "XLK", "XLF", "XLV", "XLE", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC",
    "ES=F", "NQ=F", "YM=F", "RTY=F",
    "CL=F", "BZ=F", "NG=F", "RB=F", "HO=F",
    "GC=F", "SI=F", "HG=F", "PL=F", "PA=F",
    "ZC=F", "ZS=F", "ZW=F", "KC=F", "SB=F", "CC=F", "CT=F",
    "ZB=F", "ZN=F", "ZF=F", "ZT=F",
    "BTC-USD", "ETH-USD", "SOL-USD",
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "USDCHF=X",
    "NZDUSD=X", "USDCNY=X", "USDINR=X", "USDMXN=X",
    "DX=F",
    "^GSPTSE", "^MERV", "^BVSP", "^MXX", "^FTSE", "^GDAXI", "^FCHI",
    "^STOXX50E", "^N225", "^HSI", "^AXJO", "^BSESN", "^KS11", "^TWII",
    "UUP",
    "TLT", "IEF", "SHY", "BND", "AGG", "GOVT", "TIP",
]


def load_tickers():
    if not TICKERS_FILE.exists():
        log(f"⚠ {TICKERS_FILE} missing — using reference tickers only")
        base = list(REQUIRED_REFERENCE_TICKERS)
    else:
        base = []
        for line in TICKERS_FILE.read_text().splitlines():
            t = line.strip().split("#")[0].strip().upper()
            if t:
                base.append(t)
        base = base + REQUIRED_REFERENCE_TICKERS
    seen, out = set(), []
    for t in base:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def existing_last_date(ticker):
    """Return the last date already stored for this ticker, or None."""
    p = HISTORY_DIR / f"{ticker}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        if isinstance(data, list) and data:
            return data[-1].get("date")
    except Exception:
        return None
    return None


def is_fresh(ticker, max_age_days):
    last = existing_last_date(ticker)
    if not last:
        return False
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (datetime.now(timezone.utc).date() - last_dt).days <= max_age_days


def fetch_one(ticker, years):
    """Return a list of {date, close, volume} dicts (ascending) or None.

    Uses an explicit start..end window that ENDS YESTERDAY (UTC), never today.
    This is deliberate: today's bar is the live/intraday price, which the quote
    and nightly pipelines own. Backfilling only settled history (through
    yesterday) means this job can never overwrite the current live price on a
    chart — it only fills in the older daily bars.
    """
    try:
        today = datetime.now(timezone.utc).date()
        end = today                      # yfinance 'end' is EXCLUSIVE, so end=today => last bar = yesterday
        start = end - timedelta(days=int(years * 365.25) + 5)
        t = yf.Ticker(ticker)
        df = t.history(start=start.isoformat(), end=end.isoformat(),
                       interval="1d", auto_adjust=False)
    except Exception as e:
        log(f"  ✗ {ticker}: {e}")
        return None
    if df is None or df.empty:
        return None
    bars = []
    for idx, row in df.iterrows():
        try:
            close = float(row["Close"])
        except (KeyError, ValueError, TypeError):
            continue
        if not close or close <= 0:
            continue
        date_iso = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
        try:
            vol = int(row["Volume"]) if row["Volume"] is not None else 0
        except (KeyError, ValueError, TypeError):
            vol = 0
        bars.append({"date": date_iso, "close": round(close, 4), "volume": vol})
    bars.sort(key=lambda b: b["date"])
    return bars if bars else None


def load_manifest():
    if MANIFEST.exists():
        try:
            m = json.loads(MANIFEST.read_text())
            if isinstance(m, dict):
                return m
        except Exception:
            pass
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=0, help="This batch's stripe index (0-based)")
    ap.add_argument("--of", type=int, default=1, help="Total number of batches")
    ap.add_argument("--years", type=int, default=DEFAULT_YEARS, help="Years of history to fetch")
    ap.add_argument("--max-age-days", type=int, default=4,
                    help="Skip a ticker whose stored history is at most this many days old")
    ap.add_argument("--force", action="store_true", help="Refetch even fresh tickers")
    args = ap.parse_args()

    if args.of < 1:
        args.of = 1
    if not (0 <= args.batch < args.of):
        log(f"⚠ --batch {args.batch} out of range for --of {args.of}; using 0")
        args.batch = 0

    all_tickers = load_tickers()
    # Stripe selection: ticker i goes to batch (i % of). Striping (not slicing)
    # spreads heavy/slow tickers evenly across batches so no batch is lopsided.
    tickers = [t for i, t in enumerate(all_tickers) if i % args.of == args.batch]

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Batch {args.batch+1}/{args.of}: {len(tickers)} of {len(all_tickers)} tickers · "
        f"{args.years}y history → {HISTORY_DIR}")

    # Span info we collect per ticker for the manifest.
    spans = {}  # ticker -> {first, last, rows}
    successful, skipped_fresh, failed = [], [], []

    for i, tic in enumerate(tickers, 1):
        if not args.force and is_fresh(tic, args.max_age_days):
            skipped_fresh.append(tic)
            # Still record its span from the existing file for the manifest merge.
            last = existing_last_date(tic)
            if last:
                spans[tic] = {"last": last}
            continue
        bars = fetch_one(tic, args.years)
        if not bars:
            failed.append(tic)
            continue
        out_path = HISTORY_DIR / f"{tic}.json"
        # MERGE with whatever is already stored so we never DROP bars we don't
        # re-fetch — in particular, if the live/nightly pipeline already wrote
        # today's bar (newer than our yesterday end), keep it. Union by date,
        # newest write wins per date, ascending order preserved.
        existing = []
        if out_path.exists():
            try:
                prev = json.loads(out_path.read_text())
                if isinstance(prev, list):
                    existing = prev
            except Exception:
                existing = []
        by_date = {}
        for b in existing:
            d = b.get("date")
            if d:
                by_date[d] = b
        for b in bars:          # our freshly-fetched settled history overwrites same-date entries
            by_date[b["date"]] = b
        merged = [by_date[d] for d in sorted(by_date.keys())]
        out_path.write_text(json.dumps(merged, separators=(",", ":")))
        spans[tic] = {"first": merged[0]["date"], "last": merged[-1]["date"], "rows": len(merged)}
        successful.append(tic)
        if i % 20 == 0:
            log(f"  … {i}/{len(tickers)} · {len(successful)} fetched · "
                f"{len(skipped_fresh)} fresh-skip · {len(failed)} failed")
        time.sleep(0.08)  # gentle pacing for Yahoo

    # ----- Write a per-BATCH manifest fragment (avoids parallel clobber) -----
    # When 4 batches run in parallel, each reading+writing the SAME manifest file
    # races — the last writer wins and drops the others' entries. So each batch
    # writes its own fragment (history_manifest.b<batch>.json); a merge job (or
    # the next run) combines them. We ALSO update the shared manifest optimistically
    # for backward-compat, but the fragments are the source of truth for merging.
    frag = HISTORY_DIR.parent / f"history_manifest.b{args.batch}.json"
    frag.write_text(json.dumps({
        "batch": args.batch, "of": args.of,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "years": args.years,
        "by_ticker": spans,
    }, separators=(",", ":")))

    # ----- MERGE into the manifest (best-effort; fragments are authoritative) -----
    manifest = load_manifest()
    by_ticker = manifest.get("by_ticker", {})
    if not by_ticker and isinstance(manifest.get("tickers"), list):
        for t in manifest["tickers"]:
            by_ticker.setdefault(t, {})
    for t, span in spans.items():
        prev = by_ticker.get(t, {})
        prev.update(span)
        by_ticker[t] = prev

    all_have_history = sorted(by_ticker.keys())
    new_manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(all_have_history),
        "years": args.years,
        "tickers": all_have_history,          # kept for backward-compat with the app
        "by_ticker": by_ticker,               # new: per-ticker {first,last,rows}
        "last_batch": {"batch": args.batch, "of": args.of,
                       "at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
    }
    MANIFEST.write_text(json.dumps(new_manifest, separators=(",", ":")))

    log(f"✓ Batch {args.batch+1}/{args.of} done: {len(successful)} fetched, "
        f"{len(skipped_fresh)} fresh-skipped, {len(failed)} failed")
    log(f"✓ Manifest now covers {len(all_have_history)} tickers total")
    if failed:
        log(f"  Failed: {', '.join(failed[:12])}{'…' if len(failed) > 12 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
