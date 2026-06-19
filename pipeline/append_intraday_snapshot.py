#!/usr/bin/env python3
"""
append_intraday_snapshot.py — timestamped intraday price log (append-only).

Runs right AFTER the quote fetcher in the same workflow. It reads the freshly
written data/master.json (which already carries each ticker's price +
fetched_at) and appends one compact snapshot row per ticker to a daily log:

    data/intraday/YYYY-MM-DD.json
      { date, snapshots: [ { t: "<ISO time>", p: { TICKER: price, ... } } ] }

Each workflow run adds ONE snapshot object (the time + a ticker->price map).
Old snapshots in the day's file are kept; old days' files are never touched.
This builds an intraday price tape over the trading day so the frontend can
match a news article's publish time to the NEAREST price snapshot and estimate
the price when the article was written (approximate — within one fetch cycle).

Stdlib only. Safe to run every quote cycle; cheap and tiny.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
MASTER = DATA / "master.json"
INTRADAY_DIR = DATA / "intraday"
# Keep the per-day file lean: cap snapshots/day (a 15-min cadence over ~14 trading
# hours is ~56; 200 is plenty of headroom for finer cadences).
MAX_SNAPSHOTS_PER_DAY = 200


def main():
    if not MASTER.exists():
        print("no master.json yet — skipping intraday snapshot", file=sys.stderr)
        return 0
    try:
        rows = json.loads(MASTER.read_text())
        if isinstance(rows, dict):
            rows = rows.get("rows") or list(rows.values())
    except Exception as e:
        print(f"could not read master.json: {e}", file=sys.stderr)
        return 0

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    day = now.strftime("%Y-%m-%d")

    # Build the ticker -> price map for this instant.
    pmap = {}
    for r in rows:
        t = (r.get("ticker") or "").upper()
        if not t:
            continue
        px = r.get("price") or r.get("fmpprice") or r.get("last price")
        try:
            px = round(float(px), 6) if px not in (None, "") else None
        except Exception:
            px = None
        if px is not None:
            pmap[t] = px
    if not pmap:
        print("no prices in master.json — skipping", file=sys.stderr)
        return 0

    INTRADAY_DIR.mkdir(parents=True, exist_ok=True)
    path = INTRADAY_DIR / f"{day}.json"
    if path.exists():
        try:
            doc = json.loads(path.read_text())
        except Exception:
            doc = {"date": day, "snapshots": []}
    else:
        doc = {"date": day, "snapshots": []}

    snaps = doc.get("snapshots", [])
    # De-dupe: if the last snapshot is the same minute, replace it (avoid dupes
    # when a workflow re-runs); otherwise append.
    if snaps and snaps[-1].get("t", "")[:16] == now_iso[:16]:
        snaps[-1] = {"t": now_iso, "p": pmap}
    else:
        snaps.append({"t": now_iso, "p": pmap})
    # Trim from the front if we somehow exceed the cap (keeps newest).
    if len(snaps) > MAX_SNAPSHOTS_PER_DAY:
        snaps = snaps[-MAX_SNAPSHOTS_PER_DAY:]
    doc["snapshots"] = snaps
    doc["updatedAt"] = now_iso
    doc["snapshotCount"] = len(snaps)

    path.write_text(json.dumps(doc, separators=(",", ":")))
    print(f"OK intraday/{day}.json: snapshot at {now_iso} with {len(pmap)} tickers "
          f"({len(snaps)} snapshots today)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
