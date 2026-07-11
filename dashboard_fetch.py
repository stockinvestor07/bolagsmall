"""
dashboard_fetch.py
Automatiserad ifyllnad av bolagsdashboard i Google Sheets.
Körs via GitHub Actions (workflow_dispatch), tar TICKER som env-variabel.

Kvartalshistorik (Kvartal 1-8) hämtas via SEC EDGAR med durationsfilter
(79-103 dagar = garanterat diskreta kvartal, aldrig YTD-kumulativa poster).
Q4 härleds: Årsvärde (10-K) - Q1 - Q2 - Q3, endast för summerbara mått
(Revenue, Net Income, EPS). Diluted Shares är ett genomsnitt och kan inte
härledas via subtraktion - Q4 blir N/A där, övriga kvartal hämtas direkt.
IBD Company Rank, IBD Sector Rank, Trend Stage fylls INTE i (manuellt).

RS RATING: beräknas normalt INTE här. Hela universumet (~3000 bolag)
räknas om av det separata scriptet rs_rating_update.py, som körs
schemalagt måndag + torsdag och skriver resultatet till Sheets-fliken
"RS_Cache". Detta script läser bara upp cachat värde för aktuell ticker -
MEN om cachen saknas eller är äldre än RS_CACHE_MAX_ALDER_DAGAR (t.ex.
om det schemalagda jobbet missades) körs rs_rating_update.py som en
engångs-fallback här innan uppslaget görs, så dashboarden aldrig
tyst arbetar med kraftigt inaktuell RS-data.
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
from scipy.stats import percentileofscore
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = os.environ.get("SEC_USER_AGENT", "Anton Werner anton.js.werner@gmail.com")
HEADERS = {"User-Agent": USER_AGENT}
REQUEST_TIMEOUT = 15

EPS_TAGGAR = [
    "EarningsPerShareDiluted",
    "EarningsPerShareBasicAndDiluted",
    "EarningsPerShareBasic",
]
SALES_TAGGAR = [
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
]
NI_TAGG = "NetIncomeLoss"
DIL_SHARES_TAGG = "WeightedAverageNumberOfDilutedSharesOutstanding"

AVRUNDNING_TOLERANS_ABSOLUT = 0.01
AVRUNDNING_TOLERANS_RELATIV = 0.01

_frame_cache = {}

# ============================================================
# CELLMAPPNING - skalära fält
# ============================================================
CELL_MAP = {
    "ticker": "D1",
    "roe": "P4",
    "current_ratio": "P5",
    "soliditet": "P6",
    "float": "P7",
    "inst_agande": "P9",
    "short_percentage_of_float": "P10",
    "earnings_rank": "P12",
    "sales_rank": "P13",
    "rs_rating": "P16",
    "buy_risk": "P19",
    "sektor": "P21",
    "revenue_est_1": "L4",
    "revenue_est_2": "M4",
    "eps_est_1": "L8",
    "eps_est_2": "M8",
    "pct_diff_90d_eps": "L12",
    "pct_diff_90d_revenue": "N4",
}

KVARTAL_KOLUMNER = ["C", "D", "E", "F", "G", "H", "I", "J"]
ANTAL_KVARTAL = len(KVARTAL_KOLUMNER)
RAD_REVENUE = 4
RAD_REVENUE_TILLVAXT = 5
RAD_EPS = 8
RAD_EPS_TILLVAXT = 9
RAD_DIL_SHARES = 11
RAD_SURPRISE = 12
RAD_EARNINGS_RORELSE = 13
RAD_MARGINAL = 15

TOM_KVARTAL = {
    "revenue": "N/A", "revenue_tillvaxt": "N/A", "eps": "N/A",
    "eps_tillvaxt": "N/A", "dil_shares": "N/A", "marginal": "N/A",
}

SHEET_ID = os.environ["SHEET_ID"]
SHEET_TAB = "Dashboard"
RS_CACHE_TAB = "RS_Cache"
RS_CACHE_MAX_ALDER_DAGAR = 5  # måndag->torsdag = 3 dagar, torsdag->måndag = 4 dagar


def pad_left(lista, n, fyllvarde="N/A"):
    """Vänsterpaddar en lista till längd n. fyllvarde kan vara en callable
    (t.ex. lambda: dict(...)) om varje saknad post behöver vara ett eget objekt."""
    lista = list(lista[-n:])
    brist = n - len(lista)
    if brist <= 0:
        return lista
    fyllda = [fyllvarde() if callable(fyllvarde) else fyllvarde for _ in range(brist)]
    return fyllda + lista


def hamta_med_retry(url, headers=None, forsok=3, backoff=2):
    """Retry med exponentiell backoff + explicit timeout (saknades helt tidigare)."""
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
    key = (tagg, period, enhet)
    if key in _frame_cache:
        return _frame_cache[key]
    url = f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tagg}/{enhet}/{period}.json"
    resultat = {}
    r = hamta_med_retry(url, headers=HEADERS, forsok=2)
    if r is not None:
        try:
            resultat = {str(x["cik"]).zfill(10): x["val"] for x in r.json().get("data", [])}
        except Exception:
            pass
    _frame_cache[key] = resultat
    return resultat


def verifiera_kalenderkvartal_generic(cik_str, tagg, gissad_ar, gissad_q, eget_varde, enhet="USD"):
    kandidater = [(gissad_ar, gissad_q)]
    for delta in (-1, 1):
        q, ar = gissad_q + delta, gissad_ar
        if q == 0:
            q, ar = 4, ar - 1
        elif q == 5:
            q, ar = 1, ar + 1
        kandidater.append((ar, q))
    for ar, q in kandidater:
        frame = hamta_frame_cachad(tagg, f"CY{ar}Q{q}", enhet)
        if frame.get(cik_str) == eget_varde:
            return ar, q
    return None


# ============================================================
# RS RATING - läses från cache (se rs_rating_update.py)
# ============================================================
def _oppna_rs_cache_worksheet(readonly=True):
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"] if readonly else [
        "https://www.googleapis.com/auth/spreadsheets"
    ]
    creds_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).worksheet(RS_CACHE_TAB)


def _rs_cache_ar_farsk(max_alder_dagar):
    """
    Kontrollerar om RS_Cache finns och är färsk nog. Returnerar (är_farsk, rows).
    rows är None om cachen inte kunde läsas alls (nätverksfel etc) - då avstår
    vi från att gissa och kör ingen fallback-uppdatering.
    """
    try:
        ws = _oppna_rs_cache_worksheet(readonly=True)
        rows = ws.get_all_records()
    except gspread.WorksheetNotFound:
        return False, []
    except Exception as e:
        print(f"  [rs_rating] kunde inte kontrollera cache-status: {e}")
        return True, None  # osäkert läge - anta färsk, försök inte trigga ombyggnad blint

    if not rows:
        return False, []

    aldsta_uppdatering = None
    for row in rows:
        uppdaterad = row.get("Updated_At")
        try:
            alder = datetime.now(timezone.utc) - datetime.fromisoformat(uppdaterad)
            if aldsta_uppdatering is None or alder > aldsta_uppdatering:
                aldsta_uppdatering = alder
        except Exception:
            continue

    if aldsta_uppdatering is None:
        return False, rows  # kunde inte tolka Updated_At överhuvudtaget
    return aldsta_uppdatering.days <= max_alder_dagar, rows


def sakerstall_farsk_rs_cache(max_alder_dagar=RS_CACHE_MAX_ALDER_DAGAR):
    """
    Om cachen saknas eller är äldre än max_alder_dagar (t.ex. det schemalagda
    mån/tors-jobbet missades en vecka) körs rs_rating_update.py:s hela
    beräkning här som en engångs-fallback, istället för att bara logga en
    varning och fortsätta med inaktuell data. Normalfallet (cache färsk)
    kostar bara en läsning.
    """
    ar_farsk, rows = _rs_cache_ar_farsk(max_alder_dagar)
    if rows is None:
        return  # osäkert läge, se _rs_cache_ar_farsk
    if ar_farsk:
        return

    print(f"  [rs_rating] cache saknas eller är äldre än {max_alder_dagar} dagar - "
          f"kör rs_rating_update.py som fallback innan uppslag...")
    try:
        import rs_rating_update
        rs_rating_update.main()
    except SystemExit:
        pass  # rs_rating_update.main() kan anropa sys.exit(); låt inte det avbryta dashboard-körningen
    except Exception as e:
        print(f"  [rs_rating] fallback-körning av rs_rating_update.py misslyckades: {e}")


def hamta_rs_rating_fran_cache(ticker):
    """
    Läser förberäknat RS Rating ur fliken 'RS_Cache'. Den fliken fylls
    normalt av det separata scriptet rs_rating_update.py, schemalagt
    måndag + torsdag. Om cachen är för gammal körs den uppdateringen
    här istället (se sakerstall_farsk_rs_cache), så en enskild
    dashboard-körning aldrig arbetar med kraftigt inaktuell RS-data -
    men den normala vägen kräver bara en Sheets-läsning.
    """
    sakerstall_farsk_rs_cache()

    try:
        ws = _oppna_rs_cache_worksheet(readonly=True)
        rows = ws.get_all_records()
    except gspread.WorksheetNotFound:
        print(f"  [rs_rating] fliken '{RS_CACHE_TAB}' finns fortfarande inte - kunde inte läsa RS Rating.")
        return "N/A"
    except Exception as e:
        print(f"  [rs_rating] kunde inte läsa cache: {e}")
        return "N/A"

    for row in rows:
        if str(row.get("Ticker", "")).upper() == ticker.upper():
            uppdaterad = row.get("Updated_At")
            try:
                alder = datetime.now(timezone.utc) - datetime.fromisoformat(uppdaterad)
                if alder.days > RS_CACHE_MAX_ALDER_DAGAR:
                    print(f"  [rs_rating] VARNING: cache är fortfarande {alder.days} dagar gammal "
                          f"({uppdaterad}) trots fallback-försök.")
            except Exception:
                pass
            return row.get("RS_Rank", "N/A")

    print(f"  [rs_rating] {ticker} saknas i cache trots fallback-uppdatering.")
    return "N/A"


# ============================================================
# EARNINGS-RÖRELSE
# ============================================================
def _berakna_rorelse_5hd(hist, rapport_dt, is_bmo):
    if hist.empty: return None
    handelsdagar = hist.index.sort_values()
    rap_naive = pd.Timestamp(rapport_dt.date())

    dag0_kand = handelsdagar[handelsdagar < rap_naive] if is_bmo else handelsdagar[handelsdagar <= rap_naive]
    if len(dag0_kand) == 0: return None
    dag0 = dag0_kand[-1]

    dagar_efter = handelsdagar[handelsdagar > dag0]
    if len(dagar_efter) < 5: return None
    dag5 = dagar_efter[4]

    c0 = hist.loc[dag0, "Close"]
    c5 = hist.loc[dag5, "Close"]
    if not c0: return None
    return round((c5 - c0) / c0 * 100, 1)


def hamta_earnings_rorelse(ticker, t=None, antal=4, hist=None):
    """
    hist: valfri redan hämtad (tz-naiv) prishistorik som återanvänds istället
    för att göra ett eget nätverksanrop (se hamta_yfinance_data, som redan
    laddar ner ~1 års historik för buy_risk-beräkningen).
    """
    if t is None: t = yf.Ticker(ticker)
    try:
        ed = t.get_earnings_dates(limit=12)
    except Exception as e:
        print(f"  [earnings_rorelse] get_earnings_dates misslyckades: {e}")
        return ["N/A"] * antal

    if ed is None or ed.empty: return ["N/A"] * antal
    nu = pd.Timestamp.now(tz=ed.index.tz)
    forflutna = ed.index[ed.index < nu].sort_values()[-antal:]
    if len(forflutna) == 0: return ["N/A"] * antal

    behover_egen_hamtning = hist is None or hist.empty
    if not behover_egen_hamtning:
        min_behov = forflutna.min() - pd.Timedelta(days=20)
        if hist.index.min() > min_behov:
            behover_egen_hamtning = True  # den återanvända historiken räcker inte bakåt

    if behover_egen_hamtning:
        start = forflutna.min() - pd.Timedelta(days=20)
        slut = forflutna.max() + pd.Timedelta(days=20)
        try:
            hist = t.history(start=start, end=slut)
            hist.index = hist.index.tz_localize(None)
        except Exception as e:
            print(f"  [earnings_rorelse] history-hämtning misslyckades: {e}")
            return ["N/A"] * antal

    resultat = []
    for dt in forflutna:
        is_bmo = dt.hour < 12
        rorelse = _berakna_rorelse_5hd(hist, dt.to_pydatetime(), is_bmo)
        resultat.append(rorelse if rorelse is not None else "N/A")

    return pad_left(resultat, antal)


# ============================================================
# YFINANCE - skalära fält
# ============================================================
def hamta_yfinance_data(ticker):
    resultat = {}
    t = yf.Ticker(ticker)

    try: info = t.info
    except Exception: info = {}

    resultat["roe"] = _pct(info.get("returnOnEquity"))
    resultat["current_ratio"] = info.get("currentRatio", "N/A")

    float_shares = info.get("floatShares")
    resultat["float"] = round(float_shares / 1_000_000, 1) if float_shares else "N/A"

    resultat["inst_agande"] = _pct(info.get("heldPercentInstitutions"))
    resultat["sektor"] = info.get("sector", "N/A")
    resultat["short_percentage_of_float"] = _pct(info.get("shortPercentOfFloat"))

    # Hämta 1 års historik EN gång och återanvänd för både buy_risk (MA50)
    # och earnings-rörelse nedan, istället för två separata history()-anrop.
    hist_1y = pd.DataFrame()
    try:
        hist_1y = t.history(period="1y")
        hist_1y.index = hist_1y.index.tz_localize(None)
    except Exception:
        hist_1y = pd.DataFrame()

    try:
        pris = hist_1y["Close"].iloc[-1]
        ma50 = hist_1y["Close"].rolling(50).mean().iloc[-1]
        resultat["buy_risk"] = _pct((pris - ma50) / ma50)
    except Exception:
        resultat["buy_risk"] = "N/A"

    try:
        q_bs = t.quarterly_balance_sheet
        ek = q_bs.loc["Stockholders Equity"].iloc[0]
        tillgangar = q_bs.loc["Total Assets"].iloc[0]
        if pd.notna(ek) and pd.notna(tillgangar) and tillgangar != 0:
            resultat["soliditet"] = _pct(ek / tillgangar)
        else:
            resultat["soliditet"] = "N/A"
    except Exception:
        resultat["soliditet"] = "N/A"

    try:
        rev_est = t.get_revenue_estimate()
        resultat["revenue_est_1"] = round(rev_est.loc["0q", "avg"] / 1_000_000, 1) if "0q" in rev_est.index else "N/A"
        resultat["revenue_est_2"] = round(rev_est.loc["+1q", "avg"] / 1_000_000, 1) if "+1q" in rev_est.index else "N/A"
    except Exception:
        resultat["revenue_est_1"] = "N/A"
        resultat["revenue_est_2"] = "N/A"

    try:
        eps_est = t.get_earnings_estimate()
        resultat["eps_est_1"] = eps_est.loc["0q", "avg"] if "0q" in eps_est.index else "N/A"
        resultat["eps_est_2"] = eps_est.loc["+1q", "avg"] if "+1q" in eps_est.index else "N/A"
    except Exception:
        resultat["eps_est_1"] = "N/A"
        resultat["eps_est_2"] = "N/A"

    try:
        eps_trend = t.get_eps_trend()
        nu = eps_trend.loc["0q", "current"]
        for_90d = eps_trend.loc["0q", "90daysAgo"]
        resultat["pct_diff_90d_eps"] = _pct((nu - for_90d) / abs(for_90d)) if for_90d else "N/A"
    except Exception:
        resultat["pct_diff_90d_eps"] = "N/A"

    # Medvetet N/A: det finns ingen motsvarande 90-dagars-trend för revenue-estimat
    # i yfinance (till skillnad från EPS-trenden ovan). Inte en bugg, inte manuellt fält.
    resultat["pct_diff_90d_revenue"] = "N/A"

    try:
        ed = t.get_earnings_dates(limit=12)
        ed = ed.dropna(subset=["Surprise(%)"]).sort_index()
        resultat["_surprise_lista"] = ed["Surprise(%)"].tail(4).round(1).tolist()
    except Exception as e:
        print(f"Surprise-hämtning misslyckades: {e}")
        resultat["_surprise_lista"] = []

    resultat["_earnings_rorelse_lista"] = hamta_earnings_rorelse(
        ticker, t=t, antal=4, hist=hist_1y if not hist_1y.empty else None
    )

    return resultat


# ============================================================
# SEC EDGAR - historik & fallback
# ============================================================
def _bygg_serie_for_tagg(gaap, tagg, enhet):
    if tagg not in gaap: return {}
    poster = gaap[tagg]["units"].get(enhet, [])
    serie = {}
    for p in poster:
        form = p.get("form")
        start, end = p.get("start"), p.get("end")
        if form not in ("10-Q", "10-Q/A") or not start or not end: continue
        try: d = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
        except Exception: continue
        if not (79 <= d <= 103): continue
        if end not in serie or p.get("filed", "") > serie[end].get("filed", ""):
            serie[end] = {"val": p["val"], "filed": p.get("filed", "")}
    return serie


def _valj_basta_tagg_serie(gaap, taggar, enhet, faltnamn=""):
    """
    Tidigare bugg: vid lika slutdatum vann kandidaten med STÖRST numeriskt
    värde (x[1] i sorteringsnyckeln), inte den tagg som ligger högst i den
    avsiktliga prioritetsordningen i SALES_TAGGAR/EPS_TAGGAR. Det gav en
    godtycklig, inte en avsiktlig, tagg-vinnare.

    Fix: tie-break på taggens index i listan (lägre index = högre prioritet),
    inte på värdets storlek.
    """
    kandidater = []
    for prioritet, tagg in enumerate(taggar):
        serie = _bygg_serie_for_tagg(gaap, tagg, enhet)
        if serie:
            senaste_datum = max(serie.keys())
            senaste_varde = serie[senaste_datum]["val"]
            kandidater.append((senaste_datum, prioritet, senaste_varde, tagg, serie))

    if not kandidater: return {}, None
    # Sortering: senaste datum vinner, vid lika datum vinner lägst prioritetsindex
    kandidater.sort(key=lambda x: (x[0], -x[1]), reverse=True)
    vald_datum, _, vald_varde, vald_tagg, vald_serie = kandidater[0]

    if len(kandidater) > 1:
        ovriga = ", ".join(f"{k[3]} ({k[0]}={k[2]:,.0f})" for k in kandidater[1:])
        print(f"  [{faltnamn or 'tagg'}] Flera kandidater hittades. Vald: {vald_tagg} "
              f"({vald_datum}={vald_varde:,.0f}) [prioritetsordning]. Ej valda: {ovriga}")
    else:
        print(f"  [{faltnamn or 'tagg'}] Vald tagg: {vald_tagg} ({vald_datum}={vald_varde:,.0f})")
    return vald_serie, vald_tagg


def _arsvarde(gaap, tagg, enhet):
    poster = gaap.get(tagg, {}).get("units", {}).get(enhet, [])
    serie = {}
    for p in poster:
        form = p.get("form")
        start, end = p.get("start"), p.get("end")
        if form not in ("10-K", "10-K/A") or not start or not end: continue
        try: d = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
        except Exception: continue
        if not (350 <= d <= 380): continue
        if end not in serie or p.get("filed", "") > serie[end].get("filed", ""):
            serie[end] = {"val": p["val"], "filed": p.get("filed", "")}
    return serie


def _bygg_kvartalsserie(gaap, taggar, enhet, harled_q4, faltnamn=""):
    q_serie, tagg = _valj_basta_tagg_serie(gaap, taggar, enhet, faltnamn)
    if not q_serie: return {}
    resultat = dict(q_serie)
    if harled_q4 and tagg:
        ar_serie = _arsvarde(gaap, tagg, enhet)
        kvartal_datum = sorted(datetime.strptime(e, "%Y-%m-%d") for e in q_serie.keys())
        for fy_end_str, arsdata in ar_serie.items():
            fy_end = datetime.strptime(fy_end_str, "%Y-%m-%d")
            kandidater = sorted([d for d in kvartal_datum if 0 < (fy_end - d).days <= 300])[-3:]
            if len(kandidater) == 3:
                summa = sum(q_serie[d.strftime("%Y-%m-%d")]["val"] for d in kandidater)
                resultat[fy_end_str] = {"val": arsdata["val"] - summa, "filed": arsdata["filed"]}
    return resultat


def _hitta_yoy_varde(serie, slutdatum_str):
    slut_dt = datetime.strptime(slutdatum_str, "%Y-%m-%d")
    try: target = slut_dt.replace(year=slut_dt.year - 1)
    except ValueError: target = slut_dt.replace(year=slut_dt.year - 1, day=28)
    bast_key, bast_diff = None, 21
    for e in serie.keys():
        e_dt = datetime.strptime(e, "%Y-%m-%d")
        diff = abs((e_dt - target).days)
        if diff < bast_diff:
            bast_key, bast_diff = e, diff
    return serie[bast_key]["val"] if bast_key else None


def hamta_kvartalshistorik(gaap):
    rev_serie = _bygg_kvartalsserie(gaap, SALES_TAGGAR, "USD", harled_q4=True, faltnamn="revenue")
    ni_serie = _bygg_kvartalsserie(gaap, [NI_TAGG], "USD", harled_q4=True, faltnamn="net_income")
    eps_serie = _bygg_kvartalsserie(gaap, EPS_TAGGAR, "USD/shares", harled_q4=True, faltnamn="eps")
    dil_serie = _bygg_kvartalsserie(gaap, [DIL_SHARES_TAGG], "shares", harled_q4=False, faltnamn="dil_shares")

    if not rev_serie: return []
    sorterade = sorted(rev_serie.keys(), key=lambda e: datetime.strptime(e, "%Y-%m-%d"))
    senaste = sorterade[-ANTAL_KVARTAL:]

    resultat = []
    for slut in senaste:
        revenue = rev_serie.get(slut, {}).get("val")
        revenue_da = _hitta_yoy_varde(rev_serie, slut)
        eps = eps_serie.get(slut, {}).get("val")
        eps_da = _hitta_yoy_varde(eps_serie, slut)
        ni = ni_serie.get(slut, {}).get("val")
        dil = dil_serie.get(slut, {}).get("val")

        rev_tv = berakna_stabil_tillvaxt(revenue, revenue_da)
        eps_tv = berakna_stabil_tillvaxt(eps, eps_da)
        marginal = _pct(ni / revenue) if (ni is not None and revenue) else "N/A"

        resultat.append({
            "revenue": round(revenue / 1_000_000, 1) if revenue is not None else "N/A",
            "revenue_tillvaxt": _pct(rev_tv) if rev_tv is not None else "N/A",
            "eps": round(eps, 2) if eps is not None else "N/A",
            "eps_tillvaxt": _pct(eps_tv) if eps_tv is not None else "N/A",
            "dil_shares": round(dil / 1_000_000, 1) if dil is not None else "N/A",
            "marginal": marginal,
        })

    return pad_left(resultat, ANTAL_KVARTAL, fyllvarde=lambda: dict(TOM_KVARTAL))


def _senaste_varde(gaap_facts, tagg):
    if tagg not in gaap_facts: return None
    poster = gaap_facts[tagg]["units"].get("USD", [])
    giltiga = [p for p in poster if p.get("form") in ("10-Q", "10-K")]
    if not giltiga: return None
    giltiga.sort(key=lambda x: x.get("end", ""))
    return giltiga[-1]["val"]


def sec_edgar_data_och_ranks(ticker, cik_str, falt_saknas):
    """
    Tidigare låg ROE/current_ratio, earnings_rank, sales_rank och
    kvartalshistorik i EN gemensam try/except. Ett oväntat fel i t.ex.
    ROE-beräkningen gjorde att hela resten (inklusive den oberoende
    kvartalshistoriken) aldrig kördes. Nu har varje delsteg sitt eget
    try/except, så ett fel i en del inte dränker de andra.
    """
    resultat = {"earnings_rank": "N/A", "sales_rank": "N/A", "kvartalshistorik": []}

    facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_str}.json"
    r = hamta_med_retry(facts_url, headers=HEADERS)
    if r is None:
        print(f"  [sec_edgar] kunde inte hämta companyfacts för {ticker} efter flera försök.")
        return resultat

    try:
        facts = r.json()
    except Exception as e:
        print(f"  [sec_edgar] kunde inte tolka companyfacts-JSON: {e}")
        return resultat

    gaap = facts.get("facts", {}).get("us-gaap", {})

    if "roe" in falt_saknas:
        try:
            ni = _senaste_varde(gaap, "NetIncomeLoss")
            eq = _senaste_varde(gaap, "StockholdersEquity")
            resultat["roe"] = round((ni / eq) * 100, 1) if ni and eq else "N/A"
        except Exception as e:
            print(f"  [sec_edgar] ROE-beräkning misslyckades: {e}")
            resultat["roe"] = "N/A"

    if "current_ratio" in falt_saknas:
        try:
            ca = _senaste_varde(gaap, "AssetsCurrent")
            cl = _senaste_varde(gaap, "LiabilitiesCurrent")
            resultat["current_ratio"] = round(ca / cl, 2) if ca and cl else "N/A"
        except Exception as e:
            print(f"  [sec_edgar] current_ratio-beräkning misslyckades: {e}")
            resultat["current_ratio"] = "N/A"

    # --- EARNINGS RANK ---
    try:
        eps_serie_for_rank, eps_tagg = _valj_basta_tagg_serie(gaap, EPS_TAGGAR, "USD/shares", "earnings_rank")
        if eps_serie_for_rank and eps_tagg:
            senaste_datum = max(eps_serie_for_rank.keys())
            eps_nu = eps_serie_for_rank[senaste_datum]["val"]
            dt = datetime.strptime(senaste_datum, "%Y-%m-%d")
            gissad_ar, gissad_q = dt.year, (dt.month - 1) // 3 + 1

            period = verifiera_kalenderkvartal_generic(cik_str, eps_tagg, gissad_ar, gissad_q, eps_nu, "USD-per-shares")
            if period:
                ar, q = period
                nu_dict = hamta_frame_cachad(eps_tagg, f"CY{ar}Q{q}", "USD-per-shares")
                da_dict = hamta_frame_cachad(eps_tagg, f"CY{ar - 1}Q{q}", "USD-per-shares")
                universum = []
                for c, v_nu in nu_dict.items():
                    if c in da_dict:
                        tv = berakna_stabil_tillvaxt(v_nu, da_dict[c])
                        if tv is not None: universum.append(tv)
                target = berakna_stabil_tillvaxt(eps_nu, da_dict.get(cik_str))
                if target is not None:
                    if target not in universum: universum.append(target)
                    resultat["earnings_rank"] = round(percentileofscore(universum, target, kind="weak"))
    except Exception as e:
        print(f"  [sec_edgar] earnings_rank-beräkning misslyckades: {e}")

    # --- SALES RANK ---
    try:
        sales_serie_for_rank, sales_tagg = _valj_basta_tagg_serie(gaap, SALES_TAGGAR, "USD", "sales_rank")
        if sales_serie_for_rank and sales_tagg:
            slutdatum_str = max(sales_serie_for_rank.keys())
            kant_varde = sales_serie_for_rank[slutdatum_str]["val"]
            slutdatum = datetime.strptime(slutdatum_str, "%Y-%m-%d")
            period = verifiera_kalenderkvartal_generic(
                cik_str, sales_tagg, slutdatum.year, (slutdatum.month - 1) // 3 + 1, kant_varde, "USD"
            )
            if period:
                ar, q = period
                nu_dict = hamta_frame_cachad(sales_tagg, f"CY{ar}Q{q}", "USD")
                da_dict = hamta_frame_cachad(sales_tagg, f"CY{ar - 1}Q{q}", "USD")
                universum = {c: (nu_dict[c] - da_dict[c]) / abs(da_dict[c]) for c in nu_dict if c in da_dict and da_dict[c] != 0}
                if cik_str in universum:
                    resultat["sales_rank"] = round(percentileofscore(list(universum.values()), universum[cik_str], kind="weak"))
    except Exception as e:
        print(f"  [sec_edgar] sales_rank-beräkning misslyckades: {e}")

    # --- KVARTALSHISTORIK (oberoende av ovanstående) ---
    try:
        resultat["kvartalshistorik"] = hamta_kvartalshistorik(gaap)
    except Exception as e:
        print(f"  [sec_edgar] kvartalshistorik-hämtning misslyckades: {e}")
        resultat["kvartalshistorik"] = []

    return resultat


# ============================================================
# GOOGLE SHEETS
# ============================================================
def _konvertera(varde):
    if hasattr(varde, "item"): varde = varde.item()
    return varde


def _jamfor_varden(original, aterlast):
    if aterlast is None or str(aterlast).strip() == "": return "fel"
    original_str, aterlast_str = str(original).strip(), str(aterlast).strip()
    if original_str == aterlast_str: return "match"
    try:
        o = float(original_str)
        a = float(aterlast_str.replace(",", ""))
    except (ValueError, TypeError):
        return "fel"
    diff = abs(o - a)
    if diff < 1e-6: return "match"
    relativ_grans = abs(o) * AVRUNDNING_TOLERANS_RELATIV
    tolerans = max(AVRUNDNING_TOLERANS_ABSOLUT, relativ_grans)
    if diff <= tolerans: return "avrundning"
    return "fel"


def skriv_till_sheets(data, kvartalshistorik, surprise_lista, earnings_rorelse_lista):
    """
    Tidigare gjordes ett update_acell() + acell()-anrop PER fält (18 fält),
    plus 6 radskrivningar och 2 rangeskrivningar - 40+ separata API-anrop
    mot Google Sheets (kvot ~60 req/min/user). Nu batchas alla skrivningar
    till EN batch_update, och all verifieringsläsning till EN batch_get.
    """
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)

    batch_requests = []
    faltordning = []
    for falt, varde in data.items():
        cell = CELL_MAP.get(falt)
        if not cell: continue
        varde = _konvertera(varde)
        batch_requests.append({"range": cell, "values": [[varde]]})
        faltordning.append((falt, cell, varde))

    kol_start, kol_slut = KVARTAL_KOLUMNER[0], KVARTAL_KOLUMNER[-1]

    def rad_request(rad_nr, faltnamn):
        varden = [_konvertera(kv.get(faltnamn, "N/A")) for kv in kvartalshistorik]
        return {"range": f"{kol_start}{rad_nr}:{kol_slut}{rad_nr}", "values": [varden]}

    fel = []
    avrundningsvarningar = []

    if kvartalshistorik:
        for rad_nr, faltnamn in [
            (RAD_REVENUE, "revenue"), (RAD_REVENUE_TILLVAXT, "revenue_tillvaxt"),
            (RAD_EPS, "eps"), (RAD_EPS_TILLVAXT, "eps_tillvaxt"),
            (RAD_DIL_SHARES, "dil_shares"), (RAD_MARGINAL, "marginal"),
        ]:
            batch_requests.append(rad_request(rad_nr, faltnamn))
    else:
        fel.append("kvartalshistorik: ingen data hämtad från SEC EDGAR")

    kol4_start, kol4_slut = KVARTAL_KOLUMNER[-4], KVARTAL_KOLUMNER[-1]
    surprise_pad = pad_left(surprise_lista, 4)
    rorelse_pad = pad_left(earnings_rorelse_lista, 4)
    batch_requests.append({"range": f"{kol4_start}{RAD_SURPRISE}:{kol4_slut}{RAD_SURPRISE}", "values": [surprise_pad]})
    batch_requests.append({"range": f"{kol4_start}{RAD_EARNINGS_RORELSE}:{kol4_slut}{RAD_EARNINGS_RORELSE}", "values": [rorelse_pad]})

    ws.batch_update(batch_requests, value_input_option="USER_ENTERED")

    if faltordning:
        celler = [cell for _, cell, _ in faltordning]
        lasta = ws.batch_get(celler)
        for (falt, cell, varde), cellvarden in zip(faltordning, lasta):
            lastvarde = cellvarden[0][0] if cellvarden and cellvarden[0] else None
            status = _jamfor_varden(varde, lastvarde)
            if status == "avrundning":
                avrundningsvarningar.append(f"{falt} ({cell}): skrev '{varde}', läste tillbaka '{lastvarde}' (avrundning)")
            elif status == "fel":
                fel.append(f"{falt} ({cell}): skrev '{varde}', läste tillbaka '{lastvarde}'")

    return fel, avrundningsvarningar


def main():
    ticker = os.environ.get("TICKER", "").strip().upper()
    if not ticker:
        print("Ingen ticker angiven.")
        sys.exit(1)

    print(f"Hämtar data för {ticker}...")

    def _hamta_cik():
        r = hamta_med_retry("https://www.sec.gov/files/company_tickers.json", headers=HEADERS)
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

    # yfinance-data, RS-rating-lookup (cache) och CIK-lookup är oberoende
    # nätverksanrop - kör dem parallellt istället för i serie.
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_yf = executor.submit(hamta_yfinance_data, ticker)
        future_rs = executor.submit(hamta_rs_rating_fran_cache, ticker)
        future_cik = executor.submit(_hamta_cik)

        data = future_yf.result()
        rs_rating = future_rs.result()
        cik_str = future_cik.result()

    surprise_lista = data.pop("_surprise_lista", [])
    earnings_rorelse_lista = data.pop("_earnings_rorelse_lista", [])
    data["rs_rating"] = rs_rating

    kvartalshistorik = []
    if cik_str:
        saknas = [k for k in ("roe", "current_ratio") if data.get(k) == "N/A"]
        print(f"Hämtar Ranks, kvartalshistorik och saknade fält {saknas} från SEC EDGAR...")
        sec_data = sec_edgar_data_och_ranks(ticker, cik_str, saknas)
        kvartalshistorik = sec_data.pop("kvartalshistorik", [])
        data.update(sec_data)
    else:
        print(f"Varning: Kunde inte hitta CIK för {ticker}.")
        data["earnings_rank"] = "N/A"
        data["sales_rank"] = "N/A"

    data["ticker"] = ticker

    print("Skriver till Google Sheets...")
    fel, avrundningsvarningar = skriv_till_sheets(data, kvartalshistorik, surprise_lista, earnings_rorelse_lista)

    if avrundningsvarningar:
        print("INFO - avrundning vid Sheets-lagring (ej fel):")
        for v in avrundningsvarningar: print(f"  {v}")

    if fel:
        print("VARNING - verifieringsfel:")
        for f in fel: print(f"  {f}")
        sys.exit(1)

    print(f"Klart. {ticker} skrivet till bladet '{SHEET_TAB}'.")


if __name__ == "__main__":
    main()
