#!/usr/bin/env python3
"""
apply_overrides.py — Merge the Editor's data/overrides.json into a repo's
pulled data as the FINAL layer.

The Editor (front end / Vercel) writes data/overrides.json with the user's
authoritative field values. Each override is the final value for a ticker+field
until its expiresAt (null = forever). This script applies them on top of
whatever the repo's own fetchers produced, so overrides always win — but expired
ones are skipped so the pulled data shows through again.

Where this runs: add a step to each repo's data-refresh workflow, AFTER the
fetch steps and BEFORE the commit, e.g.:

    - name: Apply editor overrides
      run: python pipeline/apply_overrides.py

What it edits: by default it patches a master data file (master.json) keyed by
ticker. Adjust MASTER_PATH / load+save to match how your repo stores per-ticker
data. The merge logic (active-override detection + field patch) is the reusable
part; the file plumbing is repo-specific.

Requires: stdlib only.
"""
import json
import os
import sys
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
OVERRIDES_PATH = os.path.join(REPO_ROOT, "data", "overrides.json")
# Adjust to your repo's per-ticker data file. Expected shape: { TICKER: {fields} }
MASTER_PATH = os.path.join(REPO_ROOT, "data", "master.json")


def now_ms():
    return int(datetime.datetime.utcnow().timestamp() * 1000)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def active_overrides(overrides_doc):
    """Yield (ticker, field, value) for every non-expired override."""
    now = now_ms()
    for ticker, fields in (overrides_doc.get("overrides") or {}).items():
        for field, rec in fields.items():
            exp = rec.get("expiresAt")
            if exp is not None and now > exp:
                continue  # expired — let pulled data show through
            yield ticker, field, rec.get("value")


def main():
    ov_doc = load_json(OVERRIDES_PATH, None)
    if not ov_doc:
        print("No overrides.json found — nothing to apply.")
        return

    master = load_json(MASTER_PATH, {})
    applied = 0
    skipped_expired = 0
    now = now_ms()

    # Count expired for reporting
    for _ticker, fields in (ov_doc.get("overrides") or {}).items():
        for _field, rec in fields.items():
            exp = rec.get("expiresAt")
            if exp is not None and now > exp:
                skipped_expired += 1

    for ticker, field, value in active_overrides(ov_doc):
        if ticker not in master:
            master[ticker] = {}
        master[ticker][field] = value
        # Tag so downstream knows this field is user-final, not pulled
        master[ticker].setdefault("_overridden", [])
        if field not in master[ticker]["_overridden"]:
            master[ticker]["_overridden"].append(field)
        applied += 1

    with open(MASTER_PATH, "w") as f:
        json.dump(master, f, indent=2)

    print(f"Applied {applied} active overrides "
          f"({skipped_expired} expired, skipped) → {MASTER_PATH}")


if __name__ == "__main__":
    main()
