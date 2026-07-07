"""
dashboard_fetch.py
Fas A - Automatiserad ifyllnad av bolagsdashboard i Google Sheets.
Körs via GitHub Actions (workflow_dispatch), tar TICKER som env-variabel.

Fält som hämtas: ROE, Current Ratio, Float, % Institutionellt ägande,
Sektor, Buy Risk, Revenue/EPS-estimat + 90d-diff.
IBD Company Rank, IBD Sector Rank, Trend Stage fylls INTE i (manuellt).
"""

import os
import sys
import json
import requests
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

HEADERS = {"User-Agent": "Anton Werner anton.js.werner@gmail.com"}

# ============================================================
# CELLMAPPNING - PLACEHOLDERS. Ersätt med dina exakta celler
# på bladet "Dashboard" innan första körning.
# ============================================================
CELL_MAP = {
    "roe": "REPLACE_ME",
    "current_ratio": "REPLACE_ME",
    "float": "REPLACE_ME",
    "inst_agande": "REPLACE_ME",
    "sektor": "REPLACE_ME",
    "buy_risk": "REPLACE_ME",
    "revenue_est_1": "REPLACE_ME",
    "revenue_est_2": "REPLACE_ME",
    "eps_est_1": "REPLACE_ME",
    "eps_est_2": "REPLACE_ME",
    "pct_diff_90d_eps": "REPLACE_ME",
    "pct_diff_90d_revenue": "REPLACE_ME",
}

SHEET_ID = os.environ["SHEET_ID"]
SHEET_TAB = "Dashboard"


def _pct(varde):
    if varde is None:
        return "N/A"
    try:
        return round(varde * 100, 1)
    except Exception:
        return "N/A"


def hamta_yfinance_data(ticker):
    """Hämtar Fas A-fält via yfinance. N/A per fält vid fel, kraschar aldrig hela körningen."""
    resultat = {}
    t = yf.Ticker(ticker)

    try:
        info = t.info
    except Exception:
        info = {}

    resultat["roe"] = _pct(info.get("returnOnEquity"))
    resultat["current_ratio"] = info.get("currentRatio", "N/A")
    resultat["float"] = info.get("floatShares", "N/A")
    resultat["inst_agande"] = _pct(info.get("heldPercentInstitutions"))
    resultat["sektor"] = info.get("sector", "N/A")

    try:
        hist = t.history(period="3mo")
        pris = hist["Close"].iloc[-1]
        ma50 = hist["Close"].rolling(50).mean().iloc[-1]
        resultat["buy_risk"] = _pct((pris - ma50) / ma50)
    except Exception:
        resultat["buy_risk"] = "N/A"

    try:
        rev_est = t.get_revenue_estimate()
        resultat["revenue_est_1"] = rev_est.loc["0y", "avg"] if "0y" in rev_est.index else "N/A"
        resultat["revenue_est_2"] = rev_est.loc["+1y", "avg"] if "+1y" in rev_est.index else "N/A"
    except Exception:
        resultat["revenue_est_1"] = "N/A"
        resultat["revenue_est_2"] = "N/A"

    try:
        eps_est = t.get_earnings_estimate()
        resultat["eps_est_1"] = eps_est.loc["0y", "avg"] if "0y" in eps_est.index else "N/A"
        resultat["eps_est_2"] = eps_est.loc["+1y", "avg"] if "+1y" in eps_est.index else "N/A"
    except Exception:
        resultat["eps_est_1"] = "N/A"
        resultat["eps_est_2"] = "N/A"

    try:
        eps_trend = t.get_eps_trend()
        nu = eps_trend.loc["0y", "current"]
        for_90d = eps_trend.loc["0y", "90daysAgo"]
        resultat["pct_diff_90d_eps"] = _pct((nu - for_90d) / abs(for_90d)) if for_90d else "N/A"
    except Exception:
        resultat["pct_diff_90d_eps"] = "N/A"

    # yfinance saknar tillförlitlig revenue-revisionshistorik - alltid N/A i Fas A
    resultat["pct_diff_90d_revenue"] = "N/A"

    return resultat


def _senaste_varde(gaap_facts, tagg):
    if tagg not in gaap_facts:
        return None
    poster = gaap_facts[tagg]["units"].get("USD", [])
    giltiga = [p for p in poster if p.get("form") in ("10-Q", "10-K")]
    if not giltiga:
        return None
    giltiga.sort(key=lambda x: x.get("end", ""))
    return giltiga[-1]["val"]


def sec_edgar_fallback(ticker, falt_saknas):
    """Fyller i saknade fält (ROE, Current Ratio) via SEC EDGAR companyfacts."""
    if not falt_saknas:
        return {}

    resultat = {}
    try:
        tm = requests.get("https://www.sec.gov/files/company_tickers.json", headers=HEADERS).json()
        cik = None
        for v in tm.values():
            if v["ticker"] == ticker:
                cik = str(v["cik_str"]).zfill(10)
                break
        if not cik:
            return resultat

        facts = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", headers=HEADERS).json()
        gaap = facts.get("facts", {}).get("us-gaap", {})

        if "roe" in falt_saknas:
            ni = _senaste_varde(gaap, "NetIncomeLoss")
            eq = _senaste_varde(gaap, "StockholdersEquity")
            resultat["roe"] = round((ni / eq) * 100, 1) if ni and eq else "N/A"

        if "current_ratio" in falt_saknas:
            ca = _senaste_varde(gaap, "AssetsCurrent")
            cl = _senaste_varde(gaap, "LiabilitiesCurrent")
            resultat["current_ratio"] = round(ca / cl, 2) if ca and cl else "N/A"

    except Exception:
        pass

    return resultat


def skriv_till_sheets(data):
    """Skriver data till Google Sheets enligt CELL_MAP. Läser tillbaka varje cell
    efter skrivning och jämför mot skickat värde (verifiering mot fel cellmappning)."""
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)

    ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)

    fel = []
    for falt, varde in data.items():
        cell = CELL_MAP.get(falt)
        if not cell or cell == "REPLACE_ME":
            fel.append(f"{falt}: ingen giltig cell angiven i CELL_MAP")
            continue
        ws.update_acell(cell, varde)
        lastvarde = ws.acell(cell).value
        if str(lastvarde) != str(varde):
            fel.append(f"{falt} ({cell}): skrev '{varde}', läste tillbaka '{lastvarde}'")

    return fel


def main():
    ticker = os.environ.get("TICKER", "").strip().upper()
    if not ticker:
        print("Ingen ticker angiven.")
        sys.exit(1)

    print(f"Hämtar data för {ticker}...")
    data = hamta_yfinance_data(ticker)

    saknas = [k for k in ("roe", "current_ratio") if data.get(k) == "N/A"]
    if saknas:
        print(f"Fyller i via SEC EDGAR: {saknas}")
        data.update(sec_edgar_fallback(ticker, saknas))

    print("Skriver till Google Sheets...")
    fel = skriv_till_sheets(data)

    if fel:
        print("VARNING - verifieringsfel:")
        for f in fel:
            print(f"  {f}")
        sys.exit(1)

    print(f"Klart. {ticker} skrivet till bladet '{SHEET_TAB}'.")


if __name__ == "__main__":
    main()
