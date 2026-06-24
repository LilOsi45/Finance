# ☕ Mein Finanz-Briefing

Ein privates, tägliches Finanz- und Nachrichten-Dashboard – die wichtigsten
Schlagzeilen kompakt nach Thema sortiert, plus ein Markt-Board. Wird jeden
Morgen automatisch erstellt.

Fünf Reiter:

0. **Meine Watchlist** – deine Lieblingsaktien mit Kurs + Tagesveränderung
   (nach Tagesgewinn sortiert) und passenden Schlagzeilen
1. **Aktien & Märkte** – Indizes, Einzelwerte, Quartalszahlen, Zinsen/Zentralbanken
2. **International Management & Wirtschaft** – Strategie, M&A, Welthandel, Zentralbanken
3. **Weltnachrichten** – wichtigste politische/geopolitische Ereignisse
4. **Markt-Board & Krypto** – erweiterte Kurstafel (Indizes, Währungen, Rohstoffe,
   Krypto mit Tagesveränderung), „größte Tagesbewegung", plus Krypto-/Konjunktur-Schlagzeilen

Oben gibt es ein **Morgen-Briefing** (die Top-Schlagzeilen des Tages) und eine
**Markt-Ticker-Leiste**. Vergangene Tage liegen im **Archiv**.

## So funktioniert es

Ein GitHub-Actions-Cron-Job läuft jeden Morgen, lädt aktuelle RSS-Quellen und
Marktdaten (kostenlos über Stooq, ohne API-Key), baut daraus das Dashboard und
schreibt es nach `docs/`. GitHub Pages serviert `docs/` als Webseite, die du
morgens per Lesezeichen öffnest.

**Standard ist ohne KI** – also kein API-Key, kein Secret, keine laufenden
Kosten. Die Schlagzeilen werden sauber aggregiert und nach Thema gruppiert.

## Einmalige Einrichtung

1. **GitHub Pages aktivieren**
   - **Settings → Pages**
   - Source: **Deploy from a branch**
   - Branch: `claude/daily-finance-digest-txp4o0`, Ordner: **`/docs`**

2. **Ersten Lauf auslösen**
   - **Actions → „Täglicher Finanz-Digest" → Run workflow**
   - Danach läuft es automatisch täglich (~07:30 Uhr) und das Dashboard ist
     unter der angezeigten Pages-URL erreichbar.

Mehr ist nicht nötig.

## Optional: KI-Zusammenfassung dazuschalten

Wenn du später doch von „nur Schlagzeilen" auf von Claude geschriebene
Einordnungen umstellen willst:

1. API-Key auf <https://console.anthropic.com> erstellen, im Repo unter
   **Settings → Secrets and variables → Actions** als `ANTHROPIC_API_KEY` ablegen.
2. In `.github/workflows/daily-digest.yml` den Schritt „Digest erzeugen" auf die
   auskommentierte `--ai`-Variante umstellen.

Kosten dann: wenige Cent pro Tag.

## Lokal testen

```bash
pip install -r requirements.txt
python scripts/build_digest.py          # ohne KI (Standard)
# python scripts/build_digest.py --ai   # mit KI (braucht ANTHROPIC_API_KEY)
```

Danach `docs/index.html` im Browser öffnen.

## Anpassen

In `scripts/build_digest.py` im Block **KONFIGURATION**:

- `WATCHLIST` – deine Aktien (Stooq-Symbol → Name; US endet auf `.us`, DE auf `.de`)
- `TABS` – Reiter und ihre RSS-Quellen (Quellen ergänzen/entfernen)
- `TICKER_SYMBOLS` / `MARKET_BOARD` – Kurse im Ticker bzw. im Markt-Board (Stooq-Symbole)
- `TIME_WINDOW_HOURS`, `MAX_ITEMS_PER_TAB` – Zeitfenster und Umfang
- `MODEL` – Modell für die optionale KI-Variante

## Hinweis zur Privatsphäre

GitHub Pages ist technisch öffentlich erreichbar (die URL ist aber nicht
verlinkt und unauffällig), auch bei privatem Repo. Da der Digest nur aus
öffentlichen Nachrichten besteht, ist das risikoarm; die Seiten tragen
zusätzlich `noindex`. Wer echte Zugriffsbeschränkung braucht, kann später auf
E-Mail-Versand umstellen.

## Mögliche Ausbaustufen

- KI-Zusammenfassung (s. o.)
- Wirtschaftskalender (anstehende Zinsentscheide, Quartalszahlen)
- Optionale Morgen-E-Mail mit Link zum Dashboard
