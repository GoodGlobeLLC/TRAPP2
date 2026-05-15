# trading# Valuatio — Stock Valuation Engine
#
#
# RUN npx serve IN TERMINAL TO START


# Alpha Vantage API key - L3XBA0QGXTFXYDLX
# finhub API key - d7r1n5hr01qtpsm0o4igd7r1n5hr01qtpsm0o4j0
# Twelvedata API key - 2a8374f8e68f47d981d2528b5e1bad7d


# Financial modeling prep API key - e5Nh6Nn6JpHxCLwsJ0FNxSqzdhm8IMUI

# For docs sheet make sure to add /pubhtml to end of url

# Equities Data
# published sheets link - 
# published sheets link - 
# published sheets link - 
# published sheets link - 



# ETFs starting with 1ETF A2:A390
# published sheets link -https://docs.google.com/spreadsheets/d/e/2PACX-1vQwiRf-z_Zce3MiyGtRrApDLUJqMfFL7yQNzpvNoN5FZ231Sg8qBLQSDLJYXwO1exgemzt6uatpKJMX/pubhtml




# Leveraged ETFs
# published sheets link - https://docs.google.com/spreadsheets/d/e/2PACX-1vSxxTcFFwQHLbWsccqThTGKonplLZUwYIHTF6hyYOWZNpZvTJbj1TFeufYC2zPIS-wbSDV3azkbj_pq/pubhtml
# published sheets link - 
# published sheets link - 
# published sheets link - 
# published sheets link - 
# published sheets link - 
# published sheets link - 
# published sheets link - 
# published sheets link - 
# published sheets link - 
# published sheets link - 
# published sheets link - 














# Valuatio

Browser-based stock valuation + macro regime tool. Two tabs: **Valuation** (DCF / CAPM / Multiples / Monte Carlo) and **Macro Quad** (Hedgeye-style GIP from FRED).

## Run it

Drop `index.html` and `app.js` in a folder. Open `index.html` (or `npx serve` for a local URL).

## Data sources

Click **Data Sources** in the toolbar. You have three options:

- **Google Sheet** (paste a published-CSV URL) — your sheet's GOOGLEFINANCE values become live data
- **Alpha Vantage key** (free, 25 calls/day) — for full financial statements
- **Stooq + Yahoo only** (no setup) — works out of the box for prices

Source preference: **Auto** uses sheet if present else AV. **Sheet First** always prefers the sheet. **Alpha Vantage** ignores the sheet.

### Setting up the Google Sheet

In Sheets: **File → Share → Publish to web → pick the sheet → CSV → Publish**. Paste that URL into Data Sources → Test Connection.

Sheet columns are looked up by header name (case-insensitive). Recognized: `Ticker`, `Price`, `Market Cap`, `Shares`, `EPS`, `Beta`, `P/E`, `Revenue`, `FCF`, `EBITDA`, `Debt`, `Cash`, `EV/EBITDA`, `Operating Margin`, `Dividend Yield`, `Sector`, `Name`. Values can use `$1.5B`, `(2.5M)` for negatives, `31.5%`, etc.

## Macro tab

Pulls Real GDP and CPI from FRED — no key, no rate limit. Computes Quad from rate-of-change, shows ETF performance, lists representative stocks per sector (click → values them).

## Disclaimer

Educational tool, not financial advice.
