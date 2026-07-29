"""
weekly_news_report.py
======================
Automatiserad veckovis nyhetsrapport för Tradingbolag-systemet.

Flöde:
1. Läser aktiv portfölj (Ticker, Sektor, Bransch) från Google Sheet "Portfölj".
2. Läser marknadsregim-data från Google Sheet "Mall v2" (Marknadsdashboard-tabben).
3. Hämtar nyheter (gratis, ingen API-nyckel) via Google News RSS för:
   - varje innehav
   - respektive sektor/bransch
   - marknaden/makro i stort
   - "vad som väntar" nästa vecka
4. Hämtar kommande earnings-datum för innehaven via yfinance.
5. Skickar allt råmaterial till Gemini API (gratis tier) som skriver en
   strukturerad rapport på svenska i tre delar.
6. Postar rapporten till Telegram (delad i chunkar om >4096 tecken).

Miljövariabler (GitHub Secrets):
  GOOGLE_SERVICE_ACCOUNT_JSON  - service account JSON (samma som befintliga scripts)
  PORTFOLIO_SHEET_ID           - Sheet ID för "Portfölj"-filen
  SHEET_ID                     - Sheet ID för "Mall v2" (marknadsregim) - återanvänder befintlig secret
  GEMINI_API_KEY                - gratis nyckel från Google AI Studio
  TELEGRAM_BOT_TOKEN            - befintlig secret
  TELEGRAM_CHAT_ID              - befintlig secret
"""

import os
import re
import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
import yfinance as yf
import requests

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

GEMINI_MODEL = "gemini-3.5-flash"  # gratis tier (jul 2026). Uppdatera vid behov
                                     # om Google byter namn på flash-modellen -
                                     # se https://ai.google.dev/gemini-api/docs/models
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

NEWS_PER_QUERY = 5          # antal artiklar per RSS-sökning
MAX_TELEGRAM_CHARS = 4000   # Telegram-gräns är 4096, marginal för säkerhets skull

MARKET_QUERIES = [
    "stock market this week outlook",
    "Federal Reserve interest rates",
    "inflation report economy US",
    "geopolitical risk markets war",
]

NEXT_WEEK_QUERIES = [
    "stock market what to watch next week",
    "economic calendar this week US",
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def get_gspread_client():
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_portfolio(gc):
    """Läser Dashboard-tabben i Portfölj-sheeten och returnerar aktiva positioner.

    Strukturen har en titelrad och en rubrikrad ("Ticker,Bolag,Sektor...")
    innan de faktiska positionsraderna. Vi letar reda på rubrikraden dynamiskt
    och läser sedan rader tills Ticker-fältet är tomt.
    """
    sh = gc.open_by_key(os.environ["PORTFOLIO_SHEET_ID"])
    ws = sh.worksheet("Dashboard")
    values = ws.get_all_values()

    header_row_idx = None
    for i, row in enumerate(values):
        if row and row[0].strip() == "Ticker":
            header_row_idx = i
            break
    if header_row_idx is None:
        raise RuntimeError("Kunde inte hitta rubrikraden ('Ticker') i Dashboard-tabben.")

    header = values[header_row_idx]
    col = {name.strip(): idx for idx, name in enumerate(header) if name.strip()}

    positions = []
    for row in values[header_row_idx + 1:]:
        if not row or not row[0].strip():
            break  # tom rad = slut på positionslistan
        ticker = row[col.get("Ticker", 0)].strip()
        if not ticker:
            break
        positions.append({
            "ticker": ticker,
            "bolag": row[col.get("Bolag", 1)].strip() if col.get("Bolag") is not None else "",
            "sektor": row[col.get("Sektor (enl Tradingview)", 2)].strip() if col.get("Sektor (enl Tradingview)") is not None else "",
            "bransch": row[col.get("Bransch (enl Tradingview)", 3)].strip() if col.get("Bransch (enl Tradingview)") is not None else "",
        })
    return positions


def get_market_regime_dump(gc, max_rows=40):
    """Dumpar Marknadsdashboard-tabben i Mall v2 som ren text.

    Vi känner inte till exakt cellschema (det kan ändras), så vi skickar en
    rå text-dump till Gemini och låter modellen extrahera regim-status för
    SPX/NASDAQ samt relevanta poäng. Robust mot layoutändringar.
    """
    sh = gc.open_by_key(os.environ["SHEET_ID"])
    ws = sh.worksheet("Marknadsdashboard")
    values = ws.get_all_values()[:max_rows]
    lines = [",".join(cell for cell in row if cell) for row in values if any(row)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Nyhetsinsamling (Google News RSS - gratis, ingen nyckel)
# ---------------------------------------------------------------------------

def fetch_google_news(query, max_items=NEWS_PER_QUERY, days_back=7):
    """Hämtar senaste artiklar för en sökfråga via Google News RSS."""
    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        root = ET.fromstring(data)
        items = []
        cutoff = datetime.utcnow() - timedelta(days=days_back)
        for item in root.findall(".//item")[: max_items * 2]:
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            pub = item.findtext("pubDate", "").strip()
            source_el = item.find("source")
            source = source_el.text.strip() if source_el is not None and source_el.text else ""
            try:
                pub_dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z")
            except ValueError:
                pub_dt = None
            if pub_dt and pub_dt < cutoff:
                continue
            items.append({"title": title, "source": source, "link": link, "pubDate": pub})
            if len(items) >= max_items:
                break
        return items
    except Exception as e:
        print(f"[VARNING] Kunde inte hämta nyheter för '{query}': {e}")
        return []


def gather_all_news(positions):
    news = {
        "holdings": {},
        "sectors": {},
        "market": {},
        "next_week": {},
    }

    for pos in positions:
        ticker, bolag = pos["ticker"], pos["bolag"]
        query = f"{bolag} {ticker} stock"
        news["holdings"][ticker] = fetch_google_news(query)

    seen_sectors = set()
    for pos in positions:
        sektor, bransch = pos["sektor"], pos["bransch"]
        key = bransch or sektor
        if not key or key in seen_sectors:
            continue
        seen_sectors.add(key)
        news["sectors"][key] = fetch_google_news(f"{key} industry news")

    for q in MARKET_QUERIES:
        news["market"][q] = fetch_google_news(q)

    for q in NEXT_WEEK_QUERIES:
        news["next_week"][q] = fetch_google_news(q, days_back=3)

    return news


def get_earnings_dates(positions):
    """Hämtar kommande earnings-datum för innehaven via yfinance."""
    result = {}
    for pos in positions:
        ticker = pos["ticker"]
        try:
            t = yf.Ticker(ticker)
            cal = t.calendar
            earnings_date = None
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if ed:
                    earnings_date = str(ed[0]) if isinstance(ed, (list, tuple)) else str(ed)
            elif hasattr(cal, "empty") and not cal.empty:
                if "Earnings Date" in cal.index:
                    earnings_date = str(cal.loc["Earnings Date"].iloc[0])
            result[ticker] = earnings_date or "Ej tillgängligt"
        except Exception as e:
            result[ticker] = f"Kunde ej hämta ({e})"
    return result


# ---------------------------------------------------------------------------
# Gemini - sammanställning av rapport
# ---------------------------------------------------------------------------

def format_news_block(news_dict):
    lines = []
    for key, items in news_dict.items():
        lines.append(f"### {key}")
        if not items:
            lines.append("(inga artiklar hittades)")
        for it in items:
            lines.append(f"- {it['title']} ({it['source']}, {it['pubDate']})")
        lines.append("")
    return "\n".join(lines)


def build_prompt(positions, market_regime_dump, news, earnings_dates):
    holdings_str = ", ".join(f"{p['ticker']} ({p['bolag']}, {p['sektor']}/{p['bransch']})" for p in positions) or "Inga aktiva positioner"

    prompt = f"""Du är en senior finansjournalist och swing trading-analytiker (VCP/Minervini-metodik).
Skriv en veckovis nyhetsrapport på SVENSKA, ca 1.5-2 A4-sidor, i exakt denna struktur med tre rubriker:

## 1. Egna Innehav
Nyheter om följande bolag och deras respektive sektorer: {holdings_str}
Fokusera på: bolagsspecifika händelser, insider-transaktioner, analytikerrörelser, sektortrend.
Om inga artiklar finns för ett bolag, skriv det kort istället för att hitta på information.

## 2. Marknaden
Sammanfatta marknadsläget: makroekonomi, räntor, inflation, geopolitik/krig, stora bolagsrapporter som påverkar index.
Väv in nuvarande marknadsregim (Bullish/Choppig/Bearish för SPX och NASDAQ) baserat på rådatan nedan.

## 3. Inför Kommande Veckan
Lista kända katalysatorer: earnings-datum för innehaven (angivna nedan), makrodata, och annat relevant från "vad som väntar"-artiklarna.

VIKTIGT:
- Använd ENDAST information från källmaterialet nedan. Hitta inte på fakta.
- Ange källa (publikation) inline där relevant, kort format t.ex. (Reuters).
- Skriv koncist och strukturerat, punktlistor där det passar. Undvik upprepning.
- Ingen inledning/avslutning utanför de tre rubrikerna.

=== RÅDATA: MARKNADSREGIM (Mall v2, Marknadsdashboard) ===
{market_regime_dump}

=== RÅDATA: EARNINGS-DATUM INNEHAV ===
{json.dumps(earnings_dates, ensure_ascii=False, indent=2)}

=== RÅDATA: NYHETER PER INNEHAV ===
{format_news_block(news['holdings'])}

=== RÅDATA: NYHETER PER SEKTOR/BRANSCH ===
{format_news_block(news['sectors'])}

=== RÅDATA: MARKNADSNYHETER ===
{format_news_block(news['market'])}

=== RÅDATA: NÄSTA VECKA ===
{format_news_block(news['next_week'])}
"""
    return prompt


def call_gemini(prompt):
    api_key = os.environ["GEMINI_API_KEY"]
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    resp = requests.post(
        f"{GEMINI_URL}?key={api_key}",
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Oväntat Gemini-svar: {data}") from e


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Dela upp i chunkar under Telegrams gräns
    chunks = []
    while len(text) > MAX_TELEGRAM_CHARS:
        split_at = text.rfind("\n", 0, MAX_TELEGRAM_CHARS)
        if split_at == -1:
            split_at = MAX_TELEGRAM_CHARS
        chunks.append(text[:split_at])
        text = text[split_at:]
    chunks.append(text)

    for i, chunk in enumerate(chunks):
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }, timeout=30)
        if not resp.ok:
            print(f"[FEL] Telegram-sändning misslyckades (chunk {i}): {resp.text}")
        time.sleep(1)  # undvik rate limit


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Läser portfölj...")
    gc = get_gspread_client()
    positions = get_portfolio(gc)
    print(f"  Hittade {len(positions)} aktiva positioner: {[p['ticker'] for p in positions]}")

    print("Läser marknadsregim...")
    market_regime_dump = get_market_regime_dump(gc)

    print("Hämtar nyheter...")
    news = gather_all_news(positions)

    print("Hämtar earnings-datum...")
    earnings_dates = get_earnings_dates(positions)

    print("Bygger prompt och anropar Gemini...")
    prompt = build_prompt(positions, market_regime_dump, news, earnings_dates)
    report = call_gemini(prompt)

    header = f"📊 VECKORAPPORT — {datetime.now().strftime('%Y-%m-%d')}\n\n"
    full_report = header + report

    print("Skickar till Telegram...")
    send_telegram(full_report)
    print("Klart.")


if __name__ == "__main__":
    main()
