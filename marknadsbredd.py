"""
marknadsbredd.py
Beräknar marknadsbredd: andel S&P 1500-bolag (S&P 500 + MidCap 400 +
SmallCap 600) som handlas över 50MA respektive 200MA. Skriver resultatet
till Marknadsdashboard-fliken.

S&P 1500 valdes istället för S&P 500 eller NYSE Composite:
- S&P 500 ensamt är för smalt (megacap-tungt, kan dölja svaghet i bredden)
- NYSE Composite innehåller tusentals icke-aktie-noteringar (preferensaktier,
  bond-CEFs, REITs, SPAC-skal) som gör breddata strukturellt missvisande
- S&P 1500 (large+mid+small cap, ren common-stock-korg, ~87% av US
  market cap) ger en bredare, renare bredd-signal utan kompositionsbruset

Körs manuellt via GitHub Actions (workflow_dispatch), oberoende av
dashboard_fetch.py (som är per-ticker och skriver till en annan flik,
"Dashboard").

Tickerlistan hämtas FÄRSK från Wikipedia vid varje körning (ingen cache -
tre snabba sidhämtningar, försumbar kostnad jämfört med kursdata-hämtningen).

Kursdata hämtas i EN batch via yf.download() istället för en loop med
ett anrop per ticker (~1500 separata anrop), för att minimera antal
requests och risken för rate-limiting.
"""

import os
import sys
import json
import io

import requests
import pandas as pd
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

USER_AGENT = os.environ.get("SEC_USER_AGENT", "Anton Werner anton.js.werner@gmail.com")
HEADERS = {"User-Agent": USER_AGENT}
REQUEST_TIMEOUT = 15

WIKIPEDIA_SIDOR = {
    "S&P 500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "S&P MidCap 400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "S&P SmallCap 600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

SHEET_ID = os.environ["SHEET_ID"]
SHEET_TAB = "Marknadsdashboard"
CELL_200MA = "O4"
CELL_50MA = "O5"

MA_KORT = 50
MA_LANG = 200
HISTORIK_PERIOD = "1y"  # räcker gott för 200MA (~252 handelsdagar)


# ============================================================
# TICKERLISTA (S&P 1500, hämtas färsk varje körning)
# ============================================================
def _stada_ticker(symbol):
    # Yahoo Finance använder "-" istället för "." (t.ex. BRK.B -> BRK-B)
    return str(symbol).strip().replace(".", "-")


def _hamta_tabell(url, namn):
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    tabeller = pd.read_html(io.StringIO(r.text))
    df = tabeller[0]
    kolumn = "Symbol" if "Symbol" in df.columns else "Ticker symbol"
    tickers = [_stada_ticker(s) for s in df[kolumn].tolist()]
    print(f"  [sp1500] {namn}: {len(tickers)} tickers hämtade från Wikipedia.")
    return tickers


def hamta_sp1500_tickers():
    print("  [sp1500] Hämtar S&P 500 + MidCap 400 + SmallCap 600 från Wikipedia...")
    alla = []
    for namn, url in WIKIPEDIA_SIDOR.items():
        try:
            alla.extend(_hamta_tabell(url, namn))
        except Exception as e:
            print(f"  [sp1500] VARNING: kunde inte hämta '{namn}': {e}")

    unika = sorted(set(alla))
    if not unika:
        raise RuntimeError("Kunde inte hämta någon S&P 1500-ticker från Wikipedia - avbryter.")

    print(f"  [sp1500] Totalt {len(unika)} unika tickers (S&P 1500).")
    return unika


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

    print(f"  [resultat] {giltiga} giltiga tickers (S&P 1500). "
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
    print("Beräknar marknadsbredd (S&P 1500)...")
    tickers = hamta_sp1500_tickers()
    pct_200ma, pct_50ma, giltiga, antal_misslyckade = berakna_marknadsbredd(tickers)

    print(f"Skriver till Google Sheets ({SHEET_TAB} -> {CELL_200MA}, {CELL_50MA})...")
    skriv_till_sheets(pct_200ma, pct_50ma)

    print(f"Klart. {giltiga} bolag inkluderade, {antal_misslyckade} exkluderade.")


if __name__ == "__main__":
    main()
