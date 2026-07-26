"""
marknadsbredd.py
Beräknar marknadsbredd: andel S&P 500-bolag som handlas över 50MA
respektive 200MA. Skriver resultatet till Marknadsdashboard-fliken.

Körs manuellt via GitHub Actions (workflow_dispatch), oberoende av
dashboard_fetch.py (som är per-ticker och skriver till en annan flik,
"Dashboard"). Marknadsbredd är marknadsbred data och hör hemma i ett
eget script/workflow, precis som rs_rating_update.py hanterar sitt
eget universum separat från dashboard_fetch.py.

Tickerlistan (S&P 500) hämtas från Wikipedia och cachas i
data/sp500_tickers.json. Cachen återanvänds om den är färsk nog
(se SP500_CACHE_MAX_ALDER_DAGAR), annars hämtas listan om.

Kursdata hämtas i EN batch via yf.download() istället för en loop med
ett anrop per ticker (~500 separata anrop), för att minimera antal
requests och risken för rate-limiting.
"""

import os
import sys
import json
import io
from datetime import datetime, timezone

import requests
import pandas as pd
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

USER_AGENT = os.environ.get("SEC_USER_AGENT", "Anton Werner anton.js.werner@gmail.com")
HEADERS = {"User-Agent": USER_AGENT}
REQUEST_TIMEOUT = 15

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SP500_CACHE_FIL = os.environ.get("SP500_CACHE_FIL", "data/sp500_tickers.json")
SP500_CACHE_MAX_ALDER_DAGAR = 7

SHEET_ID = os.environ["SHEET_ID"]
SHEET_TAB = "Marknadsdashboard"
CELL_200MA = "C8"
CELL_50MA = "C9"

MA_KORT = 50
MA_LANG = 200
HISTORIK_PERIOD = "1y"  # räcker gott för 200MA (~252 handelsdagar)


# ============================================================
# TICKERLISTA (S&P 500, cachad)
# ============================================================
def _las_ticker_cache(filnamn=SP500_CACHE_FIL):
    if not os.path.exists(filnamn):
        return None
    try:
        with open(filnamn, "r", encoding="utf-8") as f:
            data = json.load(f)
        uppdaterad = datetime.fromisoformat(data["Updated_At"])
        alder = datetime.now(timezone.utc) - uppdaterad
        if alder.days > SP500_CACHE_MAX_ALDER_DAGAR:
            return None
        tickers = data.get("tickers", [])
        return tickers if tickers else None
    except Exception as e:
        print(f"  [sp500_cache] kunde inte läsa '{filnamn}': {e}")
        return None


def _skriv_ticker_cache(tickers, filnamn=SP500_CACHE_FIL):
    try:
        os.makedirs(os.path.dirname(filnamn), exist_ok=True)
        with open(filnamn, "w", encoding="utf-8") as f:
            json.dump(
                {"Updated_At": datetime.now(timezone.utc).isoformat(), "tickers": tickers},
                f, indent=2,
            )
    except Exception as e:
        print(f"  [sp500_cache] kunde inte skriva '{filnamn}': {e}")


def hamta_sp500_tickers():
    cachad = _las_ticker_cache()
    if cachad:
        print(f"  [sp500] Använder cachad lista ({len(cachad)} tickers, "
              f"< {SP500_CACHE_MAX_ALDER_DAGAR} dagar gammal).")
        return cachad

    print("  [sp500] Cache saknas/inaktuell - hämtar aktuell lista från Wikipedia...")
    try:
        r = requests.get(WIKIPEDIA_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        tabeller = pd.read_html(io.StringIO(r.text))
        symboler = tabeller[0]["Symbol"].tolist()
        # Yahoo Finance använder "-" istället för "." i vissa tickers (t.ex. BRK.B -> BRK-B)
        tickers = [str(s).strip().replace(".", "-") for s in symboler]
    except Exception as e:
        print(f"  [sp500] Wikipedia-hämtning misslyckades: {e}")
        aldre_cache = _las_ticker_cache_ignorera_alder()
        if aldre_cache:
            print(f"  [sp500] Faller tillbaka på gammal cache ({len(aldre_cache)} tickers).")
            return aldre_cache
        raise

    _skriv_ticker_cache(tickers)
    print(f"  [sp500] Hämtade {len(tickers)} tickers, cachade i '{SP500_CACHE_FIL}'.")
    return tickers


def _las_ticker_cache_ignorera_alder(filnamn=SP500_CACHE_FIL):
    if not os.path.exists(filnamn):
        return None
    try:
        with open(filnamn, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("tickers") or None
    except Exception:
        return None


# ============================================================
# BREDDBERÄKNING
# ============================================================
def berakna_marknadsbredd(tickers):
    """
    Hämtar 1 års historik för samtliga tickers i EN batch och beräknar
    andelen som stänger över sitt 50-dagars respektive 200-dagars
    glidande medelvärde. Tickers utan tillräcklig historik (nyligen
    listade, delistade, datafel) exkluderas ur nämnaren - loggas separat.
    """
    print(f"  [yfinance] Batch-hämtar {len(tickers)} tickers ({HISTORIK_PERIOD})...")
    rad = yf.download(
        tickers,
        period=HISTORIK_PERIOD,
        group_by="ticker",
        threads=True,
        auto_adjust=True,
        progress=False,
    )

    over_200ma = 0
    over_50ma = 0
    giltiga = 0
    misslyckade = []

    for ticker in tickers:
        try:
            if len(tickers) == 1:
                close = rad["Close"]
            else:
                close = rad[ticker]["Close"]
            close = close.dropna()

            if len(close) < MA_LANG:
                misslyckade.append(ticker)
                continue

            pris = close.iloc[-1]
            ma50 = close.rolling(MA_KORT).mean().iloc[-1]
            ma200 = close.rolling(MA_LANG).mean().iloc[-1]

            if pd.isna(ma50) or pd.isna(ma200):
                misslyckade.append(ticker)
                continue

            giltiga += 1
            if pris > ma50:
                over_50ma += 1
            if pris > ma200:
                over_200ma += 1
        except Exception:
            misslyckade.append(ticker)

    if giltiga == 0:
        raise RuntimeError("Ingen ticker gav giltig data - avbryter.")

    if misslyckade:
        print(f"  [yfinance] {len(misslyckade)}/{len(tickers)} tickers exkluderade "
              f"(otillräcklig historik/datafel): {', '.join(misslyckade[:20])}"
              f"{' ...' if len(misslyckade) > 20 else ''}")

    pct_200ma = round(over_200ma / giltiga * 100, 1)
    pct_50ma = round(over_50ma / giltiga * 100, 1)

    print(f"  [resultat] {giltiga} giltiga tickers. "
          f"Över 200MA: {over_200ma} ({pct_200ma}%). Över 50MA: {over_50ma} ({pct_50ma}%).")

    return pct_200ma, pct_50ma, giltiga, len(misslyckade)


# ============================================================
# GOOGLE SHEETS
# ============================================================
def skriv_till_sheets(pct_200ma, pct_50ma):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)

    ws.batch_update(
        [
            {"range": CELL_200MA, "values": [[pct_200ma / 100]]},
            {"range": CELL_50MA, "values": [[pct_50ma / 100]]},
        ],
        value_input_option="USER_ENTERED",
    )
    # Värden skrivs som decimalandel (0.62 istället för 62), i linje med
    # övriga %-formaterade celler i Marknadsdashboard (t.ex. 1v %, 1mån %).


def main():
    print("Beräknar marknadsbredd (S&P 500)...")
    tickers = hamta_sp500_tickers()
    pct_200ma, pct_50ma, giltiga, antal_misslyckade = berakna_marknadsbredd(tickers)

    print(f"Skriver till Google Sheets ({SHEET_TAB} -> {CELL_200MA}, {CELL_50MA})...")
    skriv_till_sheets(pct_200ma, pct_50ma)

    print(f"Klart. {giltiga} bolag inkluderade, {antal_misslyckade} exkluderade.")


if __name__ == "__main__":
    main()
