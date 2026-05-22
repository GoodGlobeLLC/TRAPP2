"""
TRAPP2 ETF holdings fetcher — downloads daily holdings from issuer-direct
CSV/JSON endpoints (iShares, SSGA, Vanguard, Invesco) and commits them as
versioned JSON files to data/etf_holdings/<TICKER>.json.

The app then reads these files via GitHub raw URL — no CORS issues, no API
keys needed, always-current data.

To add a new ETF: add an entry to ETF_SOURCES below. Most issuers publish
holdings via deep URLs that are stable across days; the URL itself contains
the fund identifier so the file name is predictable.

Run nightly via .github/workflows/nightly.yml (already wired) or manually:
    python pipeline/fetch_etf_holdings.py
"""

import csv
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------
# Each entry maps a ticker → fetch config:
#   - url:    the CSV/JSON endpoint
#   - parser: which parser function handles this issuer's format
#   - skip_rows (CSV only): rows to skip at the start of the file (preambles)
#
# iShares CSVs have ~10 rows of fund header info before the actual holdings
# table starts. SSGA returns Excel which we don't fetch — they have a JSON API
# instead. Vanguard returns proper JSON.

ETF_SOURCES = {
    # ============== iShares (BlackRock) ==============
    # Pattern: https://www.ishares.com/us/products/{id}/{slug}/1467271812596.ajax?fileType=csv&fileName={SYMBOL}_holdings&dataType=fund
    "IVV":  {"url": "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund",                    "parser": "ishares_csv"},
    "IJH":  {"url": "https://www.ishares.com/us/products/239763/ishares-core-sp-midcap-etf/1467271812596.ajax?fileType=csv&fileName=IJH_holdings&dataType=fund",                "parser": "ishares_csv"},
    "IJR":  {"url": "https://www.ishares.com/us/products/239774/ishares-core-sp-smallcap-etf/1467271812596.ajax?fileType=csv&fileName=IJR_holdings&dataType=fund",              "parser": "ishares_csv"},
    "IWM":  {"url": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund",                  "parser": "ishares_csv"},
    "IWD":  {"url": "https://www.ishares.com/us/products/239708/ishares-russell-1000-value-etf/1467271812596.ajax?fileType=csv&fileName=IWD_holdings&dataType=fund",            "parser": "ishares_csv"},
    "IWF":  {"url": "https://www.ishares.com/us/products/239706/ishares-russell-1000-growth-etf/1467271812596.ajax?fileType=csv&fileName=IWF_holdings&dataType=fund",           "parser": "ishares_csv"},
    "EFA":  {"url": "https://www.ishares.com/us/products/239623/ishares-msci-eafe-etf/1467271812596.ajax?fileType=csv&fileName=EFA_holdings&dataType=fund",                     "parser": "ishares_csv"},
    "EEM":  {"url": "https://www.ishares.com/us/products/239637/ishares-msci-emerging-markets-etf/1467271812596.ajax?fileType=csv&fileName=EEM_holdings&dataType=fund",         "parser": "ishares_csv"},
    "AGG":  {"url": "https://www.ishares.com/us/products/239458/ishares-core-total-us-bond-market-etf/1467271812596.ajax?fileType=csv&fileName=AGG_holdings&dataType=fund",     "parser": "ishares_csv"},
    "TLT":  {"url": "https://www.ishares.com/us/products/239454/ishares-20-year-treasury-bond-etf/1467271812596.ajax?fileType=csv&fileName=TLT_holdings&dataType=fund",         "parser": "ishares_csv"},
    "IEF":  {"url": "https://www.ishares.com/us/products/239456/ishares-710-year-treasury-bond-etf/1467271812596.ajax?fileType=csv&fileName=IEF_holdings&dataType=fund",        "parser": "ishares_csv"},
    "SHY":  {"url": "https://www.ishares.com/us/products/239452/ishares-13-year-treasury-bond-etf/1467271812596.ajax?fileType=csv&fileName=SHY_holdings&dataType=fund",         "parser": "ishares_csv"},
    "LQD":  {"url": "https://www.ishares.com/us/products/239566/ishares-iboxx-investment-grade-corporate-bond-etf/1467271812596.ajax?fileType=csv&fileName=LQD_holdings&dataType=fund",  "parser": "ishares_csv"},
    "HYG":  {"url": "https://www.ishares.com/us/products/239565/ishares-iboxx-high-yield-corporate-bond-etf/1467271812596.ajax?fileType=csv&fileName=HYG_holdings&dataType=fund",        "parser": "ishares_csv"},
    "TIP":  {"url": "https://www.ishares.com/us/products/239467/ishares-tips-bond-etf/1467271812596.ajax?fileType=csv&fileName=TIP_holdings&dataType=fund",                     "parser": "ishares_csv"},
    "IYR":  {"url": "https://www.ishares.com/us/products/239520/ishares-us-real-estate-etf/1467271812596.ajax?fileType=csv&fileName=IYR_holdings&dataType=fund",                "parser": "ishares_csv"},
    "IYT":  {"url": "https://www.ishares.com/us/products/239526/ishares-transportation-average-etf/1467271812596.ajax?fileType=csv&fileName=IYT_holdings&dataType=fund",        "parser": "ishares_csv"},

    # ============== SSGA / State Street SPDR ==============
    # Pattern: https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{symbol}.xlsx
    # The xlsx parser uses openpyxl which we'd need to add. For now, skip — FMP + curated covers SPY/DIA/sectors.
    # If you want to add later, the xlsx files are stable and well-formatted.

    # ============== Vanguard ==============
    # Vanguard has a JSON endpoint for some ETFs. The shape varies, so for now we skip
    # and rely on the iShares + FMP combo for diversification. VOO/VTI/etc. covered via SPY proxy.
}

# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
HOLDINGS_DIR = ROOT / "data" / "etf_holdings"
HOLDINGS_DIR.mkdir(parents=True, exist_ok=True)
MANIFEST_FILE = HOLDINGS_DIR / "_manifest.json"


def log(msg):
    print(f"[etf-holdings] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
def parse_ishares_csv(text: str) -> list[dict]:
    """
    iShares CSV format:
        Fund Holdings as of, <date>
        Inception Date, <date>
        Shares Outstanding, <number>
        Stock, <split into header + holdings>
        <blank>
        Ticker, Name, Sector, Asset Class, Market Value, Weight (%), ...
        AAPL, Apple Inc, Information Technology, Equity, ...

    The actual header row is detected by looking for "Ticker" as the first cell.
    """
    holdings = []
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    header = None
    header_idx = None
    for i, row in enumerate(rows):
        if row and row[0].strip().lower() in ("ticker", "issuer ticker", "issuer"):
            header = [c.strip().lower() for c in row]
            header_idx = i
            break
    if header is None or header_idx is None:
        return holdings

    # Map column names → indices
    col = {name: idx for idx, name in enumerate(header)}
    def get(row, *names):
        for n in names:
            if n in col and col[n] < len(row):
                return row[col[n]].strip()
        return ""

    for row in rows[header_idx + 1 :]:
        if not row or not row[0].strip():
            continue
        ticker = get(row, "ticker", "issuer ticker")
        name = get(row, "name", "issuer name")
        weight_raw = get(row, "weight (%)", "weight", "% of market value")
        try:
            weight = float(weight_raw.replace(",", "").replace("%", "")) if weight_raw else None
        except ValueError:
            weight = None
        if weight is None or weight <= 0:
            continue
        asset_class = get(row, "asset class") or "Equity"
        sector = get(row, "sector")
        try:
            shares = float(get(row, "shares", "shares held").replace(",", "")) if get(row, "shares", "shares held") else None
        except ValueError:
            shares = None
        try:
            mv_raw = get(row, "market value", "market value (notional)")
            mv = float(mv_raw.replace(",", "").replace("$", "")) if mv_raw else None
        except ValueError:
            mv = None

        holdings.append({
            "asset": ticker,
            "name": name,
            "weight": round(weight, 4),
            "sharesNumber": shares,
            "marketValue": mv,
            "assetClass": asset_class,
            "subsector": sector if sector else None,
        })

    # Sort by weight desc (already mostly sorted but ensure)
    holdings.sort(key=lambda h: h["weight"] or 0, reverse=True)
    return holdings


PARSERS = {
    "ishares_csv": parse_ishares_csv,
}


# ---------------------------------------------------------------------------
# Fetch loop
# ---------------------------------------------------------------------------
def fetch_etf(ticker: str, config: dict) -> tuple[bool, str]:
    """Returns (ok, message).

    Two strategies:
      1. Issuer-direct CSV/JSON (most accurate, but iShares often 403s)
      2. Yahoo Finance ETF holdings (fallback — top 10 only but always works)

    iShares specifically requires Referer + browser-pattern User-Agent or it
    blocks the response. Strategy 2 catches that case + gives us partial data
    rather than nothing.
    """
    url = config["url"]
    parser_name = config["parser"]
    parser = PARSERS.get(parser_name)
    if not parser:
        return False, f"no parser {parser_name}"

    # Browser-pattern headers. iShares and SSGA specifically check Referer.
    # The User-Agent must look like a real desktop browser or they 403.
    issuer_referer = "https://www.ishares.com/us/products/" if "ishares" in url else \
                     "https://www.ssga.com/" if "ssga" in url else \
                     "https://investor.vanguard.com/" if "vanguard" in url else \
                     "https://www.invesco.com/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/csv,application/csv,text/plain,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": issuer_referer,
        "Sec-Ch-Ua": '"Chromium";v="124", "Not.A/Brand";v="99", "Google Chrome";v="124"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }
    # STRATEGY 1: issuer-direct
    try:
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        if r.status_code == 200 and len(r.text) >= 200:
            holdings = parser(r.text)
            if holdings:
                output = {
                    "ticker": ticker,
                    "source": "issuer-direct",
                    "fetchedAt": datetime.utcnow().isoformat() + "Z",
                    "holdingsCount": len(holdings),
                    "holdings": holdings,
                }
                out_path = HOLDINGS_DIR / f"{ticker}.json"
                out_path.write_text(json.dumps(output, indent=2))
                return True, f"{len(holdings)} holdings (issuer-direct)"
        issuer_fail_msg = f"HTTP {r.status_code}" if r.status_code != 200 else f"empty/{len(r.text)}B"
    except Exception as e:
        issuer_fail_msg = f"{type(e).__name__}: {e}"

    # STRATEGY 2: Yahoo Finance fallback (top holdings only — partial but useful)
    try:
        y_url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=topHoldings"
        y_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }
        r2 = requests.get(y_url, headers=y_headers, timeout=20)
        if r2.status_code == 200:
            j = r2.json()
            top = j.get("quoteSummary", {}).get("result", [{}])[0].get("topHoldings", {})
            yh_list = top.get("holdings") or []
            if yh_list:
                holdings = []
                for h in yh_list:
                    sym = h.get("symbol") or ""
                    name = h.get("holdingName") or sym
                    pct = h.get("holdingPercent", {}).get("raw") or 0.0
                    holdings.append({
                        "ticker": sym,
                        "name": name,
                        "weight": float(pct) * 100.0,  # store as percent
                        "shares": None,
                        "marketValue": None,
                    })
                if holdings:
                    output = {
                        "ticker": ticker,
                        "source": "yahoo-finance-fallback",
                        "note": "Top holdings only — issuer-direct CSV failed",
                        "fetchedAt": datetime.utcnow().isoformat() + "Z",
                        "holdingsCount": len(holdings),
                        "holdings": holdings,
                    }
                    out_path = HOLDINGS_DIR / f"{ticker}.json"
                    out_path.write_text(json.dumps(output, indent=2))
                    return True, f"{len(holdings)} holdings (Yahoo fallback after issuer: {issuer_fail_msg})"
    except Exception as e:
        return False, f"issuer:{issuer_fail_msg}, yahoo:{type(e).__name__}: {e}"

    return False, f"both strategies failed (issuer: {issuer_fail_msg})"


def main() -> int:
    log(f"Fetching {len(ETF_SOURCES)} ETFs into {HOLDINGS_DIR}")
    manifest = {
        "generatedAt": datetime.utcnow().isoformat() + "Z",
        "etfs": {},
    }
    success = 0
    for ticker, config in ETF_SOURCES.items():
        ok, msg = fetch_etf(ticker, config)
        if ok:
            log(f"✓ {ticker:6s} — {msg}")
            success += 1
        else:
            log(f"✗ {ticker:6s} — {msg}")
        manifest["etfs"][ticker] = {"ok": ok, "msg": msg, "fetchedAt": datetime.utcnow().isoformat() + "Z"}
        # Be courteous to the issuer's servers
        time.sleep(0.5)

    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))
    log(f"Done: {success}/{len(ETF_SOURCES)} successful. Manifest: {MANIFEST_FILE}")
    # Exit 0 even on partial success — we don't want one bad ETF to fail the whole workflow
    return 0


if __name__ == "__main__":
    sys.exit(main())
