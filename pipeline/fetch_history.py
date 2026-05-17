#!/usr/bin/env python3
"""
TRAPP2 — 5-year daily price history fetcher.

Pulls 5 years of daily closes for every ticker in data/tickers.txt.
Writes one file per ticker plus a manifest:
  data/history/<TICKER>.json   — [{date, close, volume}, ...] ascending
  data/history_manifest.json   — list of tickers that have history available

Each per-ticker file is small (~25-40 KB) so 200+ tickers commit fast.

The app's portfolio chart + sector ETF table + bond ETF table all read from
these files lazily via the history_manifest.json registered URL.

Run nightly (midnight UTC = 7pm ET) from the workflow. Skip on intraday runs.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TICKERS_FILE = DATA / "tickers.txt"
HISTORY_DIR = DATA / "history"
MANIFEST = DATA / "history_manifest.json"


def log(*args):
    print("[fetch_history]", *args, flush=True)


# Reference tickers the signal engine REQUIRES — auto-merged with tickers.txt
# so the user doesn't have to remember to add them.
# Sector ETFs, market index, vol, credit + new asset classes (crypto/futures/FX).
REQUIRED_REFERENCE_TICKERS = [
    # US equity / volatility benchmarks
    "SPY", "QQQ", "DIA", "IWM",
    "^VIX", "^VIX9D", "^MOVE",
    # Credit
    "HYG", "LQD", "JNK", "EMB",
    # Sector ETFs (11 SPDRs)
    "XLK", "XLF", "XLV", "XLE", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC",
    # Equity-index futures
    "ES=F", "NQ=F", "YM=F", "RTY=F",
    # Energy futures
    "CL=F", "BZ=F", "NG=F", "RB=F", "HO=F",
    # Metal futures
    "GC=F", "SI=F", "HG=F", "PL=F", "PA=F",
    # Agricultural / grain futures
    "ZC=F", "ZS=F", "ZW=F", "KC=F", "SB=F", "CC=F", "CT=F",
    # Interest rate futures
    "ZB=F", "ZN=F", "ZF=F", "ZT=F",
    # Crypto
    "BTC-USD", "ETH-USD", "SOL-USD",
    # FX pairs (currency)
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "USDCHF=X",
    "NZDUSD=X", "USDCNY=X", "USDINR=X", "USDMXN=X",
    "DX=F",
    # Foreign equity indexes
    "^GSPTSE", "^MERV", "^BVSP", "^MXX", "^FTSE", "^GDAXI", "^FCHI",
    "^STOXX50E", "^N225", "^HSI", "^AXJO", "^BSESN", "^KS11", "^TWII",
    # Dollar / treasury / aggregate
    "UUP",
    "TLT", "IEF", "SHY", "BND", "AGG", "GOVT", "TIP",
]


def load_tickers():
    if not TICKERS_FILE.exists():
        log(f"⚠ {TICKERS_FILE} missing — create it with one ticker per line")
        return list(REQUIRED_REFERENCE_TICKERS)
    tickers = []
    for line in TICKERS_FILE.read_text().splitlines():
        t = line.strip().split("#")[0].strip().upper()
        if t:
            tickers.append(t)
    # Merge in required reference tickers (deduped, preserves order)
    tickers = tickers + REQUIRED_REFERENCE_TICKERS
    seen = set()
    out = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def fetch_one(ticker):
    """Return a list of {date, close, volume} dicts (ascending) or None on failure."""
    try:
        t = yf.Ticker(ticker)
        df = t.history(period="5y", interval="1d", auto_adjust=False)
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
            vol = int(row["Volume"]) if not (row["Volume"] is None) else 0
        except (KeyError, ValueError, TypeError):
            vol = 0
        bars.append({"date": date_iso, "close": round(close, 4), "volume": vol})
    bars.sort(key=lambda b: b["date"])
    return bars if bars else None


def main():
    tickers = load_tickers()
    if not tickers:
        log("No tickers. Exiting.")
        return 1

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Fetching 5y history for {len(tickers)} tickers → {HISTORY_DIR}")

    successful = []
    skipped = []
    for i, tic in enumerate(tickers, 1):
        bars = fetch_one(tic)
        if not bars:
            skipped.append(tic)
            continue
        # Per-ticker file. Compact JSON — these are the largest commits in the repo.
        out_path = HISTORY_DIR / f"{tic}.json"
        out_path.write_text(json.dumps(bars, separators=(",", ":")))
        successful.append(tic)
        if i % 25 == 0:
            log(f"  … {i}/{len(tickers)} processed · {len(successful)} OK · {len(skipped)} skipped")
        time.sleep(0.05)

    # Manifest = the list of tickers that have a history file
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(successful),
        "tickers": sorted(successful),
    }
    MANIFEST.write_text(json.dumps(manifest, separators=(",", ":")))

    log(f"✓ Wrote {len(successful)} per-ticker history files")
    log(f"✓ Updated manifest with {len(successful)} tickers")
    if skipped:
        log(f"  Skipped {len(skipped)}: {', '.join(skipped[:10])}{'…' if len(skipped) > 10 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
