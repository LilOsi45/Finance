#!/usr/bin/env python3
"""Täglicher privater Finanz- & Nachrichten-Digest.

Ablauf:
  1. RSS-Feeds laden (letzte Stunden), pro Reiter gruppieren, deduplizieren.
  2. Markt-Snapshot + erweitertes Markt-Board laden (Indizes, Währungen,
     Rohstoffe, Krypto – kostenlos via Stooq, kein API-Key nötig).
  3. (Optional) Claude fasst pro Reiter zusammen. Standard ist OHNE KI:
     saubere Aggregation der wichtigsten Schlagzeilen + Originalteaser.
  4. Ein eigenständiges HTML-Dashboard nach docs/ rendern (inkl. Tagesarchiv).

Aufruf:
  python scripts/build_digest.py          # Standard: ohne KI, nur Schlagzeilen
  python scripts/build_digest.py --ai     # mit Claude-Zusammenfassung
                                          # (braucht ANTHROPIC_API_KEY)

Konfiguration: siehe Block "KONFIGURATION" – Quellen, Reiter, Markt-Symbole,
Modell und Zeitfenster lassen sich dort leicht anpassen.
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import feedparser
import requests

# ---------------------------------------------------------------------------
# KONFIGURATION  – hier kannst du gefahrlos Quellen/Reiter/Symbole ändern.
# ---------------------------------------------------------------------------

# Nur Beiträge der letzten N Stunden aufnehmen.
TIME_WINDOW_HOURS = 48

# Höchstzahl Einträge, die pro Reiter angezeigt (bzw. an Claude übergeben) werden.
MAX_ITEMS_PER_TAB = 16

# Modell für die OPTIONALE KI-Zusammenfassung (nur mit --ai relevant).
# Default: bestes Modell. Günstiger: "claude-sonnet-4-6" / "claude-haiku-4-5".
MODEL = "claude-opus-4-8"

# Zeitzone für Datum/Uhrzeit in der Anzeige.
TZ = ZoneInfo("Europe/Berlin")

# Der KI-Experten-Kommentar wird nur zu diesen Uhrzeiten (Berlin) neu erzeugt
# (spart Kosten) und sonst aus dem Zwischenspeicher weiterverwendet.
AI_HOURS = {7, 13}


def _gnews(query: str) -> str:
    """Google-News-Such-Feed (zuverlässig, sprachlich filterbar)."""
    return "https://news.google.com/rss/search?q=" + quote(query) + "&hl=de&gl=DE&ceid=DE:de"


# Persönliche Aktien-Watchlist: Yahoo-Finance-Symbol -> Anzeigename.
# US-Aktien: reines Kürzel (AAPL). Deutsche (Xetra): Endung ".DE".
WATCHLIST: dict[str, str] = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "Nvidia",
    "AMZN": "Amazon",
    "GOOGL": "Alphabet",
    "META": "Meta",
    "TSLA": "Tesla",
    "SAP.DE": "SAP",
    "SIE.DE": "Siemens",
    "ALV.DE": "Allianz",
    "MBG.DE": "Mercedes-Benz",
    "VOW3.DE": "Volkswagen",
    "DBK.DE": "Deutsche Bank",
}


# Zuordnung Yahoo-Symbol -> TradingView (Börse:Kürzel) für professionelle Charts.
TRADINGVIEW: dict[str, str] = {
    "AAPL": "NASDAQ:AAPL",
    "MSFT": "NASDAQ:MSFT",
    "NVDA": "NASDAQ:NVDA",
    "AMZN": "NASDAQ:AMZN",
    "GOOGL": "NASDAQ:GOOGL",
    "META": "NASDAQ:META",
    "TSLA": "NASDAQ:TSLA",
    "SAP.DE": "XETR:SAP",
    "SIE.DE": "XETR:SIE",
    "ALV.DE": "XETR:ALV",
    "MBG.DE": "XETR:MBG",
    "VOW3.DE": "XETR:VOW3",
    "DBK.DE": "XETR:DBK",
}


def _watchlist_query() -> str:
    names = " OR ".join(f'"{n}"' for n in WATCHLIST.values())
    return _gnews(f"({names}) Aktie when:2d")


# Reiter und ihre RSS-Quellen. Reihenfolge = Reiter-Reihenfolge.
# Der Reiter "extras" zeigt zusätzlich oben das Markt-Board (s. MARKET_BOARD).
TABS: dict[str, dict] = {
    "watchlist": {
        "title": "Meine Watchlist",
        "feeds": [_watchlist_query()],
    },
    "maerkte": {
        "title": "Aktien & Märkte",
        "feeds": [
            "https://www.tagesschau.de/wirtschaft/index~rss2.xml",
            "https://www.handelsblatt.com/contentexport/feed/finanzen",
            "https://finance.yahoo.com/news/rssindex",
            "http://feeds.marketwatch.com/marketwatch/topstories/",
            "https://www.ft.com/markets?format=rss",
            _gnews("site:ft.com markets OR stocks OR economy when:2d"),
            _gnews("DAX OR S&P 500 OR Nasdaq Aktien Börse when:2d"),
        ],
    },
    "wirtschaft": {
        "title": "International Management & Wirtschaft",
        "feeds": [
            "https://www.handelsblatt.com/contentexport/feed/unternehmen",
            "https://www.nzz.ch/wirtschaft.rss",
            "https://www.ft.com/companies?format=rss",
            _gnews("Konzern OR Übernahme OR M&A OR CEO Strategie when:2d"),
            _gnews("global economy OR world trade OR central bank when:2d"),
        ],
    },
    "welt": {
        "title": "Weltnachrichten",
        "feeds": [
            "https://www.tagesschau.de/ausland/index~rss2.xml",
            "https://www.ft.com/world?format=rss",
            _gnews("Geopolitik OR Konflikt OR Wahl Weltpolitik when:2d"),
        ],
    },
    "extras": {
        "title": "Markt-Board & Krypto",
        "feeds": [
            _gnews("Wirtschaftsdaten OR Konjunktur OR Quartalszahlen when:1d"),
            "https://cointelegraph.com/rss",
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
        ],
    },
}

# Markt-Ticker oben (kompakt). Yahoo-Finance-Symbol -> Anzeigename.
TICKER_SYMBOLS: dict[str, str] = {
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq",
    "^DJI": "Dow Jones",
    "^GDAXI": "DAX",
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
}

# Erweitertes Markt-Board (im Extras-Reiter), gruppiert.
MARKET_BOARD: dict[str, dict[str, str]] = {
    "Indizes": {
        "^GSPC": "S&P 500",
        "^GDAXI": "DAX",
        "^IXIC": "Nasdaq",
        "^DJI": "Dow Jones",
        "^FTSE": "FTSE 100",
        "^N225": "Nikkei 225",
    },
    "Währungen": {
        "EURUSD=X": "EUR/USD",
        "EURCHF=X": "EUR/CHF",
        "USDJPY=X": "USD/JPY",
    },
    "Rohstoffe": {
        "GC=F": "Gold",
        "CL=F": "Öl (WTI)",
    },
    "Krypto": {
        "BTC-USD": "Bitcoin",
        "ETH-USD": "Ethereum",
    },
}

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FinanceDigest/1.0)"}
HTTP_TIMEOUT = 20


# ---------------------------------------------------------------------------
# 1. RSS laden
# ---------------------------------------------------------------------------

def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
    return None


def _strip_html(text: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _domain(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc.replace("www.", "")


def _safe_url(url: str) -> str:
    """Nur http/https zulassen. Verhindert javascript:/data:-Links aus Feeds
    (XSS-Schutz). Unsichere/leere URLs ergeben einen leeren String."""
    from urllib.parse import urlparse
    try:
        if urlparse(url).scheme.lower() in ("http", "https"):
            return url
    except Exception:
        pass
    return ""


def _entry_image(entry) -> str:
    """Bestes Vorschaubild eines Feed-Eintrags finden (https), sonst ""."""
    cands: list[str] = []
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key)
        if media:
            cands.append((media[0] or {}).get("url", ""))
    for enc in entry.get("enclosures", []) or []:
        if "image" in (enc.get("type", "")) or enc.get("href", "").lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp")
        ):
            cands.append(enc.get("href", ""))
    blob = entry.get("summary", "")
    for c in entry.get("content", []) or []:
        blob += c.get("value", "")
    import re
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', blob)
    if m:
        cands.append(m.group(1))
    for u in cands:
        if u and u.startswith("https://"):  # http würde auf https-Seite blockiert
            return u
    return ""


def _favicon(entry, link: str) -> str:
    """Logo/Favicon der echten Quelle (Google News liefert die Publisher-URL)."""
    src = entry.get("source")
    href = src.get("href", "") if isinstance(src, dict) else ""
    dom = _domain(href) if href else _domain(link)
    return f"https://www.google.com/s2/favicons?sz=64&domain={dom}" if dom else ""


def fetch_feed(url: str) -> list[dict]:
    """Lädt einen Feed robust; gibt bei Fehler eine leere Liste zurück."""
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:  # Quelle ausgefallen -> Rest läuft weiter.
        print(f"  ! Feed-Fehler {url}: {exc}", file=sys.stderr)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=TIME_WINDOW_HOURS)
    feed_title = parsed.feed.get("title", "") if parsed.feed else ""
    # Google-News-Feeds liefern als Feed-Titel die Suchanfrage – unbrauchbar.
    if len(feed_title) > 40:
        feed_title = ""
    items = []
    for e in parsed.entries:
        ts = _entry_time(e)
        if ts and ts < cutoff:
            continue
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        # Echter Verlag: Google News liefert ihn pro Eintrag in e.source.title.
        src = e.get("source")
        publisher = src.get("title", "") if isinstance(src, dict) else ""
        source_name = publisher or feed_title or _domain(link)
        summary = _strip_html(e.get("summary") or "")[:400]
        items.append(
            {
                "headline": title,
                "url": link,
                "source": source_name,
                "snippet": summary,
                "image": _entry_image(e),
                "favicon": _favicon(e, link),
                "ts": ts.isoformat() if ts else "",
            }
        )
    return items


def gather_news() -> dict[str, list[dict]]:
    """Lädt alle Reiter, dedupliziert nach Titel, begrenzt die Anzahl."""
    result: dict[str, list[dict]] = {}
    for tab_id, cfg in TABS.items():
        print(f"- Lade Reiter '{cfg['title']}' …")
        collected: list[dict] = []
        seen: set[str] = set()
        for feed_url in cfg["feeds"]:
            for item in fetch_feed(feed_url):
                key = item["headline"].lower()[:80]
                if key in seen:
                    continue
                seen.add(key)
                collected.append(item)
        collected.sort(key=lambda x: x["ts"], reverse=True)
        result[tab_id] = collected[:MAX_ITEMS_PER_TAB]
        print(f"    {len(result[tab_id])} Einträge")
    return result


# ---------------------------------------------------------------------------
# 2. Marktdaten (Stooq, ohne API-Key)
# ---------------------------------------------------------------------------

def fetch_quote(symbol: str) -> dict | None:
    """Aktueller Kurs + Tagesveränderung in % via Yahoo-Finance (kein API-Key).
    Funktioniert zuverlässig von Servern und liefert nahezu Echtzeitkurse."""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + quote(symbol)
        + "?range=1mo&interval=1d"
    )
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        res = resp.json()["chart"]["result"][0]
        meta = res.get("meta", {})
        try:
            hist = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        except Exception:
            hist = []
        # Bevorzugt: aktueller Kurs + Vortagesschluss aus den Metadaten.
        last = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if last is None or prev is None:
            # Fallback: letzte zwei Tagesschlüsse aus der Zeitreihe.
            if len(hist) < 2:
                return None
            last, prev = hist[-1], hist[-2]
        pct = (last - prev) / prev * 100 if prev else 0.0
        return {"last": float(last), "pct": float(pct), "hist": [float(c) for c in hist]}
    except Exception as exc:
        print(f"  ! Kurs-Fehler {symbol}: {exc}", file=sys.stderr)
        return None


def gather_ticker() -> list[dict]:
    print("- Lade Markt-Ticker …")
    out = []
    for sym, name in TICKER_SYMBOLS.items():
        q = fetch_quote(sym)
        if q:
            out.append({"name": name, "last": q["last"], "pct": q["pct"]})
    print(f"    {len(out)} Kurse")
    return out


def gather_board() -> dict[str, list[dict]]:
    print("- Lade Markt-Board …")
    board: dict[str, list[dict]] = {}
    for group, syms in MARKET_BOARD.items():
        rows = []
        for sym, name in syms.items():
            q = fetch_quote(sym)
            if q:
                rows.append({"name": name, "last": q["last"], "pct": q["pct"]})
        board[group] = rows
    n = sum(len(v) for v in board.values())
    print(f"    {n} Kurse")
    return board


def gather_watchlist() -> list[dict]:
    print("- Lade Watchlist-Kurse …")
    out = []
    for sym, name in WATCHLIST.items():
        q = fetch_quote(sym)
        if not q:
            continue
        hist = q.get("hist", [])
        last = q["last"]

        def perf(days: int) -> float | None:
            if len(hist) > days and hist[-1 - days]:
                return (last / hist[-1 - days] - 1) * 100
            return None

        trend = "–"
        if len(hist) >= 5:
            sma = sum(hist) / len(hist)
            if last > sma * 1.01:
                trend = "Aufwärts"
            elif last < sma * 0.99:
                trend = "Abwärts"
            else:
                trend = "Seitwärts"
        out.append(
            {
                "name": name,
                "symbol": sym,
                "last": last,
                "pct": q["pct"],
                "p5": perf(5),    # ca. 1 Woche
                "p20": perf(20),  # ca. 1 Monat
                "trend": trend,
            }
        )
    print(f"    {len(out)} Kurse")
    return out


def _quote_link(symbol: str) -> str:
    """Link auf die TradingView-Kursseite (professioneller Live-Chart)."""
    tv = TRADINGVIEW.get(symbol)
    if tv:
        return f"https://www.tradingview.com/symbols/{tv.replace(':', '-')}/"
    return f"https://www.tradingview.com/symbols/{quote(symbol)}/" if symbol else "#"


def fmt_price(v: float) -> str:
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 1:
        return f"{v:,.2f}"
    return f"{v:.4f}"


# ---------------------------------------------------------------------------
# 3a. Digest ohne KI (Standard)
# ---------------------------------------------------------------------------

def digest_without_ai(news: dict[str, list[dict]]) -> dict:
    """Saubere Aggregation: Schlagzeilen + Originalteaser, je Reiter."""
    tabs = {}
    for tid in TABS:
        tabs[tid] = {
            "id": tid,
            "summary": "",
            "items": [
                {
                    "headline": it["headline"],
                    "insight": it["snippet"],
                    "source": it["source"],
                    "url": it["url"],
                    "image": it.get("image", ""),
                    "favicon": it.get("favicon", ""),
                }
                for it in news.get(tid, [])
            ],
        }
    briefing = [news[tid][0]["headline"] for tid in TABS if news.get(tid)]
    return {"briefing": briefing, "tabs": tabs}


# ---------------------------------------------------------------------------
# 3b. Digest mit KI (optional, --ai)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "Du bist Chefredakteur eines privaten, hochwertigen Finanz-Morgenbriefings "
    "im Stil der Financial Times, auf Deutsch, für eine Person, die "
    "International Management studiert und sich für Aktien und Finanzen "
    "interessiert. Fasse prägnant zusammen und ordne ein – kein Geschwafel, "
    "keine Wiederholungen. Wähle pro Reiter die wirklich wichtigsten Meldungen "
    "aus dem gelieferten Material aus und verwende ausschließlich die dort "
    "vorhandenen URLs. Erfinde keine Fakten und keine Zahlen."
)

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "briefing": {"type": "array", "items": {"type": "string"}},
        "tabs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "summary": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "headline": {"type": "string"},
                                "insight": {"type": "string"},
                                "source": {"type": "string"},
                                "url": {"type": "string"},
                            },
                            "required": ["headline", "insight", "source", "url"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "summary", "items"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["briefing", "tabs"],
    "additionalProperties": False,
}


def summarize_with_claude(news: dict[str, list[dict]], ticker: list[dict]) -> dict:
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "FEHLER: --ai gesetzt, aber ANTHROPIC_API_KEY fehlt. Secret "
            "hinterlegen oder ohne --ai starten.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    payload = {
        "reiter": [
            {"id": tid, "titel": TABS[tid]["title"], "meldungen": news.get(tid, [])}
            for tid in TABS
        ],
        "markt": ticker,
    }
    user_msg = (
        "Hier ist das Rohmaterial der letzten Stunden. Erstelle daraus das "
        "heutige Morgenbriefing; gib pro Reiter die wichtigsten Meldungen mit "
        "kurzer Einordnung zurück und nutze nur die vorhandenen URLs.\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"
    )
    print(f"- Frage Claude ({MODEL}) …")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = json.loads(text)
    data["tabs"] = {t["id"]: t for t in data.get("tabs", [])}
    return data


def expert_commentary(watchlist: list[dict], news: dict[str, list[dict]]) -> str:
    """Kurze, professionelle Einordnung der Watchlist (nur mit --ai + Key).
    Bewusst OHNE erfundene Kursziele – seriöse Beobachtung statt Versprechen."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""
    rows = [
        {k: r.get(k) for k in ("name", "pct", "p5", "p20", "trend")}
        for r in watchlist
    ]
    headlines = [it["headline"] for it in news.get("watchlist", [])][:12]
    sys_prompt = (
        "Du bist erfahrener Aktien-Analyst einer Privatbank und schreibst auf "
        "Deutsch eine sachliche Einordnung (6–8 Sätze) zur Watchlist des Kunden. "
        "Stütze dich nur auf die gelieferten Kennzahlen und Schlagzeilen. Nenne "
        "konkrete Chancen UND Risiken. Erfinde KEINE Kursziele und KEINE "
        "Renditeversprechen. Kein Marketing, kein Hype – nüchtern und hochwertig. "
        "Schreibe reinen Fließtext: KEIN Markdown, KEINE Sternchen, KEINE "
        "Überschrift – beginne direkt mit der Einordnung."
    )
    user_msg = (
        "Kennzahlen (pct=heute, p5=1 Woche, p20=1 Monat, jeweils %):\n"
        f"{json.dumps(rows, ensure_ascii=False)}\n\n"
        f"Aktuelle Schlagzeilen zur Watchlist:\n- " + "\n- ".join(headlines)
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        print(f"- Erstelle Experten-Kommentar ({MODEL}) …")
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            system=sys_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        # Sicherheitsnetz: etwaiges Markdown entfernen (Sternchen/Rauten).
        text = text.replace("**", "").replace("##", "").replace("__", "")
        return text.strip()
    except Exception as exc:
        print(f"  ! Experten-Kommentar fehlgeschlagen: {exc}", file=sys.stderr)
        return ""


def _expert_cache_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "docs", "expert_cache.json")


def get_expert_commentary(now: datetime, watchlist: list[dict], news: dict) -> dict:
    """KI-Kommentar nur zu AI_HOURS neu erzeugen, sonst zwischengespeicherten
    Text weiterverwenden. Rückgabe: {"text":..., "stand":...}."""
    cache: dict = {}
    try:
        with open(_expert_cache_path(), encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        cache = {}

    # Neu erzeugen an den definierten Stunden – oder einmalig, falls noch leer.
    if now.hour in AI_HOURS or not cache.get("text"):
        text = expert_commentary(watchlist, news)
        if text:
            cache = {"text": text, "stand": now.strftime("%d.%m. %H:%M Uhr")}
            try:
                path = _expert_cache_path()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False)
            except Exception as exc:
                print(f"  ! Cache-Schreibfehler: {exc}", file=sys.stderr)
    return {"text": cache.get("text", ""), "stand": cache.get("stand", "")}


# ---------------------------------------------------------------------------
# 4. HTML-Dashboard rendern
# ---------------------------------------------------------------------------

def _quote_span(q: dict) -> str:
    cls = "up" if q["pct"] >= 0 else "down"
    sign = "+" if q["pct"] >= 0 else ""
    arr = "▲" if q["pct"] >= 0 else "▼"
    return (
        f'<span class="tick {cls}"><b>{html.escape(q["name"])}</b> '
        f'{fmt_price(q["last"])} <i><span class="arr">{arr}</span> {sign}{q["pct"]:.2f}%</i></span>'
    )


def render_board(board: dict[str, list[dict]]) -> str:
    """Erweitertes Markt-Board für den Extras-Reiter."""
    e = html.escape
    all_quotes = [q for rows in board.values() for q in rows]
    if not all_quotes:
        return '<p class="empty">Keine Marktdaten verfügbar.</p>'

    mover = max(all_quotes, key=lambda q: abs(q["pct"]))
    msign = "+" if mover["pct"] >= 0 else ""
    highlight = (
        f'<p class="bhi">Größte Tagesbewegung: <b>{e(mover["name"])}</b> '
        f'{msign}{mover["pct"]:.2f}%</p>'
    )

    sections = []
    for group, rows in board.items():
        if not rows:
            continue
        cells = []
        for q in rows:
            cls = "up" if q["pct"] >= 0 else "down"
            sign = "+" if q["pct"] >= 0 else ""
            arr = "▲" if q["pct"] >= 0 else "▼"
            cells.append(
                f'<div class="bcell {cls}"><span class="bname">{e(q["name"])}</span>'
                f'<span class="bval">{fmt_price(q["last"])}</span>'
                f'<span class="bpct"><span class="arr">{arr}</span> {sign}{q["pct"]:.2f}%</span></div>'
            )
        sections.append(
            f'<h3 class="bgroup">{e(group)}</h3><div class="board">{"".join(cells)}</div>'
        )
    return f'<section class="marketboard">{highlight}{"".join(sections)}</section>'


def render_analysis(rows: list[dict], ai_text: str = "", ai_stand: str = "") -> str:
    """Faktenbasierte Experten-Einordnung der Watchlist + optionaler KI-Kommentar."""
    e = html.escape
    if not rows:
        return ""

    def key5(r: dict) -> float:
        return r["p5"] if r["p5"] is not None else r["pct"]

    ranked = sorted(rows, key=key5, reverse=True)
    top, bottom = ranked[:3], ranked[-3:][::-1]

    def chip(r: dict) -> str:
        v = key5(r)
        cls = "up" if v >= 0 else "down"
        sign = "+" if v >= 0 else ""
        return f'<span class="achip {cls}">{e(r["name"])} {sign}{v:.1f}%</span>'

    def cell(v: float | None) -> str:
        if v is None:
            return '<td class="na">–</td>'
        cls = "up" if v >= 0 else "down"
        sign = "+" if v >= 0 else ""
        return f'<td class="{cls}">{sign}{v:.1f}%</td>'

    trs = []
    for r in sorted(rows, key=lambda x: x["name"]):
        tcls = {"Aufwärts": "up", "Abwärts": "down"}.get(r["trend"], "neu")
        nm = (
            f'<a class="nmlink" href="{e(_quote_link(r.get("symbol", "")))}" '
            f'target="_blank" rel="noopener noreferrer">{e(r["name"])}</a>'
        )
        trs.append(
            f'<tr><td class="nm">{nm}</td>'
            f'{cell(r["pct"])}{cell(r["p5"])}{cell(r["p20"])}'
            f'<td class="trend {tcls}">{e(r["trend"])}</td></tr>'
        )
    table = (
        "<table class='atable'><thead><tr><th>Wert</th><th>1 Tag</th>"
        "<th>1 Woche</th><th>1 Monat</th><th>Trend</th></tr></thead><tbody>"
        + "".join(trs)
        + "</tbody></table>"
    )
    stand_part = f' <span class="aistand">(Stand {e(ai_stand)})</span>' if ai_stand else ""
    ai_block = (
        f'<div class="aikom"><b>Einordnung des Analysten:</b>{stand_part} {e(ai_text)}</div>'
        if ai_text
        else ""
    )
    return (
        '<section class="analysis"><h2>Experten-Einordnung</h2>'
        '<p class="asub">Faktenbasiert aus aktuellen Kursdaten · Bildung &amp; '
        'Einordnung, keine Anlageberatung</p>'
        f'<div class="arow"><span class="alabel">▲ Stärkste (1 Woche)</span>{"".join(chip(r) for r in top)}</div>'
        f'<div class="arow"><span class="alabel">▼ Schwächste (1 Woche)</span>{"".join(chip(r) for r in bottom)}</div>'
        f'{table}{ai_block}</section>'
    )


def render_watchlist(quotes: list[dict]) -> str:
    """Flaches Kurs-Grid für den Watchlist-Reiter, sortiert nach Tagesgewinn."""
    e = html.escape
    if not quotes:
        return '<p class="empty">Keine Watchlist-Kurse verfügbar.</p>'
    rows = sorted(quotes, key=lambda q: q["pct"], reverse=True)
    mover = max(quotes, key=lambda q: abs(q["pct"]))
    msign = "+" if mover["pct"] >= 0 else ""
    highlight = (
        f'<p class="bhi">Größte Tagesbewegung: <b>{e(mover["name"])}</b> '
        f'{msign}{mover["pct"]:.2f}%</p>'
    )
    cells = []
    for q in rows:
        cls = "up" if q["pct"] >= 0 else "down"
        sign = "+" if q["pct"] >= 0 else ""
        arr = "▲" if q["pct"] >= 0 else "▼"
        url = _quote_link(q.get("symbol", ""))
        cells.append(
            f'<a class="bcell {cls}" href="{e(url)}" target="_blank" rel="noopener noreferrer">'
            f'<span class="bname">{e(q["name"])}</span>'
            f'<span class="bval">{fmt_price(q["last"])}</span>'
            f'<span class="bpct"><span class="arr">{arr}</span> {sign}{q["pct"]:.2f}%</span>'
            f'<span class="blink">Chart · TradingView ›</span></a>'
        )
    return f'<section class="marketboard">{highlight}<div class="board">{"".join(cells)}</div></section>'


def render_html(
    digest: dict,
    ticker: list[dict],
    board: dict,
    watchlist: list[dict],
    now: datetime,
) -> str:
    e = html.escape
    datum = now.strftime("%A, %d. %B %Y · %H:%M Uhr")

    ticker_html = "".join(_quote_span(q) for q in ticker) or (
        '<span class="tick">Keine Kursdaten</span>'
    )

    briefing_items = "".join(f"<li>{e(b)}</li>" for b in digest.get("briefing", []))
    briefing_html = (
        f'<section class="briefing"><h2>Das Wichtigste in Kürze</h2>'
        f"<ul>{briefing_items}</ul></section>"
        if briefing_items
        else ""
    )

    board_html = render_board(board)

    buttons, panels = [], []
    for i, (tid, cfg) in enumerate(TABS.items()):
        active = " active" if i == 0 else ""
        buttons.append(
            f'<button class="tabbtn{active}" data-tab="{tid}">{e(cfg["title"])}</button>'
        )
        tab = digest.get("tabs", {}).get(tid, {})
        summary = tab.get("summary", "")
        summary_html = f'<p class="tabsummary">{e(summary)}</p>' if summary else ""
        cards = []
        for it in tab.get("items", []):
            ins = (it.get("insight") or "").strip()
            # Teaser weglassen, wenn er nur die Schlagzeile wiederholt.
            if ins[:40].lower() == it["headline"][:40].lower():
                ins = ""
            insight = f'<p>{e(ins)}</p>' if ins else ""
            safe = _safe_url(it.get("url", ""))
            if safe:
                title_html = (
                    f'<a class="hl" href="{e(safe)}" target="_blank" '
                    f'rel="noopener noreferrer">{e(it["headline"])}</a>'
                )
            else:  # unsicheres/leeres Link-Schema -> kein anklickbarer Link
                title_html = f'<span class="hl">{e(it["headline"])}</span>'
            img = it.get("image", "")
            thumb = (
                f'<img class="thumb" src="{e(img)}" loading="lazy" '
                f'onerror="this.remove()" alt="">'
                if img.startswith("https://")
                else ""
            )
            fav = it.get("favicon", "")
            favimg = (
                f'<img class="fav" src="{e(fav)}" loading="lazy" '
                f'onerror="this.remove()" alt="">'
                if fav
                else ""
            )
            cards.append(
                f'<article class="card">{thumb}<div class="ctext">{title_html}{insight}'
                f'<span class="src">{favimg}{e(it.get("source", ""))}</span></div></article>'
            )
        cards_html = "".join(cards) or '<p class="empty">Heute keine Meldungen.</p>'
        # Im Extras-Reiter das Markt-Board, im Watchlist-Reiter die Kurstafel oben.
        extra = ""
        if tid == "extras":
            extra = board_html
        elif tid == "watchlist":
            extra = render_analysis(
                watchlist, digest.get("expert", ""), digest.get("expert_stand", "")
            ) + render_watchlist(watchlist)
        panels.append(
            f'<div class="panel{active}" id="panel-{tid}">{extra}{summary_html}{cards_html}</div>'
        )

    return _PAGE_TEMPLATE.format(
        datum=e(datum),
        ticker=ticker_html,
        briefing=briefing_html,
        buttons="".join(buttons),
        panels="".join(panels),
        archiv_link='<a class="archiv" href="archiv/">Archiv ältere Tage →</a>',
    )


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<!-- Offener Tab aktualisiert sich automatisch alle 30 Minuten. -->
<meta http-equiv="refresh" content="1800">
<title>Mein Finanz-Briefing</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
  :root {{
    --bg:#0a0f18; --bg2:#0d1626; --card:#101b2e; --line:#1f3048;
    --line-soft:#17243a; --text:#e9edf4; --muted:#8ea4c0; --muted2:#647488;
    --gold:#5b9dd9; --gold-bright:#8ec5ff; --up:#4fc78a; --down:#e0796d;
  }}
  * {{ box-sizing:border-box; }}
  html, body {{ overflow-x:hidden; max-width:100%; }}
  body {{ margin:0; color:var(--text); line-height:1.6;
          font-family:'Inter',system-ui,sans-serif; -webkit-font-smoothing:antialiased;
          background:radial-gradient(1200px 540px at 50% -220px,#16304f 0%,transparent 70%),var(--bg); }}
  .wrap {{ max-width:880px; margin:0 auto; padding:0 18px 80px; width:100%; }}
  a {{ color:inherit; }}
  .hl, .card p, .src, .briefing li, .tabsummary {{ overflow-wrap:anywhere; }}

  header {{ text-align:center; padding:46px 0 26px; }}
  .kicker {{ font-size:.7rem; letter-spacing:.34em; text-transform:uppercase;
             color:var(--gold); margin-bottom:14px; font-weight:500; }}
  header h1 {{ font-family:'Inter',system-ui,sans-serif; font-weight:700;
               font-size:2.2rem; line-height:1.1; margin:0; letter-spacing:-.02em; }}
  .rule {{ width:64px; height:1px; margin:20px auto 16px;
           background:linear-gradient(90deg,transparent,var(--gold),transparent); }}
  .datum {{ color:var(--muted); font-size:.82rem; letter-spacing:.02em; }}

  .ticker {{ display:flex; flex-wrap:wrap; justify-content:center; gap:0;
             border-top:1px solid var(--line); border-bottom:1px solid var(--line);
             padding:14px 0; margin:0 0 34px; font-size:.82rem; }}
  .tick {{ padding:2px 18px; border-right:1px solid var(--line-soft); }}
  .tick:last-child {{ border-right:none; }}
  .tick b {{ font-weight:600; color:var(--text); margin-right:7px; }}
  .tick i {{ font-style:normal; font-weight:500; font-variant-numeric:tabular-nums; }}
  .tick.up i {{ color:var(--up); }}
  .tick.down i {{ color:var(--down); }}

  .briefing {{ background:linear-gradient(180deg,var(--bg2),transparent);
               border:1px solid var(--line); border-radius:14px;
               padding:22px 26px; margin-bottom:34px; }}
  .briefing h2 {{ font-family:'Inter',system-ui,sans-serif; font-weight:600;
                  font-size:1.05rem; margin:0 0 14px; color:var(--gold-bright); }}
  .briefing ul {{ margin:0; padding:0; list-style:none; }}
  .briefing li {{ position:relative; padding-left:22px; margin:9px 0; color:#cfd3da; }}
  .briefing li::before {{ content:'\\2014'; position:absolute; left:0; color:var(--gold); }}

  .tabs {{ display:flex; flex-wrap:wrap; justify-content:center; gap:8px; margin-bottom:30px; }}
  .tabbtn {{ background:transparent; color:var(--muted); border:1px solid var(--line);
             padding:9px 18px; border-radius:999px; cursor:pointer;
             font-family:'Inter',sans-serif; font-size:.8rem; letter-spacing:.02em; transition:.2s; }}
  .tabbtn:hover {{ color:var(--text); border-color:var(--gold); }}
  .tabbtn.active {{ color:#0c0e12; font-weight:600; border-color:transparent;
             background:linear-gradient(180deg,var(--gold-bright),var(--gold)); }}
  .panel {{ display:none; animation:fade .4s ease; }}
  .panel.active {{ display:block; }}
  @keyframes fade {{ from {{ opacity:0; transform:translateY(6px); }} to {{ opacity:1; }} }}
  .tabsummary {{ color:#c2c7cf; font-style:italic; margin:0 0 20px; }}

  .marketboard {{ margin-bottom:26px; }}
  .bhi {{ color:var(--gold); font-size:.78rem; letter-spacing:.04em; margin:0 0 16px;
          text-transform:uppercase; }}
  .bhi b {{ color:var(--text); }}
  .bgroup {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.18em;
             color:var(--muted2); margin:20px 0 10px; }}
  .board {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:10px; }}
  .bcell {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
            padding:13px 15px; display:flex; flex-direction:column; gap:3px; transition:.2s; }}
  .bcell:hover {{ border-color:var(--gold); }}
  .bcell .bname {{ font-size:.72rem; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); }}
  .bcell .bval {{ font-size:1.2rem; font-weight:600; font-variant-numeric:tabular-nums; }}
  .bcell .bpct {{ font-size:.8rem; font-weight:500; font-variant-numeric:tabular-nums; }}
  .bcell.up .bpct {{ color:var(--up); }}
  .bcell.down .bpct {{ color:var(--down); }}
  a.bcell {{ text-decoration:none; color:inherit; cursor:pointer; }}
  .blink {{ font-size:.66rem; color:var(--gold); margin-top:5px; letter-spacing:.04em; }}
  a.bcell:hover .blink {{ color:var(--gold-bright); }}
  .nmlink {{ color:inherit; text-decoration:none; border-bottom:1px dotted var(--muted2); }}
  .nmlink:hover {{ color:var(--gold-bright); border-color:var(--gold-bright); }}

  .card {{ display:flex; gap:18px; border-bottom:1px solid var(--line-soft); padding:20px 2px; }}
  .card:first-of-type {{ padding-top:4px; }}
  .ctext {{ flex:1; min-width:0; }}
  .thumb {{ width:132px; height:99px; object-fit:cover; border-radius:12px; order:2;
            border:1px solid var(--line); flex-shrink:0; background:var(--card); }}
  .card .hl {{ font-family:'Inter',system-ui,sans-serif; color:var(--text); font-weight:600;
               text-decoration:none; font-size:1.22rem; line-height:1.35; display:block; transition:.2s; }}
  a.hl:hover {{ color:var(--gold-bright); }}
  .card p {{ margin:8px 0 10px; color:#b9c3d4; font-size:.95rem; }}
  .card .src {{ display:flex; align-items:center; gap:8px; color:var(--muted2);
               font-size:.72rem; letter-spacing:.06em; text-transform:uppercase; }}
  .fav {{ width:18px; height:18px; border-radius:5px; flex-shrink:0;
          background:var(--card); }}
  .arr {{ font-size:.7em; }}
  @media (max-width:560px) {{
    .thumb {{ width:96px; height:74px; }} .card {{ gap:12px; }}
    .atable {{ font-size:.75rem; }}
    .atable th, .atable td {{ padding:7px 4px; }}
  }}
  .empty {{ color:var(--muted); text-align:center; padding:40px 0; font-style:italic; }}

  .analysis {{ background:linear-gradient(180deg,var(--bg2),transparent);
               border:1px solid var(--line); border-radius:14px;
               padding:22px 24px; margin-bottom:26px; }}
  .analysis h2 {{ font-family:'Inter',system-ui,sans-serif; font-weight:600;
                  font-size:1.15rem; margin:0 0 4px; color:var(--gold-bright); }}
  .asub {{ color:var(--muted2); font-size:.7rem; letter-spacing:.05em;
           text-transform:uppercase; margin:0 0 18px; }}
  .arow {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-bottom:10px; }}
  .alabel {{ font-size:.8rem; color:var(--muted); margin-right:4px; }}
  .achip {{ font-size:.8rem; font-weight:600; padding:4px 11px; border-radius:999px;
            border:1px solid var(--line); font-variant-numeric:tabular-nums; }}
  .achip.up {{ color:var(--up); }} .achip.down {{ color:var(--down); }}
  .atable {{ width:100%; border-collapse:collapse; margin-top:16px; table-layout:fixed;
             font-size:.85rem; font-variant-numeric:tabular-nums; }}
  .atable td.nm {{ overflow-wrap:anywhere; }}
  .atable th {{ text-align:right; font-weight:500; color:var(--muted2);
               font-size:.68rem; text-transform:uppercase; letter-spacing:.06em;
               padding:6px 8px; border-bottom:1px solid var(--line); }}
  .atable th:first-child {{ text-align:left; }}
  .atable td {{ text-align:right; padding:9px 8px; border-bottom:1px solid var(--line-soft); }}
  .atable td.nm {{ text-align:left; color:var(--text); }}
  .atable td.up {{ color:var(--up); }} .atable td.down {{ color:var(--down); }}
  .atable td.na {{ color:var(--muted2); }}
  .atable td.trend {{ font-size:.78rem; }}
  .trend.up {{ color:var(--up); }} .trend.down {{ color:var(--down); }} .trend.neu {{ color:var(--muted); }}
  .aikom {{ margin-top:18px; padding-top:16px; border-top:1px solid var(--line);
            color:#c7d2e2; font-size:.92rem; line-height:1.7; }}
  .aikom b {{ color:var(--gold-bright); font-weight:600; }}
  .aistand {{ color:var(--muted2); font-size:.78em; }}

  .archiv {{ display:inline-block; margin-top:30px; color:var(--gold); text-decoration:none;
             font-size:.8rem; letter-spacing:.04em; }}
  .archiv:hover {{ color:var(--gold-bright); }}
  footer {{ margin-top:40px; padding-top:20px; border-top:1px solid var(--line-soft);
            color:var(--muted2); font-size:.72rem; letter-spacing:.03em; text-align:center; }}

  @media (max-width:560px) {{
    header h1 {{ font-size:2rem; }}
    .tick {{ padding:2px 12px; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="kicker">Privates Finanz-Briefing</div>
    <h1>Mein Finanz-Briefing</h1>
    <div class="rule"></div>
    <div class="datum">Stand: {datum} · aktualisiert sich automatisch</div>
  </header>
  <div class="ticker">{ticker}</div>
  {briefing}
  <div class="tabs">{buttons}</div>
  {panels}
  {archiv_link}
  <footer>Privat erstellt aus öffentlichen Quellen · automatisch generiert.</footer>
</div>
<script>
  document.querySelectorAll('.tabbtn').forEach(function (btn) {{
    btn.addEventListener('click', function () {{
      document.querySelectorAll('.tabbtn').forEach(function (b) {{ b.classList.remove('active'); }});
      document.querySelectorAll('.panel').forEach(function (p) {{ p.classList.remove('active'); }});
      btn.classList.add('active');
      document.getElementById('panel-' + btn.dataset.tab).classList.add('active');
    }});
  }});
</script>
</body>
</html>
"""


def write_outputs(page_html: str, now: datetime) -> None:
    docs = os.path.join(os.path.dirname(__file__), "..", "docs")
    archiv = os.path.join(docs, "archiv")
    os.makedirs(archiv, exist_ok=True)

    with open(os.path.join(docs, "index.html"), "w", encoding="utf-8") as f:
        f.write(page_html)

    datestr = now.strftime("%Y-%m-%d")
    with open(os.path.join(archiv, f"{datestr}.html"), "w", encoding="utf-8") as f:
        f.write(page_html)

    entries = sorted(
        (f[:-5] for f in os.listdir(archiv) if f.endswith(".html") and f != "index.html"),
        reverse=True,
    )
    links = "".join(f'<li><a href="{d}.html">{d}</a></li>' for d in entries)
    archiv_index = (
        "<!DOCTYPE html><html lang='de'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex, nofollow'>"
        "<title>Archiv – Finanz-Briefing</title>"
        "<style>"
        "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500&family=Playfair+Display:wght@700&display=swap');"
        "body{font-family:'Inter',system-ui,sans-serif;background:#0a0c10;color:#ece9e2;"
        "max-width:600px;margin:0 auto;padding:50px 20px;line-height:1.6;}"
        "h1{font-family:'Inter',system-ui,sans-serif;font-weight:700;font-size:2rem;margin:0 0 6px;}"
        "a{color:#c8a86a;text-decoration:none;}a:hover{color:#e6cd94;}"
        ".back{font-size:.85rem;}"
        "ul{list-style:none;padding:0;margin:24px 0 0;}"
        "li{border-bottom:1px solid #1b2026;padding:12px 0;letter-spacing:.02em;}"
        "</style></head><body>"
        "<h1>Archiv</h1><p class='back'><a href='../'>← Zurück zum heutigen Briefing</a></p>"
        f"<ul>{links}</ul></body></html>"
    )
    with open(os.path.join(archiv, "index.html"), "w", encoding="utf-8") as f:
        f.write(archiv_index)


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ai",
        action="store_true",
        help="Mit Claude-Zusammenfassung (braucht ANTHROPIC_API_KEY). "
        "Standard ist ohne KI (nur Schlagzeilen).",
    )
    args = parser.parse_args()

    now = datetime.now(TZ)
    print(f"== Finanz-Digest, {now:%Y-%m-%d %H:%M} ==")

    news = gather_news()
    ticker = gather_ticker()
    board = gather_board()
    watchlist = gather_watchlist()

    digest = digest_without_ai(news)
    if args.ai:
        regenerate = (now.hour in AI_HOURS)
        print(
            f"- Modus: mit KI-Experten-Kommentar "
            f"({'neu erzeugen' if regenerate else 'aus Zwischenspeicher'})"
        )
        ex = get_expert_commentary(now, watchlist, news)
        digest["expert"] = ex["text"]
        digest["expert_stand"] = ex["stand"]
    else:
        print("- Modus: ohne KI (nur Schlagzeilen-Aggregation)")

    page = render_html(digest, ticker, board, watchlist, now)
    write_outputs(page, now)
    print("✓ docs/index.html geschrieben.")


if __name__ == "__main__":
    main()
