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


def _gnews(query: str) -> str:
    """Google-News-Such-Feed (zuverlässig, sprachlich filterbar)."""
    return "https://news.google.com/rss/search?q=" + quote(query) + "&hl=de&gl=DE&ceid=DE:de"


# Reiter und ihre RSS-Quellen. Reihenfolge = Reiter-Reihenfolge.
# Der Reiter "extras" zeigt zusätzlich oben das Markt-Board (s. MARKET_BOARD).
TABS: dict[str, dict] = {
    "maerkte": {
        "title": "Aktien & Märkte",
        "feeds": [
            "https://www.tagesschau.de/wirtschaft/index~rss2.xml",
            "https://www.handelsblatt.com/contentexport/feed/finanzen",
            "https://finance.yahoo.com/news/rssindex",
            "http://feeds.marketwatch.com/marketwatch/topstories/",
            _gnews("DAX OR S&P 500 OR Nasdaq Aktien Börse when:2d"),
        ],
    },
    "wirtschaft": {
        "title": "International Management & Wirtschaft",
        "feeds": [
            "https://www.handelsblatt.com/contentexport/feed/unternehmen",
            "https://www.nzz.ch/wirtschaft.rss",
            _gnews("Konzern OR Übernahme OR M&A OR CEO Strategie when:2d"),
            _gnews("global economy OR world trade OR central bank when:2d"),
        ],
    },
    "welt": {
        "title": "Weltnachrichten",
        "feeds": [
            "https://www.tagesschau.de/ausland/index~rss2.xml",
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

# Markt-Ticker oben (kompakt). Stooq-Symbol -> Anzeigename.
TICKER_SYMBOLS: dict[str, str] = {
    "^spx": "S&P 500",
    "^ndq": "Nasdaq",
    "^dji": "Dow Jones",
    "^dax": "DAX",
    "btcusd": "Bitcoin",
    "ethusd": "Ethereum",
}

# Erweitertes Markt-Board (im Extras-Reiter), gruppiert.
MARKET_BOARD: dict[str, dict[str, str]] = {
    "Indizes": {
        "^spx": "S&P 500",
        "^dax": "DAX",
        "^ndq": "Nasdaq",
        "^dji": "Dow Jones",
        "^ftm": "FTSE 100",
        "^nkx": "Nikkei 225",
    },
    "Währungen": {
        "eurusd": "EUR/USD",
        "eurchf": "EUR/CHF",
        "usdjpy": "USD/JPY",
    },
    "Rohstoffe": {
        "xauusd": "Gold",
        "cl.f": "Öl (WTI)",
    },
    "Krypto": {
        "btcusd": "Bitcoin",
        "ethusd": "Ethereum",
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
    source = parsed.feed.get("title", "") if parsed.feed else ""
    items = []
    for e in parsed.entries:
        ts = _entry_time(e)
        if ts and ts < cutoff:
            continue
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        summary = _strip_html(e.get("summary") or "")[:400]
        items.append(
            {
                "headline": title,
                "url": link,
                "source": source or _domain(link),
                "snippet": summary,
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
    """Letzter Schlusskurs + Tagesveränderung in % via Stooq-Historie."""
    d2 = datetime.now(timezone.utc).strftime("%Y%m%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y%m%d")
    url = f"https://stooq.com/q/d/l/?s={symbol}&d1={d1}&d2={d2}&i=d"
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        closes = [float(r["Close"]) for r in rows if r.get("Close") not in (None, "", "N/D")]
        if len(closes) < 2:
            return None
        last, prev = closes[-1], closes[-2]
        pct = (last - prev) / prev * 100 if prev else 0.0
        return {"last": last, "pct": pct}
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


# ---------------------------------------------------------------------------
# 4. HTML-Dashboard rendern
# ---------------------------------------------------------------------------

def _quote_span(q: dict) -> str:
    cls = "up" if q["pct"] >= 0 else "down"
    sign = "+" if q["pct"] >= 0 else ""
    return (
        f'<span class="tick {cls}"><b>{html.escape(q["name"])}</b> '
        f'{fmt_price(q["last"])} <i>{sign}{q["pct"]:.2f}%</i></span>'
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
            cells.append(
                f'<div class="bcell {cls}"><span class="bname">{e(q["name"])}</span>'
                f'<span class="bval">{fmt_price(q["last"])}</span>'
                f'<span class="bpct">{sign}{q["pct"]:.2f}%</span></div>'
            )
        sections.append(
            f'<h3 class="bgroup">{e(group)}</h3><div class="board">{"".join(cells)}</div>'
        )
    return f'<section class="marketboard">{highlight}{"".join(sections)}</section>'


def render_html(digest: dict, ticker: list[dict], board: dict, now: datetime) -> str:
    e = html.escape
    datum = now.strftime("%A, %d. %B %Y · %H:%M Uhr")

    ticker_html = "".join(_quote_span(q) for q in ticker) or (
        '<span class="tick">Keine Kursdaten</span>'
    )

    briefing_items = "".join(f"<li>{e(b)}</li>" for b in digest.get("briefing", []))
    briefing_html = (
        f'<section class="briefing"><h2>☕ Morgen-Briefing</h2>'
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
            insight = f'<p>{e(it["insight"])}</p>' if it.get("insight") else ""
            cards.append(
                f'<article class="card">'
                f'<a class="hl" href="{e(it["url"])}" target="_blank" rel="noopener">'
                f'{e(it["headline"])}</a>{insight}'
                f'<span class="src">{e(it.get("source", ""))}</span></article>'
            )
        cards_html = "".join(cards) or '<p class="empty">Heute keine Meldungen.</p>'
        # Im Extras-Reiter das Markt-Board oben anzeigen.
        extra = board_html if tid == "extras" else ""
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
<title>Mein Finanz-Briefing</title>
<style>
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:#0f1115; color:#e8eaed;
          font-family:Georgia,'Times New Roman',serif; line-height:1.5; }}
  .wrap {{ max-width:860px; margin:0 auto; padding:20px 16px 60px; }}
  header h1 {{ font-size:1.7rem; margin:0 0 2px; }}
  .datum {{ color:#9aa0aa; font-size:.9rem; margin-bottom:14px; }}
  .ticker {{ display:flex; flex-wrap:wrap; gap:14px; padding:10px 12px;
             background:#1a1d24; border:1px solid #2a2e38; border-radius:10px;
             font-family:system-ui,sans-serif; font-size:.85rem; margin-bottom:22px; }}
  .tick b {{ font-weight:600; }}
  .tick i {{ font-style:normal; font-weight:600; }}
  .tick.up i {{ color:#4caf50; }}
  .tick.down i {{ color:#ef5350; }}
  .briefing {{ background:#1a1d24; border:1px solid #2a2e38; border-left:3px solid #e0a96d;
               border-radius:10px; padding:14px 18px; margin-bottom:24px; }}
  .briefing h2 {{ margin:0 0 8px; font-size:1.15rem; }}
  .briefing ul {{ margin:0; padding-left:20px; }}
  .briefing li {{ margin:5px 0; }}
  .tabs {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:18px; }}
  .tabbtn {{ background:#1a1d24; color:#9aa0aa; border:1px solid #2a2e38;
             padding:8px 14px; border-radius:20px; cursor:pointer;
             font-family:system-ui,sans-serif; font-size:.85rem; }}
  .tabbtn.active {{ background:#e0a96d; color:#15171c; border-color:#e0a96d; font-weight:600; }}
  .panel {{ display:none; }}
  .panel.active {{ display:block; }}
  .tabsummary {{ color:#c9cdd4; font-style:italic; margin:0 0 16px; }}
  .marketboard {{ margin-bottom:20px; }}
  .bhi {{ color:#e0a96d; font-family:system-ui,sans-serif; font-size:.9rem; margin:0 0 12px; }}
  .bgroup {{ font-size:.8rem; text-transform:uppercase; letter-spacing:.05em;
             color:#9aa0aa; font-family:system-ui,sans-serif; margin:14px 0 6px; }}
  .board {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
            gap:8px; }}
  .bcell {{ background:#1a1d24; border:1px solid #2a2e38; border-radius:8px;
            padding:8px 10px; display:flex; flex-direction:column;
            font-family:system-ui,sans-serif; }}
  .bcell .bname {{ font-size:.78rem; color:#9aa0aa; }}
  .bcell .bval {{ font-size:1.05rem; font-weight:600; }}
  .bcell .bpct {{ font-size:.82rem; font-weight:600; }}
  .bcell.up .bpct {{ color:#4caf50; }}
  .bcell.down .bpct {{ color:#ef5350; }}
  .card {{ background:#1a1d24; border:1px solid #2a2e38; border-radius:10px;
           padding:14px 16px; margin-bottom:12px; }}
  .card .hl {{ color:#e8eaed; font-weight:600; text-decoration:none; font-size:1.05rem; }}
  .card .hl:hover {{ color:#e0a96d; }}
  .card p {{ margin:6px 0; color:#c9cdd4; font-size:.95rem; }}
  .card .src {{ color:#9aa0aa; font-size:.78rem; font-family:system-ui,sans-serif; }}
  .empty {{ color:#9aa0aa; }}
  .archiv {{ display:inline-block; margin-top:18px; color:#e0a96d;
             font-family:system-ui,sans-serif; font-size:.85rem; }}
  footer {{ margin-top:30px; color:#6b7280; font-size:.75rem;
            font-family:system-ui,sans-serif; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>☕ Mein Finanz-Briefing</h1>
    <div class="datum">{datum}</div>
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
        "<style>body{font-family:system-ui,sans-serif;background:#0f1115;color:#e8eaed;"
        "max-width:600px;margin:40px auto;padding:0 16px;}a{color:#e0a96d;}"
        "li{margin:6px 0;}</style></head><body>"
        "<h1>Archiv</h1><p><a href='../'>← Zurück zum heutigen Briefing</a></p>"
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

    if args.ai:
        print("- Modus: mit KI-Zusammenfassung")
        digest = summarize_with_claude(news, ticker)
    else:
        print("- Modus: ohne KI (Schlagzeilen-Aggregation)")
        digest = digest_without_ai(news)

    page = render_html(digest, ticker, board, now)
    write_outputs(page, now)
    print("✓ docs/index.html geschrieben.")


if __name__ == "__main__":
    main()
