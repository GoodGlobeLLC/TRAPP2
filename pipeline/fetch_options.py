#!/usr/bin/env python3
"""
Fetch live options chains for the main tickers and compute Greeks.

Source: yfinance (free) for the chain (strike, bid/ask/last, IV, volume, OI).
Greeks: Black-Scholes, computed here in stdlib math (no extra deps) from the
        contract's IV + the spot + a risk-free rate.

Writes one file per ticker:  data/options/<TICKER>.json
    {
      ticker, spot, asOf, riskFreeRate,
      expiries: [
        { expiry: "2026-06-26", dte: 6, isFriday: true,
          calls: [ {strike, last, bid, ask, mid, volume, openInterest, iv,
                    inTheMoney, delta, gamma, theta, vega, rho, intrinsic,
                    extrinsic, breakeven}, ... ],
          puts:  [ ... ] }
      ]
    }

Design notes:
  • Only the nearest N expiries are pulled (most liquidity + relevance), and only
    strikes within ±STRIKE_PCT of spot (near the money) — keeps files small and
    the run inside the Actions time budget.
  • Both calls AND puts.
  • Expired contracts are never written (dte < 0 skipped), so the app naturally
    drops them. The bot keeps whatever it traded in its own data.
  • Stdlib + yfinance only.

Env (optional):
    OPTIONS_TICKERS   comma list to override the universe (else reads
                      data/options_universe.txt, else data/tickers.txt)
    RISK_FREE_RATE    annualized decimal (else read from data/macro/DGS3MO, else 0.04)
    MAX_EXPIRIES      nearest expiries per ticker (default 4)
    STRIKE_PCT        ± strike window around spot (default 0.25 = ±25%)
    TIME_BUDGET_SEC   stop starting new tickers after this many seconds (default 2400)
"""
import json
import math
import os
import sys
import time
import datetime

try:
    import yfinance as yf
except ImportError:
    print("yfinance required: pip install yfinance", file=sys.stderr)
    sys.exit(1)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OPTIONS_DIR = os.path.join(DATA_DIR, "options")

# Expiry ladder: instead of just "nearest N", we pick a SPREAD of expiries across
# the term structure so the app/bot can trade short-dated, monthly, AND LEAPS.
#   • NEAR weeklies:   the next few expiries (≤ NEAR_MAX_DTE days)
#   • MONTHLIES:       standard monthly expiries out to MONTHLY_MAX_DTE days
#   • LEAPS:           the longest-dated expiries (≥ LEAP_MIN_DTE days), incl. the
#                      furthest available (often 1-2 years out)
# MAX_EXPIRIES caps the TOTAL pulled per ticker so the job stays within budget.
MAX_EXPIRIES     = int(os.environ.get("MAX_EXPIRIES", "10"))      # total cap per ticker
NEAR_COUNT       = int(os.environ.get("NEAR_COUNT", "3"))         # nearest weeklies to always include
NEAR_MAX_DTE     = int(os.environ.get("NEAR_MAX_DTE", "45"))      # "near" window
MONTHLY_MAX_DTE  = int(os.environ.get("MONTHLY_MAX_DTE", "120"))  # monthlies out to ~4 months
LEAP_MIN_DTE     = int(os.environ.get("LEAP_MIN_DTE", "180"))     # LEAPS = 6 months+
LEAP_COUNT       = int(os.environ.get("LEAP_COUNT", "3"))         # how many long-dated expiries to include
STRIKE_PCT = float(os.environ.get("STRIKE_PCT", "0.25"))
TIME_BUDGET_SEC = int(os.environ.get("TIME_BUDGET_SEC", "2400"))
MIN_OI = 1  # skip totally illiquid contracts (open interest 0 AND volume 0)


# ---------- Black-Scholes Greeks (stdlib) ----------
def _norm_cdf(x):
    # standard normal CDF via erf (stdlib)
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def greeks(opt_type, S, K, T, r, sigma):
    """Return (delta, gamma, theta_per_day, vega_per_1pct, rho_per_1pct).
    opt_type: 'call' | 'put'. T in years, sigma = implied vol (decimal)."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return (None, None, None, None, None)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    pdf_d1 = _norm_pdf(d1)
    gamma = pdf_d1 / (S * sigma * sqrtT)
    vega = S * pdf_d1 * sqrtT / 100.0  # per 1% vol move
    if opt_type == "call":
        delta = _norm_cdf(d1)
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrtT) - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365.0
        rho = K * T * math.exp(-r * T) * _norm_cdf(d2) / 100.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrtT) + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365.0
        rho = -K * T * math.exp(-r * T) * _norm_cdf(-d2) / 100.0
    rnd = lambda v: round(v, 4) if v is not None and math.isfinite(v) else None
    return (rnd(delta), rnd(gamma), rnd(theta), rnd(vega), rnd(rho))


# ---------- helpers ----------
def _read_universe():
    env = os.environ.get("OPTIONS_TICKERS", "").strip()
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    for fname in ("options_universe.txt", "tickers.txt"):
        p = os.path.join(DATA_DIR, fname)
        if os.path.exists(p):
            with open(p) as f:
                return [ln.strip().upper() for ln in f if ln.strip() and not ln.startswith("#")]
    return []


def _risk_free_rate():
    env = os.environ.get("RISK_FREE_RATE", "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    # Try the 3-month Treasury from the macro pipeline (DGS3MO is a percent).
    p = os.path.join(DATA_DIR, "macro", "DGS3MO.json")
    try:
        with open(p) as f:
            d = json.load(f)
        obs = d.get("observations") or d.get("data") or []
        if obs:
            last = obs[-1]
            val = last.get("value") if isinstance(last, dict) else last[1]
            return float(val) / 100.0
    except Exception:
        pass
    return 0.04


def _is_friday(date_str):
    try:
        return datetime.date.fromisoformat(date_str).weekday() == 4
    except Exception:
        return False


def _safe(v, default=None):
    try:
        if v is None:
            return default
        f = float(v)
        return f if math.isfinite(f) else default
    except Exception:
        return default


def _third_friday(d):
    """True if date d (datetime.date) is the third Friday of its month — the
    standard 'monthly' option expiry."""
    return d.weekday() == 4 and 15 <= d.day <= 21


def _select_expiries(all_exps, today):
    """From every available expiry, pick a spread across the term structure:
    nearest weeklies + monthlies + LEAPS. Returns an ordered, de-duplicated list
    of expiry strings, capped at MAX_EXPIRIES. Falls back gracefully when a name
    has few expiries (e.g. only weeklies)."""
    dated = []
    for e in all_exps:
        try:
            d = datetime.date.fromisoformat(e)
        except Exception:
            continue
        dte = (d - today).days
        if dte < 0:
            continue
        dated.append((e, d, dte))
    if not dated:
        return []
    dated.sort(key=lambda x: x[2])

    picked = []

    # (a) Nearest weeklies — always include the front few for short-dated trades.
    for e, d, dte in dated:
        if dte <= NEAR_MAX_DTE and len(picked) < NEAR_COUNT:
            picked.append(e)

    # (b) Monthlies (third-Friday) out to MONTHLY_MAX_DTE — the liquid sweet spot.
    for e, d, dte in dated:
        if e in picked:
            continue
        if _third_friday(d) and NEAR_MAX_DTE < dte <= MONTHLY_MAX_DTE:
            picked.append(e)

    # (c) Bridge: if no monthly landed in the 45-120d window (some names lack
    #     standard monthlies), grab the nearest expiry in that band anyway.
    if not any((NEAR_MAX_DTE < (datetime.date.fromisoformat(e) - today).days <= MONTHLY_MAX_DTE) for e in picked):
        for e, d, dte in dated:
            if NEAR_MAX_DTE < dte <= MONTHLY_MAX_DTE and e not in picked:
                picked.append(e); break

    # (d) LEAPS — the longest-dated expiries (6 months+). Prefer third-Friday
    #     monthlies (true LEAPS), then fall back to the furthest available.
    leaps = [(e, d, dte) for (e, d, dte) in dated if dte >= LEAP_MIN_DTE]
    leap_monthlies = [t for t in leaps if _third_friday(t[1])]
    leap_pool = leap_monthlies if leap_monthlies else leaps
    # Spread them out: take evenly across the long end (e.g. ~6mo, ~1yr, ~2yr).
    if leap_pool:
        idxs = set()
        n = len(leap_pool)
        for k in range(min(LEAP_COUNT, n)):
            idxs.add(round(k * (n - 1) / max(LEAP_COUNT - 1, 1)))
        for i in sorted(idxs):
            e = leap_pool[i][0]
            if e not in picked:
                picked.append(e)

    # Always include the single furthest-dated expiry (true long LEAP) if room.
    furthest = dated[-1][0]
    if furthest not in picked:
        picked.append(furthest)

    # De-dupe, keep chronological order, cap at MAX_EXPIRIES.
    seen = set()
    ordered = []
    for e, d, dte in dated:
        if e in picked and e not in seen:
            seen.add(e); ordered.append(e)
    return ordered[:MAX_EXPIRIES]


def _row(opt_type, rec, S, T, r):
    strike = _safe(rec.get("strike"))
    if strike is None or strike <= 0:
        return None
    last = _safe(rec.get("lastPrice"), 0.0)
    bid = _safe(rec.get("bid"), 0.0)
    ask = _safe(rec.get("ask"), 0.0)
    mid = round((bid + ask) / 2.0, 4) if (bid or ask) else last
    iv = _safe(rec.get("impliedVolatility"), 0.0)
    vol = int(_safe(rec.get("volume"), 0) or 0)
    oi = int(_safe(rec.get("openInterest"), 0) or 0)
    if vol == 0 and oi < MIN_OI:
        return None  # totally illiquid — skip
    itm = bool(rec.get("inTheMoney", False))
    d, g, th, ve, rho = greeks(opt_type, S, strike, T, r, iv) if iv > 0 else (None, None, None, None, None)
    if opt_type == "call":
        intrinsic = max(0.0, S - strike)
        breakeven = round(strike + (mid or last), 2)
    else:
        intrinsic = max(0.0, strike - S)
        breakeven = round(strike - (mid or last), 2)
    price = mid if mid else last
    extrinsic = round(max(0.0, (price or 0) - intrinsic), 4)
    return {
        "strike": round(strike, 2),
        "last": round(last, 4),
        "bid": round(bid, 4),
        "ask": round(ask, 4),
        "mid": mid,
        "volume": vol,
        "openInterest": oi,
        "iv": round(iv, 4),
        "inTheMoney": itm,
        "delta": d, "gamma": g, "theta": th, "vega": ve, "rho": rho,
        "intrinsic": round(intrinsic, 2),
        "extrinsic": extrinsic,
        "breakeven": breakeven,
    }


def fetch_ticker(ticker, r):
    tk = yf.Ticker(ticker)
    # Spot price.
    spot = None
    try:
        fi = getattr(tk, "fast_info", None)
        if fi:
            spot = _safe(fi.get("last_price") if hasattr(fi, "get") else getattr(fi, "last_price", None))
    except Exception:
        pass
    if spot is None:
        try:
            h = tk.history(period="1d")
            if not h.empty:
                spot = float(h["Close"].iloc[-1])
        except Exception:
            pass
    if not spot or spot <= 0:
        return None
    try:
        expiries = list(tk.options or [])
    except Exception:
        return None
    if not expiries:
        return None
    today = datetime.date.today()
    chosen = _select_expiries(expiries, today)
    out_expiries = []
    for exp in chosen:
        try:
            dte = (datetime.date.fromisoformat(exp) - today).days
        except Exception:
            continue
        if dte < 0:
            continue  # expired — skip
        T = max(dte, 0) / 365.0
        if T <= 0:
            T = 0.5 / 365.0  # same-day: tiny but nonzero so Greeks don't div0
        try:
            chain = tk.option_chain(exp)
        except Exception:
            continue
        lo, hi = spot * (1 - STRIKE_PCT), spot * (1 + STRIKE_PCT)
        calls, puts = [], []
        for _, rec in chain.calls.iterrows():
            s = _safe(rec.get("strike"))
            if s is None or s < lo or s > hi:
                continue
            row = _row("call", rec, spot, T, r)
            if row:
                calls.append(row)
        for _, rec in chain.puts.iterrows():
            s = _safe(rec.get("strike"))
            if s is None or s < lo or s > hi:
                continue
            row = _row("put", rec, spot, T, r)
            if row:
                puts.append(row)
        if calls or puts:
            calls.sort(key=lambda x: x["strike"])
            puts.sort(key=lambda x: x["strike"])
            _bucket = ("near" if dte <= NEAR_MAX_DTE
                       else "monthly" if dte <= MONTHLY_MAX_DTE
                       else "leap")
            out_expiries.append({
                "expiry": exp, "dte": dte, "isFriday": _is_friday(exp),
                "bucket": _bucket, "isLeap": dte >= LEAP_MIN_DTE,
                "calls": calls, "puts": puts,
            })
    if not out_expiries:
        return None
    return {
        "ticker": ticker,
        "spot": round(spot, 4),
        "asOf": datetime.datetime.utcnow().isoformat() + "Z",
        "riskFreeRate": round(r, 4),
        "expiries": out_expiries,
    }


def main():
    universe = _read_universe()
    if not universe:
        print("No options universe found (set OPTIONS_TICKERS or add data/options_universe.txt / data/tickers.txt).")
        return 0
    r = _risk_free_rate()
    os.makedirs(OPTIONS_DIR, exist_ok=True)
    print(f"Fetching options for {len(universe)} tickers · r={r:.3f} · ≤{MAX_EXPIRIES} expiries (near+monthly+LEAPS) · ±{STRIKE_PCT*100:.0f}% strikes")
    start = time.time()
    written = 0
    skipped = 0
    for i, ticker in enumerate(universe):
        if time.time() - start > TIME_BUDGET_SEC:
            print(f"Time budget hit at {i}/{len(universe)} — stopping (partial run is fine; next run continues).")
            break
        try:
            data = fetch_ticker(ticker, r)
        except Exception as e:
            print(f"  {ticker}: error {e}", file=sys.stderr)
            data = None
        if not data:
            skipped += 1
            # Remove any stale file so the app doesn't show dead options.
            stale = os.path.join(OPTIONS_DIR, f"{ticker}.json")
            if os.path.exists(stale):
                try:
                    os.remove(stale)
                except Exception:
                    pass
            continue
        with open(os.path.join(OPTIONS_DIR, f"{ticker}.json"), "w") as f:
            json.dump(data, f, separators=(",", ":"))
        written += 1
        if written % 25 == 0:
            print(f"  …{written} written ({i+1}/{len(universe)})")
    # Manifest so the frontend knows which tickers have options without 404-probing.
    have = sorted([f[:-5] for f in os.listdir(OPTIONS_DIR) if f.endswith(".json") and f != "manifest.json"])
    with open(os.path.join(OPTIONS_DIR, "manifest.json"), "w") as f:
        json.dump({"asOf": datetime.datetime.utcnow().isoformat() + "Z", "tickers": have, "count": len(have)}, f)
    print(f"Done — {written} written, {skipped} skipped (no/illiquid options), {len(have)} total with chains.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
