#!/usr/bin/env python3
"""
cleanup_history_orphans.py — delete history files no longer in tickers.txt.

Removes data/history/<T>.json for any ticker that is NOT in data/tickers.txt and
NOT a required reference ticker, then rewrites data/history_manifest.json to drop
the removed tickers. This purges equities you pulled out of a repo's tickers.txt
whose history files lingered and kept feeding the app stale/wrong data.

Set DRY_RUN=true (env) to list orphans without deleting.

Stdlib only.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TICKERS_FILE = DATA / "tickers.txt"
HISTORY_DIR = DATA / "history"
MANIFEST = DATA / "history_manifest.json"

# Must match fetch_history.py's reference list so we never delete engine refs.
REQUIRED_REFERENCE_TICKERS = [
    "SPY", "QQQ", "DIA", "IWM", "^VIX", "^VIX9D", "^MOVE",
    "HYG", "LQD", "JNK", "EMB",
    "XLK", "XLF", "XLV", "XLE", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC",
    "ES=F", "NQ=F", "YM=F", "RTY=F",
    "CL=F", "BZ=F", "NG=F", "RB=F", "HO=F",
    "GC=F", "SI=F", "HG=F", "PL=F", "PA=F",
    "ZC=F", "ZS=F", "ZW=F", "KC=F", "SB=F", "CC=F", "CT=F",
    "ZB=F", "ZN=F", "ZF=F", "ZT=F",
    "BTC-USD", "ETH-USD", "SOL-USD",
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "USDCHF=X",
    "NZDUSD=X", "USDCNY=X", "USDINR=X", "USDMXN=X", "DX=F",
    "^GSPTSE", "^MERV", "^BVSP", "^MXX", "^FTSE", "^GDAXI", "^FCHI",
    "^STOXX50E", "^N225", "^HSI", "^AXJO", "^BSESN", "^KS11", "^TWII",
    "UUP", "TLT", "IEF", "SHY", "BND", "AGG", "GOVT", "TIP",
]


def log(*a):
    print("[cleanup]", *a, flush=True)


def load_valid():
    valid = set(t.upper() for t in REQUIRED_REFERENCE_TICKERS)
    if TICKERS_FILE.exists():
        for line in TICKERS_FILE.read_text().splitlines():
            t = line.strip().split("#")[0].strip().upper()
            if t:
                valid.add(t)
    return valid


def main():
    dry = os.environ.get("DRY_RUN", "true").lower() != "false"
    if not HISTORY_DIR.exists():
        log("no data/history/ directory — nothing to do")
        return
    valid = load_valid()
    orphans = []
    for p in sorted(HISTORY_DIR.glob("*.json")):
        if p.stem.upper() not in valid:
            orphans.append(p)

    log(f"{len(list(HISTORY_DIR.glob('*.json')))} history files · "
        f"{len(valid)} valid tickers · {len(orphans)} orphans")
    if not orphans:
        log("No orphans found.")
        return

    names = [p.stem for p in orphans]
    log("Orphans: " + ", ".join(names[:50]) + (" …" if len(names) > 50 else ""))

    if dry:
        log(f"DRY RUN — would delete {len(orphans)} file(s). Set DRY_RUN=false to apply.")
        return

    removed = []
    for p in orphans:
        try:
            p.unlink()
            removed.append(p.stem.upper())
        except Exception as e:
            log(f"  ! could not remove {p.name}: {e}")
    log(f"Deleted {len(removed)} orphan file(s).")

    # Rewrite the manifest to drop removed tickers.
    if MANIFEST.exists():
        try:
            m = json.loads(MANIFEST.read_text())
        except Exception:
            m = {}
        bt = m.get("by_ticker", {}) if isinstance(m, dict) else {}
        for t in removed:
            bt.pop(t, None)
            bt.pop(t.upper(), None)
        kept = sorted(bt.keys())
        new_m = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(kept),
            "tickers": kept,
            "by_ticker": bt,
        }
        MANIFEST.write_text(json.dumps(new_m, separators=(",", ":")))
        log(f"Manifest rewritten: {len(kept)} tickers remain.")


if __name__ == "__main__":
    main()
