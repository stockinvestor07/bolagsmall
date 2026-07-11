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
"""

import os
import sys
import json
import requests
import yfinance as yf
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from scipy.stats import percentileofscore
from concurrent.futures import ThreadPoolExecutor

HEADERS = {"User-Agent": "Anton Werner anton.js.werner@gmail.com"}

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
    key = (tagg, period, enhet)
    if key in _frame_cache:
        return _frame_cache[key]
    url = f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tagg}/{enhet}/{period}.json"
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

def hamta_earnings_rorelse(ticker, t=None, antal=4):
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

    while len(resultat) < antal:
        resultat.insert(0, "N/A")
    return resultat

# ============================================================
# RS RATING (PARALLELL INHÄMTNING)
# ============================================================
def _bearbeta_rs_batch(batch):
    lokal_rs = []
    try:
        raw_data = yf.download(batch, period="1y", auto_adjust=True, group_by="ticker", progress=False)
        for ticker in batch:
            try:
                if ticker not in raw_data.columns.levels[0]: continue
                close_series = raw_data[ticker]["Close"].dropna().values
                if len(close_series) < 240: continue
                
                ret_3m = (close_series[-1] - close_series[-63]) / close_series[-63]
                ret_6m = (close_series[-1] - close_series[-126]) / close_series[-126]
                ret_9m = (close_series[-1] - close_series[-189]) / close_series[-189]
                ret_12m = (close_series[-1] - close_series[0]) / close_series[0]
                
                rs_score = (ret_3m * 2) + ret_6m + ret_9m + ret_12m
                lokal_rs.append({"Ticker": ticker, "RS_Score": rs_score})
            except:
                continue
    except:
        pass
    return lokal_rs

def hamta_rs_rating(target_ticker, sec_tickers_dict):
    tickers = [v["ticker"].replace("-", "").replace(".", "") for v in sec_tickers_dict.values() if v["ticker"].isalpha()]
    unika = []
    seen = set()
    for t in tickers:
        if t not in seen:
            seen.add(t)
            unika.append(t)
            
    universum = unika[:3000]
    
    # Garantera att analys-tickern är med oavsett market cap
    if target_ticker not in universum:
        universum[-1] = target_ticker
        
    totalt = len(universum)
    batch_storlek = 500
    batches = [universum[i:i + batch_storlek] for i in range(0, totalt, batch_storlek)]
    
    master_rs_data = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        resultat_listor = executor.map(_bearbeta_rs_batch, batches)
        for lista in resultat_listor:
            master_rs_data.extend(lista)

    df = pd.DataFrame(master_rs_data)
    if df.empty or "RS_Score" not in df.columns:
        return "N/A"
    
    raw_scores = df["RS_Score"].values
    df["RS_Rating"] = df["RS_Score"].apply(lambda x: int(percentileofscore(raw_scores, x, kind="weak"))).clip(1, 99)
    
    traff = df[df["Ticker"] == target_ticker]
    if not traff.empty:
        return traff["RS_Rating"].values[0]
    return "N/A"

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

    try:
        hist = t.history(period="3mo")
        pris = hist["Close"].iloc[-1]
        ma50 = hist["Close"].rolling(50).mean().iloc[-1]
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

    resultat["pct_diff_90d_revenue"] = "N/A"

    try:
        ed = t.get_earnings_dates(limit=12)
        ed = ed.dropna(subset=["Surprise(%)"]).sort_index()
        resultat["_surprise_lista"] = ed["Surprise(%)"].tail(4).round(1).tolist()
    except Exception as e:
        print(f"Surprise-hämtning misslyckades: {e}")
        resultat["_surprise_lista"] = []

    resultat["_earnings_rorelse_lista"] = hamta_earnings_rorelse(ticker, t=t, antal=4)

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
    kandidater = []
    for tagg in taggar:
        serie = _bygg_serie_for_tagg(gaap, tagg, enhet)
        if serie:
            senaste_datum = max(serie.keys())
            senaste_varde = serie[senaste_datum]["val"]
            kandidater.append((senaste_datum, senaste_varde, tagg, serie))

    if not kandidater: return {}, None
    kandidater.sort(key=lambda x: (x[0], x[1]), reverse=True)
    vald_datum, vald_varde, vald_tagg, vald_serie = kandidater[0]

    if len(kandidater) > 1:
        ovriga = ", ".join(f"{k[2]} ({k[0]}={k[1]:,.0f})" for k in kandidater[1:])
        print(f"  [{faltnamn or 'tagg'}] Flera kandidater hittades. Vald: {vald_tagg} ({vald_datum}={vald_varde:,.0f}). Ej valda: {ovriga}")
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

    while len(resultat) < ANTAL_KVARTAL:
        resultat.insert(0, {"revenue": "N/A", "revenue_tillvaxt": "N/A", "eps": "N/A", "eps_tillvaxt": "N/A", "dil_shares": "N/A", "marginal": "N/A"})
    return resultat

def _senaste_varde(gaap_facts, tagg):
    if tagg not in gaap_facts: return None
    poster = gaap_facts[tagg]["units"].get("USD", [])
    giltiga = [p for p in poster if p.get("form") in ("10-Q", "10-K")]
    if not giltiga: return None
    giltiga.sort(key=lambda x: x.get("end", ""))
    return giltiga[-1]["val"]

def sec_edgar_data_och_ranks(ticker, cik_str, falt_saknas):
    resultat = {"earnings_rank": "N/A", "sales_rank": "N/A", "kvartalshistorik": []}
    try:
        facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_str}.json"
        facts = requests.get(facts_url, headers=HEADERS).json()
        gaap = facts.get("facts", {}).get("us-gaap", {})

        if "roe" in falt_saknas:
            ni = _senaste_varde(gaap, "NetIncomeLoss")
            eq = _senaste_varde(gaap, "StockholdersEquity")
            resultat["roe"] = round((ni / eq) * 100, 1) if ni and eq else "N/A"

        if "current_ratio" in falt_saknas:
            ca = _senaste_varde(gaap, "AssetsCurrent")
            cl = _senaste_varde(gaap, "LiabilitiesCurrent")
            resultat["current_ratio"] = round(ca / cl, 2) if ca and cl else "N/A"

        # --- EARNINGS RANK ---
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
                da_dict = hamta_frame_cachad(eps_tagg, f"CY{ar-1}Q{q}", "USD-per-shares")
                universum = []
                for c, v_nu in nu_dict.items():
                    if c in da_dict:
                        tv = berakna_stabil_tillvaxt(v_nu, da_dict[c])
                        if tv is not None: universum.append(tv)
                target = berakna_stabil_tillvaxt(eps_nu, da_dict.get(cik_str))
                if target is not None:
                    if target not in universum: universum.append(target)
                    resultat["earnings_rank"] = round(percentileofscore(universum, target, kind="weak"))

        # --- SALES RANK ---
        sales_serie_for_rank, sales_tagg = _valj_basta_tagg_serie(gaap, SALES_TAGGAR, "USD", "sales_rank")
        if sales_serie_for_rank and sales_tagg:
            slutdatum_str = max(sales_serie_for_rank.keys())
            kant_varde = sales_serie_for_rank[slutdatum_str]["val"]
            slutdatum = datetime.strptime(slutdatum_str, "%Y-%m-%d")
            period = verifiera_kalenderkvartal_generic(cik_str, sales_tagg, slutdatum.year, (slutdatum.month - 1) // 3 + 1, kant_varde, "USD")
            if period:
                ar, q = period
                nu_dict = hamta_frame_cachad(sales_tagg, f"CY{ar}Q{q}", "USD")
                da_dict = hamta_frame_cachad(sales_tagg, f"CY{ar-1}Q{q}", "USD")
                universum = {c: (nu_dict[c] - da_dict[c]) / abs(da_dict[c]) for c in nu_dict if c in da_dict and da_dict[c] != 0}
                if cik_str in universum:
                    resultat["sales_rank"] = round(percentileofscore(list(universum.values()), universum[cik_str], kind="weak"))

        resultat["kvartalshistorik"] = hamta_kvartalshistorik(gaap)
    except Exception as e:
        print(f"Fel vid SEC-exekvering: {e}")
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
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)

    fel = []
    avrundningsvarningar = []

    for falt, varde in data.items():
        cell = CELL_MAP.get(falt)
        if not cell: continue
        varde = _konvertera(varde)
        ws.update_acell(cell, varde)
        lastvarde = ws.acell(cell).value
        status = _jamfor_varden(varde, lastvarde)
        if status == "avrundning":
            avrundningsvarningar.append(f"{falt} ({cell}): skrev '{varde}', läste tillbaka '{lastvarde}' (avrundning)")
        elif status == "fel":
            fel.append(f"{falt} ({cell}): skrev '{varde}', läste tillbaka '{lastvarde}'")

    kol_start, kol_slut = KVARTAL_KOLUMNER[0], KVARTAL_KOLUMNER[-1]
    def skriv_rad(rad_nr, faltnamn):
        varden = [_konvertera(kv.get(faltnamn, "N/A")) for kv in kvartalshistorik]
        ws.update(range_name=f"{kol_start}{rad_nr}:{kol_slut}{rad_nr}", values=[varden])

    if kvartalshistorik:
        skriv_rad(RAD_REVENUE, "revenue")
        skriv_rad(RAD_REVENUE_TILLVAXT, "revenue_tillvaxt")
        skriv_rad(RAD_EPS, "eps")
        skriv_rad(RAD_EPS_TILLVAXT, "eps_tillvaxt")
        skriv_rad(RAD_DIL_SHARES, "dil_shares")
        skriv_rad(RAD_MARGINAL, "marginal")
    else:
        fel.append("kvartalshistorik: ingen data hämtad från SEC EDGAR")

    kol4_start, kol4_slut = KVARTAL_KOLUMNER[-4], KVARTAL_KOLUMNER[-1]
    surprise_pad = surprise_lista[-4:]
    surprise_pad = ["N/A"] * (4 - len(surprise_pad)) + surprise_pad
    ws.update(range_name=f"{kol4_start}{RAD_SURPRISE}:{kol4_slut}{RAD_SURPRISE}", values=[surprise_pad])

    rorelse_pad = earnings_rorelse_lista[-4:]
    rorelse_pad = ["N/A"] * (4 - len(rorelse_pad)) + rorelse_pad
    ws.update(range_name=f"{kol4_start}{RAD_EARNINGS_RORELSE}:{kol4_slut}{RAD_EARNINGS_RORELSE}", values=[rorelse_pad])

    return fel, avrundningsvarningar

def main():
    ticker = os.environ.get("TICKER", "").strip().upper()
    if not ticker:
        print("Ingen ticker angiven.")
        sys.exit(1)

    print(f"Hämtar data för {ticker}...")
    data = hamta_yfinance_data(ticker)
    surprise_lista = data.pop("_surprise_lista", [])
    earnings_rorelse_lista = data.pop("_earnings_rorelse_lista", [])

    cik_str = None
    tm = {}
    try:
        tm = requests.get("https://www.sec.gov/files/company_tickers.json", headers=HEADERS).json()
        for v in tm.values():
            if v["ticker"] == ticker:
                cik_str = str(v["cik_str"]).zfill(10)
                break
    except Exception as e:
        print(f"Kunde inte ladda ticker-mappning: {e}")

    # --- INJEKTERAD RS RATING ---
    print("Kör parallell mass-inhämtning för RS Rating (tar ~60 sekunder)...")
    data["rs_rating"] = hamta_rs_rating(ticker, tm) if tm else "N/A"

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
