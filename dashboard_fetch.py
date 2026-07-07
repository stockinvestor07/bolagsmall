"""
dashboard_fetch.py
Fas A - Automatiserad ifyllnad av bolagsdashboard i Google Sheets.
Körs via GitHub Actions (workflow_dispatch), tar TICKER som env-variabel.

Fält som hämtas: ROE, Current Ratio, Float, % Institutionellt ägande,
Sektor, Buy Risk, Revenue/EPS-estimat + 90d-diff, Earnings Rank, Sales Rank.
IBD Company Rank, IBD Sector Rank, Trend Stage fylls INTE i (manuellt).
"""

import os
import sys
import json
import requests
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from scipy.stats import percentileofscore

HEADERS = {"User-Agent": "Anton Werner anton.js.werner@gmail.com"}

# SEC-konstanter för Rank-beräkningar
EPS_PRIMARY_TAG = "EarningsPerShareDiluted"
EPS_FALLBACK_TAG = "EarningsPerShareBasicAndDiluted"
SALES_TAGGAR = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues", 
    "SalesRevenueNet"
]

# Cache för att förhindra dubbla nätverksanrop mot SEC Frames API
_frame_cache = {}

# ============================================================
# CELLMAPPNING - PLACEHOLDERS. Ersätt med dina exakta celler
# på bladet "Dashboard" innan första körning.
# ============================================================
CELL_MAP = {
    "roe": "P4", 
    "current_ratio": "P5", 
    "float": "P7", 
    "inst_agande": "P9", 
    "sektor": "P21", 
    "buy_risk": "P19", 
    "revenue_est_1": "L4", 
    "revenue_est_2": "M4", 
    "eps_est_1": "L8", 
    "eps_est_2": "M8", 
    "pct_diff_90d_eps": "L12", 
    "pct_diff_90d_revenue": "N4",
    "earnings_rank": "P12",  # Ny cell
    "sales_rank": "P13",     # Ny cell
    "short_percentage_of_float": "P10",
    "soliditet": "P8",
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


def berakna_stabil_tillvaxt(nu, da):
    if nu is None or da is None or da == 0: 
        return None
    justerad_namnare = max(abs(da), 0.05)
    return (nu - da) / justerad_namnare


def hamta_frame_cachad(tagg, period, enhet="USD"):
    """Hämtar och cachar SEC frames för universumsberäkning."""
    key = (tagg, period, enhet)
    if key in _frame_cache:
        return _frame_cache[key]
    
    url_enhet = "USD-per-shares" if enhet == "USD-per-shares" else "USD"
    url = f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tagg}/{url_enhet}/{period}.json"
    resultat = {}
    try:
        r = requests.get(url, headers=HEADERS)
        if r.status_code == 200:
            resultat = {str(x["cik"]).zfill(10): x["val"] for x in r.json().get("data", [])}
    except Exception:
        pass
    _frame_cache[key] = resultat
    return resultat


def verifiera_kalenderkvartal_generic(cik_str, tagg, gissad_ar, gissad_q, eget_varde, enhet="USD"):
    """Verifierar kvartal ±1 mot frames api för att säkerställa korrekt synkronisering."""
    kandidater = [(gissad_ar, gissad_q)]
    for delta in (-1, 1):
        q, ar = gissad_q + delta, gissad_ar
        if q == 0: q, ar = 4, ar - 1
        elif q == 5: q, ar = 1, ar + 1
        kandidater.append((ar, q))
    
    for ar, q in kandidater:
        frame = hamta_frame_cachad(tagg, f"CY{ar}Q{q}", enhet)
        if frame.get(cik_str) == eget_varde:
            return ar, q
    return None


def hamta_yfinance_data(ticker):
    """Hämtar Fas A-fält via yfinance. N/A per fält vid fel."""
    resultat = {}
    t = yf.Ticker(ticker)

    try:
        info = t.info
    except Exception:
        info = {}

    resultat["roe"] = _pct(info.get("returnOnEquity"))
    resultat["current_ratio"] = info.get("currentRatio", "N/A")
    resultat["float"] = float(info.get("floatShares", 0)) / 1_000_000
    resultat["inst_agande"] = _pct(info.get("heldPercentInstitutions"))
    resultat["sektor"] = info.get("sector", "N/A")
    resultat["short_percentage_of_float"] = _pct(info.get("shortPercentOfFloat"))

    try:
        hist = t.history(period="3mo")
        pris = hist["Close"].iloc[-1]
        ma50 = hist["Close"].rolling(50).mean().iloc[-1]
        resultat["buy_risk"] = _pct((pris - ma50) / ma50)
    except Exception:
        resultat["buy_risk"] = "N/A"

    # --- SOLIDITET (SENASTE KVARTALET) ---
    try:
        q_bs = t.quarterly_balance_sheet
        # Den senaste rapporten är den första kolumnen (index 0)
        ek = q_bs.loc["Stockholders Equity"].iloc[0]
        tillgangar = q_bs.loc["Total Assets"].iloc[0]
        
        if pd.notna(ek) and pd.notna(tillgangar) and tillgangar != 0:
            resultat["soliditet"] = _pct(ek / tillgangar)
        else:
            resultat["soliditet"] = "N/A"
    except Exception:
        resultat["soliditet"] = "N/A"

    # --- REVENUE ESTIMATES (KVARTAL I MUSD) ---
    try:
        rev_est = t.get_revenue_estimate()
        
        if "0q" in rev_est.index and rev_est.loc["0q", "avg"] is not None:
            resultat["revenue_est_1"] = round(rev_est.loc["0q", "avg"] / 1000000, 1)
        else:
            resultat["revenue_est_1"] = "N/A"
            
        if "+1q" in rev_est.index and rev_est.loc["+1q", "avg"] is not None:
            resultat["revenue_est_2"] = round(rev_est.loc["+1q", "avg"] / 1000000, 1)
        else:
            resultat["revenue_est_2"] = "N/A"
            
    except Exception:
        resultat["revenue_est_1"] = "N/A"
        resultat["revenue_est_2"] = "N/A"

    # --- EPS ESTIMATES (KVARTAL) ---
    try:
        eps_est = t.get_earnings_estimate()
        # Ändrat från "0y"/"+1y" till "0q" (nuvarande kvartal) och "+1q" (nästkommande)
        resultat["eps_est_1"] = eps_est.loc["0q", "avg"] if "0q" in eps_est.index else "N/A"
        resultat["eps_est_2"] = eps_est.loc["+1q", "avg"] if "+1q" in eps_est.index else "N/A"
    except Exception:
        resultat["eps_est_1"] = "N/A"
        resultat["eps_est_2"] = "N/A"

    # --- EPS TREND % DIFF 90D (KVARTAL) ---
    try:
        eps_trend = t.get_eps_trend()
        # Ändrat från "0y" till "0q" för att matcha % Diff (90d) mot nuvarande kvartal
        nu = eps_trend.loc["0q", "current"]
        for_90d = eps_trend.loc["0q", "90daysAgo"]
        resultat["pct_diff_90d_eps"] = _pct((nu - for_90d) / abs(for_90d)) if for_90d else "N/A"
    except Exception:
        resultat["pct_diff_90d_eps"] = "N/A"

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


def sec_edgar_data_och_ranks(ticker, cik_str, falt_saknas, berakna_ranks=True):
    """
    Kombinerad funktion för att hämta fallback-fält (ROE, Current Ratio) 
    samt beräkna global Earnings Rank och Sales Rank via SEC EDGAR.
    """
    resultat = {"earnings_rank": "N/A", "sales_rank": "N/A"}
    try:
        facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_str}.json"
        facts = requests.get(facts_url, headers=HEADERS).json()
        gaap = facts.get("facts", {}).get("us-gaap", {})

        # 1. Fallback-hantering för ROE & Current Ratio
        if "roe" in falt_saknas:
            ni = _senaste_varde(gaap, "NetIncomeLoss")
            eq = _senaste_varde(gaap, "StockholdersEquity")
            resultat["roe"] = round((ni / eq) * 100, 1) if ni and eq else "N/A"

        if "current_ratio" in falt_saknas:
            ca = _senaste_varde(gaap, "AssetsCurrent")
            cl = _senaste_varde(gaap, "LiabilitiesCurrent")
            resultat["current_ratio"] = round(ca / cl, 2) if ca and cl else "N/A"

        if not berakna_ranks:
            return resultat

        # 2. EARNINGS RANK BERÄKNING
        eps_tagg = EPS_PRIMARY_TAG if EPS_PRIMARY_TAG in gaap else (EPS_FALLBACK_TAG if EPS_FALLBACK_TAG in gaap else None)
        if eps_tagg:
            eps_enheter = gaap[eps_tagg].get("units", {}).get("USD/shares", [])
            eps_giltiga = [x for x in eps_enheter if x.get("form") in ["10-Q", "10-Q/A", "10-K", "10-K/A"]]
            if eps_giltiga:
                eps_giltiga.sort(key=lambda x: x.get("filed", ""))
                senaste_eps_rapp = eps_giltiga[-1]
                eps_nu = senaste_eps_rapp["val"]
                
                dt = datetime.strptime(senaste_eps_rapp["filed"], "%Y-%m-%d")
                gissad_ar = dt.year
                gissad_q = 4 if dt.month in [1, 2, 3] else (1 if dt.month in [4, 5, 6] else (2 if dt.month in [7, 8, 9] else 3))
                if dt.month in [1, 2, 3]: 
                    gissad_ar -= 1

                eps_period = verifiera_kalenderkvartal_generic(cik_str, eps_tagg, gissad_ar, gissad_q, eps_nu, "USD-per-shares")
                if eps_period:
                    eps_ar, eps_q = eps_period
                    eps_nu_dict = hamta_frame_cachad(eps_tagg, f"CY{eps_ar}Q{eps_q}", "USD-per-shares")
                    eps_da_dict = hamta_frame_cachad(eps_tagg, f"CY{eps_ar-1}Q{eps_q}", "USD-per-shares")

                    eps_universum = []
                    for c_str, val_nu in eps_nu_dict.items():
                        if c_str in eps_da_dict:
                            t_v = berakna_stabil_tillvaxt(val_nu, eps_da_dict[c_str])
                            if t_v is not None: 
                                eps_universum.append(t_v)
                    
                    target_eps_tillv = berakna_stabil_tillvaxt(eps_nu, eps_da_dict.get(cik_str))
                    if target_eps_tillv is not None:
                        if target_eps_tillv not in eps_universum: 
                            eps_universum.append(target_eps_tillv)
                        resultat["earnings_rank"] = round(percentileofscore(eps_universum, target_eps_tillv, kind='weak'))

        # 3. SALES RANK BERÄKNING
        sales_tagg = None
        sales_kvartal = []
        for t_tagg in SALES_TAGGAR:
            if t_tagg in gaap:
                for p in gaap[t_tagg]["units"].get("USD", []):
                    if p.get("form") not in ("10-Q", "10-Q/A"): 
                        continue
                    try:
                        d = (datetime.strptime(p["end"], "%Y-%m-%d") - datetime.strptime(p["start"], "%Y-%m-%d")).days
                        if 79 <= d <= 103:
                            sales_kvartal.append((datetime.strptime(p["end"], "%Y-%m-%d"), p["val"]))
                    except Exception:
                        continue
                if sales_kvartal:
                    sales_tagg = t_tagg
                    break

        if sales_tagg and sales_kvartal:
            slutdatum, kant_varde = max(sales_kvartal, key=lambda x: x[0])
            sales_period = verifiera_kalenderkvartal_generic(cik_str, sales_tagg, slutdatum.year, (slutdatum.month - 1) // 3 + 1, kant_varde, "USD")
            if sales_period:
                s_ar, s_q = sales_period
                sales_nu_dict = hamta_frame_cachad(sales_tagg, f"CY{s_ar}Q{s_q}", "USD")
                sales_da_dict = hamta_frame_cachad(sales_tagg, f"CY{s_ar-1}Q{s_q}", "USD")

                sales_universum = {c: (sales_nu_dict[c] - sales_da_dict[c]) / abs(sales_da_dict[c]) 
                                   for c in sales_nu_dict if c in sales_da_dict and sales_da_dict[c] != 0}
                
                if cik_str in sales_universum:
                    resultat["sales_rank"] = round(percentileofscore(list(sales_universum.values()), sales_universum[cik_str], kind="weak"))

    except Exception as e:
        print(f"Fel vid SEC-exekvering: {e}")
    
    return resultat


def skriv_till_sheets(data):
    """Skriver data till Google Sheets enligt CELL_MAP och verifierar cellvärdet."""
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
            
        # --- FIX: Konvertera numpy/scipy-typer till inbyggda Python-typer ---
        if hasattr(varde, "item"):  # Träffar numpy.int64, numpy.float64 etc.
            varde = varde.item()
        # -------------------------------------------------------------------

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

    # Slå upp CIK-nummer för SEC-anrop
    cik_str = None
    try:
        tm = requests.get("https://www.sec.gov/files/company_tickers.json", headers=HEADERS).json()
        for v in tm.values():
            if v["ticker"] == ticker:
                cik_str = str(v["cik_str"]).zfill(10)
                break
    except Exception as e:
        print(f"Kunde inte ladda ticker-mappning: {e}")

    saknas = [k for k in ("roe", "current_ratio") if data.get(k) == "N/A"]

    if cik_str:
        print(f"Hämtar Ranks och saknade fält {saknas} från SEC EDGAR...")
        sec_data = sec_edgar_data_och_ranks(ticker, cik_str, saknas, berakna_ranks=True)
        data.update(sec_data)
    else:
        print(f"Varning: Kunde inte hitta CIK för {ticker}. Sätter Ranks till N/A.")
        data["earnings_rank"] = "N/A"
        data["sales_rank"] = "N/A"

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
