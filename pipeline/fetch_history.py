#!/usr/bin/env python3
"""
fetch_history.py — 20-year daily price history fetcher (single-pass or batched).

FIXES IN THIS VERSION (vs the batched-race version)
---------------------------------------------------
1. ORPHAN CLEANUP. After fetching, it deletes data/history/<T>.json files whose
   ticker is no longer in tickers.txt (and isn't a required reference ticker).
   This is what removes the equities you pulled out of TRAPP2-1's tickers.txt
   but whose history files lingered and kept feeding the app stale/wrong data.
   Cleanup runs ONLY on a full pass (--of 1) so a single stripe can't delete
   another stripe's files. Disable with --no-prune.

2. NEVER TRUNCATES. Writing a ticker always MERGES the freshly-fetched bars with
   whatever is already on disk (union by date), so a short top-up run can never
   shrink a 20-year file. The 5-day nightly tail and the 20-year backfill write
   the same files safely. (This is why the nightly was appearing to overwrite
   20y history — a non-merging writer truncated it. This version can't.)

3. RELIABLE FULL PASS. Run with --of 1 (the default) to do the WHOLE universe in
   ONE job — no parallel push race that drops files. Batching is still available
   (--batch/--of) but the recommended workflow runs a single sequential job so
   every ticker lands in one commit.

USAGE
    python fetch_history.py                       # ALL tickers, one pass (recommended)
    python fetch_history.py --years 20            # explicit 20y
    python fetch_history.py --max-age-days 1      # incremental top-up (skip fresh)
    python fetch_history.py --batch 0 --of 4      # one stripe (legacy batched mode)
    python fetch_history.py --no-prune            # don't delete orphan files
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


# Reference tickers the signal engine REQUIRES — auto-merged with tickers.txt and
# NEVER treated as orphans during cleanup.
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


def fetch_one(ticker, years, retries=2):
    """Return [{date, close, volume}] ascending, or None. Ends YESTERDAY (UTC)."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            today = datetime.now(timezone.utc).date()
            end = today                  # 'end' exclusive → last bar = yesterday
            start = end - timedelta(days=int(years * 365.25) + 5)
            t = yf.Ticker(ticker)
            df = t.history(start=start.isoformat(), end=end.isoformat(),
                           interval="1d", auto_adjust=False)
            if df is None or df.empty:
                # brief backoff then retry — empty is often a transient throttle
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
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
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            log(f"  ✗ {ticker}: {e}")
            return None
    if last_err:
        log(f"  ✗ {ticker}: {last_err}")
    return None


def write_merged(ticker, bars):
    """Union freshly-fetched bars with what's on disk; never drop existing bars."""
    out_path = HISTORY_DIR / f"{ticker}.json"
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
    for b in bars:                       # fresh settled history wins per date
        by_date[b["date"]] = b
    merged = [by_date[d] for d in sorted(by_date.keys())]
    out_path.write_text(json.dumps(merged, separators=(",", ":")))
    return merged


def prune_orphans(valid_tickers):
    """Delete history files whose ticker is no longer wanted. Returns removed list."""
    if not HISTORY_DIR.exists():
        return []
    valid = set(t.upper() for t in valid_tickers)
    removed = []
    for p in HISTORY_DIR.glob("*.json"):
        tic = p.stem.upper()
        if tic not in valid:
            try:
                p.unlink()
                removed.append(tic)
            except Exception as e:
                log(f"  ! could not remove orphan {p.name}: {e}")
    return removed


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
    ap.add_argument("--of", type=int, default=1, help="Total number of batches (1 = full pass)")
    ap.add_argument("--years", type=int, default=DEFAULT_YEARS, help="Years of history to fetch")
    ap.add_argument("--max-age-days", type=int, default=4,
                    help="Skip a ticker whose stored history is at most this many days old")
    ap.add_argument("--force", action="store_true", help="Refetch even fresh tickers")
    ap.add_argument("--no-prune", action="store_true", help="Do NOT delete orphan history files")
    args = ap.parse_args()

    if args.of < 1:
        args.of = 1
    if not (0 <= args.batch < args.of):
        log(f"⚠ --batch {args.batch} out of range for --of {args.of}; using 0")
        args.batch = 0

    all_tickers = load_tickers()
    tickers = [t for i, t in enumerate(all_tickers) if i % args.of == args.batch]

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    full_pass = (args.of == 1)
    log(f"{'FULL PASS' if full_pass else f'Batch {args.batch+1}/{args.of}'}: "
        f"{len(tickers)} of {len(all_tickers)} tickers · {args.years}y → {HISTORY_DIR}")

    spans = {}
    successful, skipped_fresh, failed = [], [], []

    for i, tic in enumerate(tickers, 1):
        if not args.force and is_fresh(tic, args.max_age_days):
            skipped_fresh.append(tic)
            last = existing_last_date(tic)
            if last:
                spans[tic] = {"last": last}
            continue
        bars = fetch_one(tic, args.years)
        if not bars:
            failed.append(tic)
            continue
        merged = write_merged(tic, bars)
        spans[tic] = {"first": merged[0]["date"], "last": merged[-1]["date"], "rows": len(merged)}
        successful.append(tic)
        if i % 20 == 0:
            log(f"  … {i}/{len(tickers)} · {len(successful)} fetched · "
                f"{len(skipped_fresh)} fresh-skip · {len(failed)} failed")
        time.sleep(0.08)

    # ----- ORPHAN CLEANUP (full pass only) -----
    removed = []
    if full_pass and not args.no_prune:
        removed = prune_orphans(all_tickers)
        if removed:
            log(f"Pruned {len(removed)} orphan history file(s): "
                f"{', '.join(removed[:20])}{' …' if len(removed) > 20 else ''}")
        else:
            log("No orphan history files to prune.")
    elif not full_pass:
        log("(skipping orphan cleanup — only runs on a full pass, --of 1)")

    # ----- Per-batch manifest fragment (kept for legacy batched mode) -----
    if not full_pass:
        frag = HISTORY_DIR.parent / f"history_manifest.b{args.batch}.json"
        frag.write_text(json.dumps({
            "batch": args.batch, "of": args.of,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "years": args.years, "by_ticker": spans,
        }, separators=(",", ":")))

    # ----- Rebuild the manifest -----
    # On a full pass the manifest is authoritative = exactly the spans we just
    # built (so pruned tickers drop out of the manifest too). In batched mode we
    # merge into the existing manifest as before.
    if full_pass:
        by_ticker = dict(spans)
    else:
        manifest = load_manifest()
        by_ticker = manifest.get("by_ticker", {})
        if not by_ticker and isinstance(manifest.get("tickers"), list):
            for t in manifest["tickers"]:
                by_ticker.setdefault(t, {})
        # drop any pruned tickers
        for t in removed:
            by_ticker.pop(t.upper(), None)
        for t, span in spans.items():
            prev = by_ticker.get(t, {})
            prev.update(span)
            by_ticker[t] = prev

    all_have_history = sorted(by_ticker.keys())
    new_manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(all_have_history),
        "years": args.years,
        "tickers": all_have_history,
        "by_ticker": by_ticker,
    }
    MANIFEST.write_text(json.dumps(new_manifest, separators=(",", ":")))

    log(f"Done. {len(successful)} fetched · {len(skipped_fresh)} fresh-skip · "
        f"{len(failed)} failed · {len(removed)} pruned · manifest has {len(all_have_history)} tickers")
    if failed:
        log(f"  Failed (will retry next run): {', '.join(failed[:30])}{' …' if len(failed) > 30 else ''}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
