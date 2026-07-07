"""
dashboard_fetch.py
Automatiserad ifyllnad av bolagsdashboard i Google Sheets.
Körs via GitHub Actions (workflow_dispatch), tar TICKER som env-variabel.

Fält: ROE, Current Ratio, Soliditet, Float, % Inst. ägande, Short % of float,
Sektor, Buy Risk, Revenue/EPS-estimat + 90d-diff, Earnings Rank, Sales Rank,
samt kvartalshistorik (Kvartal 5-8): Revenue, EPS dil., Vinstmarginal,
Dil. Shares, % Tillväxt (YoY), % Surprise.
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

HEADERS = {"User-Agent": "Anton Werner anton.js.werner@gmail.com"}

EPS_PRIMARY_TAG = "EarningsPerShareDiluted"
EPS_FALLBACK_TAG = "EarningsPerShareBasicAndDiluted"
SALES_TAGGAR = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]
NI_TAGG = "NetIncomeLoss"
DIL_SHARES_TAGG = "WeightedAverageNumberOfDilutedSharesOutstanding"

_frame_cache = {}

# ============================================================
# CELLMAPPNING - skalära fält
# ============================================================
CELL_MAP = {
    "roe": "P4",
    "current_ratio": "P5",
    "soliditet": "P6",
    "float": "P7",
    "inst_agande": "P9",
    "short_percentage_of_float": "P10",
    "earnings_rank": "P12",
    "sales_rank": "P13",
    "buy_risk": "P19",
    "sektor": "P21",
    "revenue_est_1": "L4",
    "revenue_est_2": "M4",
    "eps_est_1": "L8",
    "eps_est_2": "M8",
    "pct_diff_90d_eps": "L12",
    "pct_diff_90d_revenue": "N4",
}

# Kvartalstabellen: kolumner G-J = Kvartal 5-8 (de 4 senast rapporterade)
KVARTAL_KOLUMNER = ["G", "H", "I", "J"]
RAD_REVENUE = 4
RAD_REVENUE_TILLVAXT = 5
RAD_EPS = 8
RAD_EPS_TILLVAXT = 9
RAD_DIL_SHARES = 11
RAD_SURPRISE = 12
RAD_MARGINAL = 14

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
# YFINANCE - skalära fält
# ============================================================
def hamta_yfinance_data(ticker):
    resultat = {}
    t = yf.Ticker(ticker)

    try:
        info = t.info
    except Exception:
        info = {}

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

    # % Surprise senaste 4 kvartal (yfinance earnings dates)
    try:
        ed = t.get_earnings_dates(limit=8)
        ed = ed.dropna(subset=["Surprise(%)"]).sort_index()
        resultat["_surprise_lista"] = ed["Surprise(%)"].tail(4).round(1).tolist()
    except Exception:
        resultat["_surprise_lista"] = []

    return resultat


# ============================================================
# SEC EDGAR - fallback-fält, Earnings Rank, Sales Rank, kvartalshistorik
# ============================================================
def _senaste_varde(gaap_facts, tagg):
    if tagg not in gaap_facts:
        return None
    poster = gaap_facts[tagg]["units"].get("USD", [])
    giltiga = [p for p in poster if p.get("form") in ("10-Q", "10-K")]
    if not giltiga:
        return None
    giltiga.sort(key=lambda x: x.get("end", ""))
    return giltiga[-1]["val"]


def _serie_per_fy_fp(gaap_facts, taggar, enhet):
    """Bygger {(fy, fp): {'val':.., 'filed':..}} från första taggen med data."""
    for tagg in taggar:
        if tagg not in gaap_facts:
            continue
        poster = gaap_facts[tagg]["units"].get(enhet, [])
        serie = {}
        for p in poster:
            fy, fp, form = p.get("fy"), p.get("fp"), p.get("form")
            if fy is None or fp is None or form not in ("10-Q", "10-Q/A", "10-K", "10-K/A"):
                continue
            key = (fy, fp)
            if key not in serie or p.get("filed", "") > serie[key].get("filed", ""):
                serie[key] = p
        if serie:
            return serie
    return {}


def _harled_kvartal(serie):
    """(fy,'Q1'..'Q3') direkt från filing. Q4 härleds: FY - Q1 - Q2 - Q3."""
    resultat = {}
    fy_set = {fy for fy, fp in serie.keys()}
    for fy in fy_set:
        q1, q2, q3 = serie.get((fy, "Q1")), serie.get((fy, "Q2")), serie.get((fy, "Q3"))
        fy_full = serie.get((fy, "FY"))
        if q1:
            resultat[(fy, "Q1")] = q1["val"]
        if q2:
            resultat[(fy, "Q2")] = q2["val"]
        if q3:
            resultat[(fy, "Q3")] = q3["val"]
        if fy_full and q1 and q2 and q3:
            resultat[(fy, "Q4")] = fy_full["val"] - q1["val"] - q2["val"] - q3["val"]
    return resultat


def hamta_kvartalshistorik(gaap):
    """Returnerar lista med de 4 senaste kvartalen (äldst->nyast), en dict per kvartal."""
    q_ordning = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}

    rev_serie = _serie_per_fy_fp(gaap, SALES_TAGGAR, "USD")
    rev_q = _harled_kvartal(rev_serie)

    ni_serie = _serie_per_fy_fp(gaap, [NI_TAGG], "USD")
    ni_q = _harled_kvartal(ni_serie)

    eps_serie = _serie_per_fy_fp(gaap, [EPS_PRIMARY_TAG, EPS_FALLBACK_TAG], "USD/shares")
    eps_q = _harled_kvartal(eps_serie)

    dil_serie = _serie_per_fy_fp(gaap, [DIL_SHARES_TAGG], "shares")
    dil_q = _harled_kvartal(dil_serie)

    if not rev_q:
        return []

    sorterade_nycklar = sorted(rev_q.keys(), key=lambda k: (k[0], q_ordning[k[1]]))
    senaste_4 = sorterade_nycklar[-4:]

    resultat = []
    for fy, q in senaste_4:
        foreg_key = (fy - 1, q)
        revenue = rev_q.get((fy, q))
        revenue_da = rev_q.get(foreg_key)
        eps = eps_q.get((fy, q))
        eps_da = eps_q.get(foreg_key)
        ni = ni_q.get((fy, q))
        dil_shares = dil_q.get((fy, q))

        rev_tillvaxt = berakna_stabil_tillvaxt(revenue, revenue_da)
        eps_tillvaxt = berakna_stabil_tillvaxt(eps, eps_da)
        marginal = _pct(ni / revenue) if (ni is not None and revenue) else "N/A"

        resultat.append({
            "revenue": round(revenue / 1_000_000, 1) if revenue is not None else "N/A",
            "revenue_tillvaxt": _pct(rev_tillvaxt) if rev_tillvaxt is not None else "N/A",
            "eps": round(eps, 2) if eps is not None else "N/A",
            "eps_tillvaxt": _pct(eps_tillvaxt) if eps_tillvaxt is not None else "N/A",
            "dil_shares": round(dil_shares / 1_000_000, 1) if dil_shares is not None else "N/A",
            "marginal": marginal,
        })

    return resultat


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
        eps_tagg = EPS_PRIMARY_TAG if EPS_PRIMARY_TAG in gaap else (EPS_FALLBACK_TAG if EPS_FALLBACK_TAG in gaap else None)
        if eps_tagg:
            eps_enheter = gaap[eps_tagg].get("units", {}).get("USD/shares", [])
            eps_giltiga = [x for x in eps_enheter if x.get("form") in ["10-Q", "10-Q/A", "10-K", "10-K/A"]]
            if eps_giltiga:
                eps_giltiga.sort(key=lambda x: x.get("filed", ""))
                senaste = eps_giltiga[-1]
                eps_nu = senaste["val"]
                dt = datetime.strptime(senaste["filed"], "%Y-%m-%d")
                gissad_ar = dt.year
                gissad_q = 4 if dt.month in [1, 2, 3] else (1 if dt.month in [4, 5, 6] else (2 if dt.month in [7, 8, 9] else 3))
                if dt.month in [1, 2, 3]:
                    gissad_ar -= 1

                period = verifiera_kalenderkvartal_generic(cik_str, eps_tagg, gissad_ar, gissad_q, eps_nu, "USD-per-shares")
                if period:
                    ar, q = period
                    nu_dict = hamta_frame_cachad(eps_tagg, f"CY{ar}Q{q}", "USD-per-shares")
                    da_dict = hamta_frame_cachad(eps_tagg, f"CY{ar-1}Q{q}", "USD-per-shares")
                    universum = []
                    for c, v_nu in nu_dict.items():
                        if c in da_dict:
                            tv = berakna_stabil_tillvaxt(v_nu, da_dict[c])
                            if tv is not None:
                                universum.append(tv)
                    target = berakna_stabil_tillvaxt(eps_nu, da_dict.get(cik_str))
                    if target is not None:
                        if target not in universum:
                            universum.append(target)
                        resultat["earnings_rank"] = round(percentileofscore(universum, target, kind="weak"))

        # --- SALES RANK ---
        sales_tagg, sales_kvartal = None, []
        for tg in SALES_TAGGAR:
            if tg not in gaap:
                continue
            kvartal = []
            for p in gaap[tg]["units"].get("USD", []):
                if p.get("form") not in ("10-Q", "10-Q/A"):
                    continue
                try:
                    d = (datetime.strptime(p["end"], "%Y-%m-%d") - datetime.strptime(p["start"], "%Y-%m-%d")).days
                    if 79 <= d <= 103:
                        kvartal.append((datetime.strptime(p["end"], "%Y-%m-%d"), p["val"]))
                except Exception:
                    continue
            if kvartal:
                sales_tagg, sales_kvartal = tg, kvartal
                break

        if sales_tagg and sales_kvartal:
            slutdatum, kant_varde = max(sales_kvartal, key=lambda x: x[0])
            period = verifiera_kalenderkvartal_generic(
                cik_str, sales_tagg, slutdatum.year, (slutdatum.month - 1) // 3 + 1, kant_varde, "USD"
            )
            if period:
                ar, q = period
                nu_dict = hamta_frame_cachad(sales_tagg, f"CY{ar}Q{q}", "USD")
                da_dict = hamta_frame_cachad(sales_tagg, f"CY{ar-1}Q{q}", "USD")
                universum = {c: (nu_dict[c] - da_dict[c]) / abs(da_dict[c]) for c in nu_dict if c in da_dict and da_dict[c] != 0}
                if cik_str in universum:
                    resultat["sales_rank"] = round(percentileofscore(list(universum.values()), universum[cik_str], kind="weak"))

        # --- KVARTALSHISTORIK ---
        resultat["kvartalshistorik"] = hamta_kvartalshistorik(gaap)

    except Exception as e:
        print(f"Fel vid SEC-exekvering: {e}")

    return resultat


# ============================================================
# GOOGLE SHEETS
# ============================================================
def _konvertera(varde):
    if hasattr(varde, "item"):
        varde = varde.item()
    return varde


def skriv_till_sheets(data, kvartalshistorik, surprise_lista):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)

    fel = []

    # Skalära fält
    for falt, varde in data.items():
        cell = CELL_MAP.get(falt)
        if not cell:
            continue
        varde = _konvertera(varde)
        ws.update_acell(cell, varde)
        lastvarde = ws.acell(cell).value
        if str(lastvarde) != str(varde):
            fel.append(f"{falt} ({cell}): skrev '{varde}', läste tillbaka '{lastvarde}'")

    # Kvartalstabell (batch-skrivning per rad, G:J)
    def skriv_rad(rad_nr, faltnamn):
        varden = [_konvertera(kv.get(faltnamn, "N/A")) for kv in kvartalshistorik]
        if len(varden) < 4:
            varden = ["N/A"] * (4 - len(varden)) + varden
        rng = f"G{rad_nr}:J{rad_nr}"
        ws.update(rng, [varden])

    if kvartalshistorik:
        skriv_rad(RAD_REVENUE, "revenue")
        skriv_rad(RAD_REVENUE_TILLVAXT, "revenue_tillvaxt")
        skriv_rad(RAD_EPS, "eps")
        skriv_rad(RAD_EPS_TILLVAXT, "eps_tillvaxt")
        skriv_rad(RAD_DIL_SHARES, "dil_shares")
        skriv_rad(RAD_MARGINAL, "marginal")
    else:
        fel.append("kvartalshistorik: ingen data hämtad från SEC EDGAR")

    # % Surprise (senaste 4, från yfinance)
    surprise_pad = surprise_lista[-4:]
    surprise_pad = ["N/A"] * (4 - len(surprise_pad)) + surprise_pad
    ws.update(f"G{RAD_SURPRISE}:J{RAD_SURPRISE}", [surprise_pad])

    return fel


def main():
    ticker = os.environ.get("TICKER", "").strip().upper()
    if not ticker:
        print("Ingen ticker angiven.")
        sys.exit(1)

    print(f"Hämtar data för {ticker}...")
    data = hamta_yfinance_data(ticker)
    surprise_lista = data.pop("_surprise_lista", [])

    cik_str = None
    try:
        tm = requests.get("https://www.sec.gov/files/company_tickers.json", headers=HEADERS).json()
        for v in tm.values():
            if v["ticker"] == ticker:
                cik_str = str(v["cik_str"]).zfill(10)
                break
    except Exception as e:
        print(f"Kunde inte ladda ticker-mappning: {e}")

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

    print("Skriver till Google Sheets...")
    fel = skriv_till_sheets(data, kvartalshistorik, surprise_lista)

    if fel:
        print("VARNING - verifieringsfel:")
        for f in fel:
            print(f"  {f}")
        sys.exit(1)

    print(f"Klart. {ticker} skrivet till bladet '{SHEET_TAB}'.")


if __name__ == "__main__":
    main()
