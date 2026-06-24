# ☕ Mein Finanz-Briefing

Ein privates, tägliches Finanz- und Nachrichten-Dashboard im Stil der Financial
Times – kompakt zusammengefasst von Claude, automatisch jeden Morgen erstellt.

Vier Reiter:

1. **Aktien & Märkte** – Indizes, Einzelwerte, Quartalszahlen, Zinsen/Zentralbanken
2. **International Management & Unternehmen** – Strategie, M&A, Führungswechsel, Welthandel
3. **Weltnachrichten & Geopolitik** – politische Ereignisse mit Markt-/Wirtschaftsbezug
4. **DACH & Krypto** – Deutschland/Österreich/Schweiz-Wirtschaft + Bitcoin/Krypto

Oben gibt es ein **Morgen-Briefing** (die wichtigsten Punkte des Tages) und eine
**Markt-Ticker-Leiste** mit Tagesveränderungen. Vergangene Tage liegen im **Archiv**.

## So funktioniert es

Ein GitHub-Actions-Cron-Job läuft jeden Morgen, lädt aktuelle RSS-Quellen und
einen Markt-Snapshot (kostenlos über Stooq, ohne API-Key), lässt Claude pro
Reiter zusammenfassen und schreibt das Dashboard nach `docs/`. GitHub Pages
serviert `docs/` als Webseite, die du morgens per Lesezeichen öffnest.

## Einmalige Einrichtung

1. **Anthropic-API-Key als Secret hinterlegen**
   - Key erstellen unter <https://console.anthropic.com> → API Keys
   - Im Repo: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `ANTHROPIC_API_KEY`, Wert: dein Key
   - Kosten: wenige Cent pro Tag (ein Aufruf täglich)

2. **GitHub Pages aktivieren**
   - **Settings → Pages**
   - Source: **Deploy from a branch**
   - Branch: `claude/daily-finance-digest-txp4o0`, Ordner: **`/docs`**
   - Nach dem ersten Lauf ist das Dashboard unter der angezeigten Pages-URL erreichbar.

3. **Ersten Lauf auslösen**
   - **Actions → „Täglicher Finanz-Digest" → Run workflow**
   - Danach läuft es automatisch täglich (~07:30 Uhr).

## Lokal testen

```bash
pip install -r requirements.txt

# Ohne KI (nur Schlagzeilen – kein API-Key nötig):
python scripts/build_digest.py --no-ai

# Mit KI-Zusammenfassung:
ANTHROPIC_API_KEY=sk-... python scripts/build_digest.py
```

Danach `docs/index.html` im Browser öffnen.

## Anpassen

In `scripts/build_digest.py` im Block **KONFIGURATION**:

- `TABS` – Reiter und ihre RSS-Quellen (Quellen ergänzen/entfernen)
- `MARKET_SYMBOLS` – Indizes/Krypto im Ticker (Stooq-Symbole)
- `MODEL` – `claude-opus-4-8` (beste Qualität) oder günstiger
  `claude-sonnet-4-6` / `claude-haiku-4-5`
- `TIME_WINDOW_HOURS`, `MAX_ITEMS_PER_TAB` – Zeitfenster und Umfang

## Hinweis zur Privatsphäre

GitHub Pages ist technisch öffentlich erreichbar (die URL ist aber nicht
verlinkt und unauffällig), auch bei privatem Repo. Da der Digest nur aus
öffentlichen Nachrichten besteht, ist das risikoarm; die Seiten tragen
zusätzlich `noindex`, werden also nicht von Suchmaschinen erfasst. Wer echte
Zugriffsbeschränkung braucht, kann später auf E-Mail-Versand umstellen.

## Mögliche Ausbaustufen

- Wirtschaftskalender (anstehende Zinsentscheide, Quartalszahlen)
- Persönliche Watchlist eigener Aktien
- Optionale Morgen-E-Mail mit den Top-Punkten + Link zum Dashboard
- `web_search`-Tool für tagesaktuelle Recherche zusätzlich zu RSS
