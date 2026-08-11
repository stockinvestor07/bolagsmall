"""
sektordashboard.py
Fyller i "Sektordashboard"-bladet: läser tickers från B5:B10 och skriver
motsvarande data i kolumn C-O per rad.

Återanvänder (importerar direkt, duplicerar INTE) följande från den
redan fungerande dashboard_fetch.py i repo-roten:
  - hamta_med_retry / HEADERS         (SEC EDGAR-anrop med retry+backoff)
  - hamta_rs_rating_fran_cache        (RS Rating från data/rs_cache.json,
                                        med automatisk fallback-uppdatering
                                        om cachen saknas/är för gammal)
  - sec_edgar_data_och_ranks          (Earnings Rank, Sales Rank via SEC
                                        EDGAR-percentiler, samt kvartals-
                                        historik med YoY-tillväxt per kvartal)
  - berakna_stabil_tillvaxt / _pct    (samma tillväxtformel som resten
                                        av systemet använder)

Kolumnmappning (rad 5-10, en rad per ticker i B5:B10):
  C = Rev Q %      (senaste kvartalets YoY-tillväxt, från kvartalshistorik)
  D = Rev Y %      (helårs-YoY-tillväxt, yfinance årsredovisning)
  E = Sales Rank    (SEC EDGAR-percentil, från dashboard_fetch.py)
  F = EPS Q %      (senaste kvartalets YoY-tillväxt, från kvartalshistorik)
  G = EPS Y %      (helårs-YoY-tillväxt, yfinance årsredovisning)
  H = Earnings Rank (SEC EDGAR-percentil, från dashboard_fetch.py)
  I = Avg % Surprise (SENASTE rapportens överraskning, ej snitt - enligt beslut)
  J = 90d change % (enkel prisförändring senaste ~90 kalenderdagar)
  K = RS            (RS Rating från cache, dashboard_fetch.py)
  L = ROE %         (yfinance)
  M = Inst. ägande  (yfinance)
  N = Förändring inst. ägande -> LÄMNAS TOM (kräver historik, skippas tills vidare)
  O = Float (M)     (yfinance)

Körs manuellt via GitHub Actions (workflow_dispatch). Kräver secrets
GOOGLE_SERVICE_ACCOUNT_JSON och SHEET_ID (samma som dashboard_fetch.py).
"""

import os
import sys
import time
import json
import yfinance as yf
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

import dashboard_fetch as df  # återanvänder befintlig, redan fungerande logik

SEKTOR_TAB = "Sektordashboard"
TICKER_KOLUMN = "B"
FORSTA_RAD = 5
SISTA_RAD = 10

SKRIV_KOLUMNER_1 = "C"   # start på C:M-blocket (hoppar över N)
SKRIV_KOLUMNER_1_SLUT = "M"
SKRIV_KOLUMN_FLOAT = "O"

PAUS_MELLAN_TICKERS_SEK = 1.0  # schysst mot SEC EDGAR:s fair-use-policy


def _hamta_cik(ticker):
    """Samma uppslag som i dashboard_fetch.py:s main(), men exponerad som
    egen funktion här eftersom den ligger som nästlad closure där.
    Återanvänder df.hamta_med_retry / df.HEADERS för retry+backoff."""
    r = df.hamta_med_retry("https://www.sec.gov/files/company_tickers.json", headers=df.HEADERS)
    if r is None:
        return None
    try:
        tm = r.json()
    except Exception:
        return None
    for v in tm.values():
        if v["ticker"] == ticker:
            return str(v["cik_str"]).zfill(10)
    return None


def _hamta_helars_tillvaxt(ticker_obj, rad_namn):
    """Rev Y % / EPS Y % - helårs-YoY-tillväxt från yfinance årsredovisning
    (senaste kompletta fiskala år jämfört med föregående). Använder samma
    tillväxtformel (berakna_stabil_tillvaxt) som resten av systemet."""
    try:
        fin = ticker_obj.financials  # årsdata, senaste år först (kolumn 0)
        if rad_namn not in fin.index or fin.shape[1] < 2:
            return "N/A"
        nu = fin.loc[rad_namn].iloc[0]
        da = fin.loc[rad_namn].iloc[1]
        if pd.isna(nu) or pd.isna(da):
            return "N/A"
        tv = df.berakna_stabil_tillvaxt(float(nu), float(da))
        return df._pct(tv) if tv is not None else "N/A"
    except Exception as e:
        print(f"    [helars_tillvaxt] {rad_namn} misslyckades: {e}")
        return "N/A"


def _hamta_90d_change(ticker_obj):
    try:
        hist = ticker_obj.history(period="6mo")
        if hist.empty:
            return "N/A"
        hist.index = hist.index.tz_localize(None)
        nu = hist["Close"].iloc[-1]
        malmang_datum = hist.index[-1] - pd.Timedelta(days=90)
        tidigare = hist[hist.index <= malmang_datum]
        if tidigare.empty:
            return "N/A"
        da = tidigare["Close"].iloc[-1]
        if not da:
            return "N/A"
        return round((nu - da) / da * 100, 1)
    except Exception as e:
        print(f"    [90d_change] misslyckades: {e}")
        return "N/A"


def hamta_data_for_ticker(ticker):
    print(f"  Hämtar data för {ticker}...")
    rad = {}

    t = yf.Ticker(ticker)

    # yfinance-baserade fält (återanvänder dashboard_fetch.py:s befintliga
    # yfinance-hämtning för ROE, Float, Inst. ägande och surprise-listan)
    yf_data = df.hamta_yfinance_data(ticker)
    rad["roe"] = yf_data.get("roe", "N/A")
    rad["float"] = yf_data.get("float", "N/A")
    rad["inst_agande"] = yf_data.get("inst_agande", "N/A")

    surprise_lista = yf_data.get("_surprise_lista", [])
    rad["avg_surprise"] = surprise_lista[-1] if surprise_lista else "N/A"  # senaste, ej snitt

    rad["change_90d"] = _hamta_90d_change(t)
    rad["rev_y"] = _hamta_helars_tillvaxt(t, "Total Revenue")
    rad["eps_y"] = _hamta_helars_tillvaxt(t, "Diluted EPS")

    # RS Rating från cache (med ev. fallback-ombyggnad om cachen är gammal)
    rad["rs_rating"] = df.hamta_rs_rating_fran_cache(ticker)

    # SEC EDGAR: Earnings Rank, Sales Rank, kvartalshistorik (för Rev Q% / EPS Q%)
    cik_str = _hamta_cik(ticker)
    if cik_str:
        sec_data = df.sec_edgar_data_och_ranks(ticker, cik_str, falt_saknas=[])
        rad["earnings_rank"] = sec_data.get("earnings_rank", "N/A")
        rad["sales_rank"] = sec_data.get("sales_rank", "N/A")
        kvartalshistorik = sec_data.get("kvartalshistorik", [])
        if kvartalshistorik:
            senaste_kvartal = kvartalshistorik[-1]
            rad["rev_q"] = senaste_kvartal.get("revenue_tillvaxt", "N/A")
            rad["eps_q"] = senaste_kvartal.get("eps_tillvaxt", "N/A")
        else:
            rad["rev_q"] = "N/A"
            rad["eps_q"] = "N/A"
    else:
        print(f"    Varning: kunde inte hitta CIK för {ticker}, hoppar över SEC EDGAR-fält.")
        rad["earnings_rank"] = "N/A"
        rad["sales_rank"] = "N/A"
        rad["rev_q"] = "N/A"
        rad["eps_q"] = "N/A"

    return rad


def bygg_rad_varden(rad):
    """Bygger C:M-blocket (13 kolumner: C,D,E,F,G,H,I,J,K,L,M) i rätt ordning.
    N (Förändring inst. ägande) lämnas explicit utanför."""
    return [
        rad["rev_q"],          # C
        rad["rev_y"],          # D
        rad["sales_rank"],     # E
        rad["eps_q"],          # F
        rad["eps_y"],          # G
        rad["earnings_rank"],  # H
        rad["avg_surprise"],   # I
        rad["change_90d"],     # J
        rad["rs_rating"],      # K
        rad["roe"],            # L
        rad["inst_agande"],    # M
    ]


def koppla_sheets():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.environ["SHEET_ID"]).worksheet(SEKTOR_TAB)


def main():
    ws = koppla_sheets()

    ticker_range = f"{TICKER_KOLUMN}{FORSTA_RAD}:{TICKER_KOLUMN}{SISTA_RAD}"
    ticker_celler = ws.get(ticker_range)
    tickers = [(FORSTA_RAD + i, rad[0].strip().upper())
               for i, rad in enumerate(ticker_celler) if rad and rad[0].strip()]

    if not tickers:
        print(f"Inga tickers hittades i {ticker_range}.")
        sys.exit(1)

    print(f"Hittade {len(tickers)} tickers: {', '.join(t for _, t in tickers)}")

    batch_requests = []
    fel_tickers = []

    for i, (rad_nr, ticker) in enumerate(tickers):
        try:
            rad = hamta_data_for_ticker(ticker)
            varden = bygg_rad_varden(rad)
            batch_requests.append({
                "range": f"{SKRIV_KOLUMNER_1}{rad_nr}:{SKRIV_KOLUMNER_1_SLUT}{rad_nr}",
                "values": [varden],
            })
            batch_requests.append({
                "range": f"{SKRIV_KOLUMN_FLOAT}{rad_nr}",
                "values": [[rad["float"]]],
            })
        except Exception as e:
            print(f"  FEL för {ticker}: {e}")
            fel_tickers.append(ticker)

        if i < len(tickers) - 1:
            time.sleep(PAUS_MELLAN_TICKERS_SEK)

    if batch_requests:
        print("Skriver till Google Sheets...")
        ws.batch_update(batch_requests, value_input_option="USER_ENTERED")

    if fel_tickers:
        print(f"VARNING: misslyckades helt för: {', '.join(fel_tickers)}")
        sys.exit(1)

    print(f"Klart. {len(tickers)} bolag uppdaterade i bladet '{SEKTOR_TAB}'.")


if __name__ == "__main__":
    main()
