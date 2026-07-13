"""
rs_rating_update.py
Beräknar RS Rating (IBD-stil) för HELA aktie-universumet (~3000 bolag)
och cachar resultatet i en JSON-fil i repot ("data/rs_cache.json").

VARFÖR ETT EGET SCRIPT:
Tidigare räknades RS Rating om från grunden (nedladdning av 1 års
historik för ~3000 bolag, ~60 sekunder) varje gång EN enskild ticker
skulle uppdateras i dashboard_fetch.py. Percentilen för universumet
ändras marginellt dag för dag, så det är onödigt dyrt att göra om
hela beräkningen vid varje enskild körning.

VARFÖR JSON-FIL I REPOT (inte Google Sheets):
RS-cachen är ren maskindata utan värde för manuell uppföljning, och
skulle bara skräpa ner Sheeten (som är till för egen utvärdering).
Filen versionshanteras istället i repot och committas automatiskt
av detta script.

KÖRSCHEMA:
Kör detta script schemalagt måndag + torsdag (inte vid varje
dashboard-körning). Se .github/workflows/rs-rating-update.yml
(cron "0 11 * * 1,4", UTC).

dashboard_fetch.py läser sedan bara upp redan beräknat RS Rating för
aktuell ticker ur data/rs_cache.json (hamta_rs_rating_fran_cache).
Om filen saknas eller är för gammal körs detta scripts main() som
en engångs-fallback DIREKT I MINNET av dashboard_fetch.py (se
sakerstall_farsk_rs_cache) - den fallback-körningen committas
INTE tillbaka till repot, den permanenta filen uppdateras nästa
schemalagda mån/tors-körning som vanligt.
"""

import os
import sys
import time
import json
import subprocess
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = os.environ.get("SEC_USER_AGENT", "Anton Werner anton.js.werner@gmail.com")
HEADERS = {"User-Agent": USER_AGENT}
REQUEST_TIMEOUT = 15

# Filen ligger i repo-roten under data/. Path är relativ till cwd, vilket i
# GitHub Actions alltid är repo-roten (actions/checkout@v4 checkar ut dit).
CACHE_FIL = os.environ.get("RS_CACHE_FIL", "data/rs_cache.json")

BATCH_STORLEK = 250
MAX_WORKERS = 2
MIN_HANDELSDAGAR = 240
MIN_LYCKAD_ANDEL_VARNING = 80.0  # varna om färre än X% av universumet gick att beräkna
BATCH_PAUS_SEK = 3  # paus innan varje batch-nedladdning, för att undvika rate-limiting mot Yahoo


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
    # Kort paus innan varje batch-hämtning. Körs inifrån ThreadPoolExecutor,
    # vilket sprider ut anropen även när flera workers är aktiva samtidigt.
    time.sleep(BATCH_PAUS_SEK)
    try:
        # threads=False (tidigare True): med threads=True spawnar yfinance EGNA
        # interna parallella anrop per ticker INOM batchen, utöver den yttre
        # ThreadPoolExecutor-parallelliteten här. Med 500 tickers/batch x flera
        # samtidiga batchar blev den sammanlagda samtidiga belastningen mot
        # Yahoos API extremt hög, vilket är den sannolika orsaken till att även
        # extremt likvida tickers (NVDA, AMZN, AVGO) rapporterades som "failed"
        # (rate-limiting, inte faktisk avlistning).
        raw_data = yf.download(
            batch, period="1y", auto_adjust=True, group_by="ticker", progress=False, threads=False
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


def skriv_cache_till_fil(df, filnamn=CACHE_FIL):
    """Skriver cachen som JSON: {"TICKER": {"RS_Score": .., "RS_Rank": .., "Updated_At": ..}, ...}"""
    os.makedirs(os.path.dirname(filnamn) or ".", exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    cache = {}
    for _, r in df.iterrows():
        cache[r["Ticker"]] = {
            "RS_Score": round(float(r["RS_Score"]), 4),
            "RS_Rank": int(r["RS_Rank"]),
            "Updated_At": now,
        }

    with open(filnamn, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)

    return filnamn


def committa_cache_fil(filnamn=CACHE_FIL):
    """
    Committar och pushar cache-filen till repot. Körs bara när scriptet
    körs som sitt eget schemalagda/manuella workflow (RS Rating Update),
    INTE när det anropas som fallback inifrån dashboard_fetch.py (se
    ANROPAD_SOM_FALLBACK-flaggan i main).

    Kräver att workflowet har 'permissions: contents: write' och att
    checkout-steget använder standard GITHUB_TOKEN (actions/checkout@v4
    gör detta automatiskt).
    """
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", filnamn],
            capture_output=True, text=True, check=True
        )
        if not status.stdout.strip():
            print("  [git] inga ändringar i cache-filen, hoppar över commit.")
            return

        subprocess.run(["git", "config", "user.name", "rs-rating-bot"], check=True)
        subprocess.run(["git", "config", "user.email", "rs-rating-bot@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", filnamn], check=True)
        subprocess.run(
            ["git", "commit", "-m", f"chore: uppdatera RS_Cache ({datetime.now(timezone.utc).date()})"],
            check=True
        )
        subprocess.run(["git", "push"], check=True)
        print("  [git] cache-fil committad och pushad.")
    except subprocess.CalledProcessError as e:
        print(f"  [git] commit/push misslyckades: {e}")
        # Vi låter INTE detta faila hela körningen - cachen finns på disk
        # för den här körningen även om committen misslyckas.


def main(committa=True, target_ticker=None):
    """
    committa=False används av dashboard_fetch.py:s fallback-anrop -
    då ska bara filen skrivas lokalt, inte committas till repot (se plan: alternativ A).

    target_ticker: säkerställer att just DEN tickern som dashboard_fetch.py
    väntar på finns med i universumet, oavsett om den ligger inom de första
    3000 (se bygg_universum). Utan denna kunde t.ex. DELL falla utanför
    urvalet och fallback-körningen ändå inte ge ett svar för DELL.
    """
    print("Hämtar ticker->CIK-mappning...")
    r = hamta_med_retry("https://www.sec.gov/files/company_tickers.json", headers=HEADERS)
    if r is None:
        print("Kunde inte hämta ticker-mappning efter flera försök. Avbryter.")
        sys.exit(1)
    tm = r.json()

    print("Beräknar RS Rating för hela universumet (tar ett par minuter)...")
    df = berakna_rs_universum(tm, target_ticker=normalisera_ticker_for_yf(target_ticker) if target_ticker else None)
    if df.empty:
        print("Ingen RS-data kunde beräknas. Avbryter utan att skriva till cache.")
        sys.exit(1)

    print(f"Skriver {len(df)} rader till '{CACHE_FIL}'...")
    skriv_cache_till_fil(df)

    if committa:
        committa_cache_fil()
    else:
        print("  [git] committa=False (fallback-läge) - filen uppdaterad lokalt, ingen commit.")

    print("Klart.")


if __name__ == "__main__":
    main(committa=True, target_ticker=None)
