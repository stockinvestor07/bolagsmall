"""
rs_rating_update.py
Beräknar RS Rating (IBD-stil) för HELA aktie-universumet (~3000 bolag)
och cachar resultatet i en separat Sheets-flik ("RS_Cache").

VARFÖR ETT EGET SCRIPT:
Tidigare räknades RS Rating om från grunden (nedladdning av 1 års
historik för ~3000 bolag, ~60 sekunder) varje gång EN enskild ticker
skulle uppdateras i dashboard_fetch.py. Percentilen för universumet
ändras marginellt dag för dag, så det är onödigt dyrt att göra om
hela beräkningen vid varje enskild körning.

KÖRSCHEMA:
Kör detta script schemalagt måndag + torsdag (inte vid varje
dashboard-körning). Exempel på GitHub Actions-cron (UTC):

  on:
    schedule:
      - cron: "0 11 * * 1,4"   # Måndag och torsdag kl 11:00 UTC

dashboard_fetch.py läser sedan bara upp redan beräknat RS Rating för
aktuell ticker ur "RS_Cache"-fliken (hamta_rs_rating_fran_cache).
"""

import os
import sys
import time
import json
import requests
import yfinance as yf
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = os.environ.get("SEC_USER_AGENT", "Anton Werner anton.js.werner@gmail.com")
HEADERS = {"User-Agent": USER_AGENT}
REQUEST_TIMEOUT = 15

SHEET_ID = os.environ["SHEET_ID"]
CACHE_TAB = "RS_Cache"
CACHE_HEADER = ["Ticker", "RS_Score", "RS_Rank", "Updated_At"]

BATCH_STORLEK = 500
MAX_WORKERS = 6
MIN_HANDELSDAGAR = 240
MIN_LYCKAD_ANDEL_VARNING = 80.0  # varna om färre än X% av universumet gick att beräkna


def hamta_med_retry(url, headers=None, forsok=3, backoff=2):
    """Enkel retry med exponentiell backoff + explicit timeout (saknades helt tidigare)."""
    for i in range(forsok):
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r
            print(f"  [retry] {url} -> HTTP {r.status_code} (försök {i + 1}/{forsok})")
        except requests.RequestException as e:
            print(f"  [retry] {url} -> {e} (försök {i + 1}/{forsok})")
        if i < forsok - 1:
            time.sleep(backoff * (i + 1))
    return None


def normalisera_ticker_for_yf(ticker):
    # yfinance använder "-" för aktieklass (t.ex. BRK-B), inte "."
    return ticker.replace(".", "-")


def bygg_universum(sec_tickers_dict, target_ticker=None):
    """
    Tidigare bugg: filtret 'isalpha()' uteslöt redan alla tickers med
    bindestreck/punkt (t.ex. BRK-B), vilket gjorde de efterföljande
    .replace()-anropen till no-ops och tystade bort en hel kategori av
    bolag ur RS-universumet. Om target_ticker själv hade "-" eller "."
    fanns den dessutom ALDRIG i universum, vilket gjorde att
    'universum[-1] = target_ticker' tyst skrev över en annan akties plats.

    Fix: tillåt alnum + "." + "-", normalisera till yfinance-format, och
    lägg till target_ticker separat (utan att skriva över någon annan post).
    """
    unika, seen = [], set()
    for v in sec_tickers_dict.values():
        raw = v.get("ticker", "")
        if not raw or not all(c.isalnum() or c in ".-" for c in raw):
            continue
        t = normalisera_ticker_for_yf(raw)
        if t not in seen:
            seen.add(t)
            unika.append(t)

    universum = unika[:3000]
    if target_ticker and target_ticker not in universum:
        universum.append(target_ticker)
    return universum


def _bearbeta_rs_batch(batch):
    lokal, misslyckade = [], []
    try:
        raw_data = yf.download(
            batch, period="1y", auto_adjust=True, group_by="ticker", progress=False, threads=True
        )
    except Exception as e:
        print(f"  [rs_batch] hela batchen misslyckades ({len(batch)} tickers): {e}")
        return [], list(batch)

    for ticker in batch:
        try:
            if ticker not in raw_data.columns.levels[0]:
                misslyckade.append(ticker)
                continue
            close = raw_data[ticker]["Close"].dropna().values
            if len(close) < MIN_HANDELSDAGAR:
                misslyckade.append(ticker)
                continue

            ret_3m = (close[-1] - close[-63]) / close[-63]
            ret_6m = (close[-1] - close[-126]) / close[-126]
            ret_9m = (close[-1] - close[-189]) / close[-189]
            ret_12m = (close[-1] - close[0]) / close[0]

            rs_score = (ret_3m * 2) + ret_6m + ret_9m + ret_12m
            lokal.append({"Ticker": ticker, "RS_Score": rs_score})
        except Exception:
            misslyckade.append(ticker)
    return lokal, misslyckade


def berakna_rs_universum(sec_tickers_dict, target_ticker=None):
    universum = bygg_universum(sec_tickers_dict, target_ticker)
    batches = [universum[i:i + BATCH_STORLEK] for i in range(0, len(universum), BATCH_STORLEK)]

    master, alla_misslyckade = [], []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for lokal, misslyckade in executor.map(_bearbeta_rs_batch, batches):
            master.extend(lokal)
            alla_misslyckade.extend(misslyckade)

    andel = (len(master) / len(universum) * 100) if universum else 0.0
    print(f"RS-universum: {len(master)}/{len(universum)} tickers lyckades ({andel:.1f}%).")
    if andel < MIN_LYCKAD_ANDEL_VARNING:
        exempel = alla_misslyckade[:10]
        print(f"  VARNING: låg träffandel. {len(alla_misslyckade)} tickers misslyckades, "
              f"t.ex.: {exempel}")

    df = pd.DataFrame(master)
    if df.empty or "RS_Score" not in df.columns:
        return df

    # Tidigare: percentileofscore i en .apply-loop = O(n^2), full scan av
    # arrayen för VARJE rad (~9 miljoner jämförelser för 3000 tickers).
    # .rank(pct=True) gör samma sak i O(n log n) via en enda sortering.
    df["RS_Rank"] = (
        df["RS_Score"].rank(pct=True, method="max") * 100
    ).round().astype(int).clip(1, 99)
    return df


def skriv_cache_till_sheets(df):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    try:
        ws = sh.worksheet(CACHE_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=CACHE_TAB, rows=len(df) + 10, cols=len(CACHE_HEADER))

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [CACHE_HEADER]
    for _, r in df.iterrows():
        rows.append([r["Ticker"], round(float(r["RS_Score"]), 4), int(r["RS_Rank"]), now])

    ws.clear()
    ws.update(range_name="A1", values=rows)  # EN batch-skrivning för hela universumet


def main():
    print("Hämtar ticker->CIK-mappning...")
    r = hamta_med_retry("https://www.sec.gov/files/company_tickers.json", headers=HEADERS)
    if r is None:
        print("Kunde inte hämta ticker-mappning efter flera försök. Avbryter.")
        sys.exit(1)
    tm = r.json()

    print("Beräknar RS Rating för hela universumet (tar ~60 sekunder eller mer)...")
    df = berakna_rs_universum(tm)
    if df.empty:
        print("Ingen RS-data kunde beräknas. Avbryter utan att skriva till cache.")
        sys.exit(1)

    print(f"Skriver {len(df)} rader till fliken '{CACHE_TAB}'...")
    skriv_cache_till_sheets(df)
    print("Klart.")


if __name__ == "__main__":
    main()
