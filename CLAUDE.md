# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Kinda Bet** — a Flask web app that surfaces every market a sportsbook offers (1X2, totals, handicaps, BTTS, half-time, …) and compares the prices across NL operators.

- **Reference operator:** 711.nl. It exposes the most markets, so we render its catalog as the source of truth.
- **Comparison operators:** Unibet.nl, TOTO.nl, Bet365.nl. Each market row expands into a per-operator table with green/red % deltas vs the reference.
- **Discovery fallback:** if a league returns nothing from 711, we fall back to Unibet (also Kambi platform).
- **Leagues:** UEFA Champions League, Europa League, Premier League (England), Süper Lig (Turkey), 1. Lig (Turkey).

## Run / install

**Backend (Python/Flask):**
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
.venv/bin/python app.py    # http://127.0.0.1:5000
```

**Frontend (Vite + React + TypeScript) lives in `web/`:**
```bash
cd web && npm install
npm run dev                # dev server at http://127.0.0.1:5173 (proxies /api → :5000)
npm run build              # produces web/dist/ — Flask serves this in production
```

In production, Flask serves the bundled React app from `web/dist/`. In development, run Flask and Vite side by side: hit `:5173` (Vite proxies `/api` calls to Flask). If `web/dist/index.html` is missing, Flask returns a 503 with build instructions.

`google-chrome` **is** required — `fetch_toto` shells out to it via `--headless --dump-dom` to render TOTO's match pages (their full market catalog is SignalR-only). Bet365 is still a placeholder until someone wires up a SPA-capable scraper for it.

There's no test suite or linter. Smoke tests:

```bash
.venv/bin/python -c "import scrapers; print(len(scrapers.discover_matches()))"
.venv/bin/python -c "import scrapers; print(len(scrapers.fetch_kambi(operator='711.nl', kambi_event_id=<id>)))"
```

## Architecture

Three Python files; the Kambi parser is the heaviest.

### `scrapers.py`

- `discover_matches()` — reads Kambi listView for each `(competition, league_term)` pair via 711, falls back to Unibet. Returns canonical match dicts keyed by `kambi_event_id`.
- `fetch_kambi(operator, kambi_event_id)` — pulls the **`betoffer/event/{id}` endpoint** (not `listView`). This returns *every* bet offer for the event — typically 300+ on a UCL match. Each offer is parsed by `_kambi_parse_betoffers`.
- `fetch_toto(home, away, …)` — fuzzy-finds the TOTO event via the JSON `/search` endpoint, then renders the match page (`https://sport.toto.nl/wedden/wedstrijd/<event_id>`) in headless Chrome. TOTO's REST API only exposes the *primary* (1X2 early-payout) market; their full catalog (handicaps, totals, BTTS, special bets, …) is delivered via SignalR WebSocket. Rather than reverse-engineering that auth+protocol, we let Chrome's virtual-time-budget run for 30s, dump the rendered DOM, and walk every inline JSON market object (~387 markets on a UCL match, 3 on a low-tier Turkish match — TOTO genuinely offers fewer markets there). Cost: ~3-5s per refresh per match. Mapping `groupCode` → canonical key lives in `TOTO_GROUP_TO_CANONICAL`. **Asian-Handicap line scaling**: TOTO stores AH lines as integer counts of 0.25 steps (`handicapValue=2` means line=0.5), so we divide by 4 for `ASIAN_HANDICAP*` markets only.
- `fetch_tonybet(home, away, kickoff_utc_iso)` — TonyBet runs on a Sportradar-derived JSON API at `platform.tonybet.nl/api/event/list`. Three sport categories cover all our leagues: `101` (UEFA UCL+UEL), `41` (Premier League), `111` (Süper Lig + 1. Lig). The API caps at 100-150 events per call, so we fetch by category. **TTL cache (60s)** in `_tonybet_cache` collapses redundant calls during a bulk-refresh sweep. Markets identified by integer `id`: `621` (1X2), `589` (BTTS), `868` (OU 2.5), `557` (Asian Handicap, line via `specifiers="hcp=X"`), `721` (Double Chance). Asian Handicap selections come back as outcome IDs `1714`/`1715` — we substitute the home/away team names from the input args so they line up with Kambi's team-name selection_keys cross-operator. Bet365 was tried first but their site obfuscates classes and loads odds via SPA-only WebSocket, while bwin.com IP-blocks datacenter IPs.

#### Kambi market canonicalization (load-bearing)

`KAMBI_CANONICAL_CRIT` in `scrapers.py` maps `criterion.id` → canonical root (e.g. `1001159858 → MATCH_RESULT_FT`, `1001642858 → BTTS_FT`). For markets with a line (Over/Under, handicap), the line is appended: `OVER_UNDER_FT@2.5`, `ASIAN_HANDICAP_FT@-1.5`. Unknown criteria fall back to `KAMBI_<crit_id>[@line]`.

**Why this matters:**
- The line is on each `outcome.line` (in milliunits), *not* on the bet offer. `_kambi_offer_line` reads it from the first non-null outcome.
- The previous `lifetime == "FULL_TIME" and betOfferType.id == 2` heuristic *false-matched* "First Goal", "Most Shots on Target", "Draw No Bet" — they all share that shape. Always identify canonical markets by `criterion.id` lookup; never by betOfferType alone.
- Cross-Kambi comparison (711 ↔ Unibet) works automatically for *all* markets, canonical or not, because criterion.id is shared across brands. TOTO can only line up with markets that have a canonical root we explicitly map.

When a new market type starts showing up (e.g. corners, cards, player goals), inspect the offer's `criterion.englishLabel` and decide: add to `KAMBI_CANONICAL_CRIT` only if it has a meaningful TOTO equivalent. Otherwise let it fall through to the `KAMBI_<crit_id>` bucket.

#### Selection canonicalization

`_kambi_canonical_selection` normalizes outcome labels: `1/X/2`, `Yes/Ja → YES`, `No/Nee → NO`, `Over → OVER`, `Under → UNDER`. Handicap selections (where outcome labels are team names) pass through unchanged — Kambi platform uses identical team names across brands, so 711 ↔ Unibet match without aliasing.

### `db.py`

- Two tables: `matches` (one row per fixture) and `odds_snapshots` (append-only; one row per `(operator, market_key, selection_key)` per refresh).
- `latest_odds(match_id)` is a self-join that picks `MAX(taken_at)` per `(operator, market_key, selection_key)` — that's the data the UI renders.
- All access goes through a module-level `threading.Lock` (SQLite + threads).
- Schema notes: `odds_snapshots` replaces an older `snapshots` table from the prior 1X2-only design. The old table may still exist in `data/odds.db`; it's harmless dead weight.

### `app.py`

- `GET /api/matches` — matches grouped by competition. `?sync=1` forces re-discovery from Kambi. Each match carries `last_refresh`.
- `GET /api/match/<id>` — pivots `latest_odds` into the nested `markets[].selections[].operators[]` shape the frontend expects. Markets are filtered to those the **reference operator (711) has odds for** — if 711 didn't return a market, it doesn't show, even if Unibet did.
- `POST /api/match/<id>/refresh` — runs `fetch_all_for_match` in a 2-worker thread pool, blocks on `fut.result(timeout=180)`, inserts to DB, returns the new market view.
- `_market_sort_key` and `_selection_sort_key` define display order: 1X2 → DC → BTTS → OU → Handicap → everything else; selections inside go 1/X/2, OVER/UNDER, YES/NO.

### Frontend (`web/`)

Vite + React 18 + TypeScript + react-router-dom. SPA with two routes:

- `/` — `pages/HomePage.tsx`: featured strip (top 4 fixtures, prioritized UCL → UEL → Premier → Süper → 1. Lig), then league sections with click-to-detail match cards.
- `/match/:id` — `pages/MatchPage.tsx`: hero with big team names + league badge + kickoff, "En İyi Fiyatlar" (best-prices) summary, all markets with full operator comparison.

Component breakdown:
- `App.tsx` — `<BrowserRouter>` + routes. Catch-all renders HomePage so unknown URLs land somewhere usable.
- `components/Topbar.tsx` — sticky header, logo, search, "↻ Maçları Yenile" button. Used by HomePage.
- `components/SearchBar.tsx` — `/` to focus, `Esc` to clear, debounced 150ms in HomePage.
- `components/MatchCard.tsx` — the new home-page card: smart relative kickoff (Bugün/Yarın/Çar), team layout, 3-up reference odds (1/X/2) read from `match.headline_odds`. Whole card is a `<Link to=/match/:id>`.
- `components/FeaturedStrip.tsx` — top fixtures grid above the league listing.
- `components/LeagueCard.tsx` — collapsible league section, renders `MatchCard`s.
- `components/MarketRow.tsx` — collapsible market in the detail page (reference odds inline, expand for comparison).
- `components/ComparisonTable.tsx` — operator × selection grid with green/red diff pills.
- `components/BestPricesPanel.tsx` — picks best price per selection across operators for the headline markets (1X2, BTTS, OU 2.5).
- `api.ts` — single source of truth for the backend contract. If you change a Flask route shape, update here and TypeScript will scream at every callsite.
- `format.ts` — display helpers: `fmtOdd`, `fmtPct`, `fmtKickoff` (TR locale), `fmtKickoffSmart` (Bugün/Yarın/weekday/date), `fmtTs`, `diffSign`, `norm`, `leaguePillMeta`.
- `index.css` — full design system: cream/orange palette, Inter font (CDN-loaded), match-card hover lifts, hero gradient, best-prices panel, skeleton shimmer animations, mobile responsive grid.

**API contract gotcha:** `/api/matches` includes `headline_odds: {"1": …, "X": …, "2": …} | null` per match — pulled in a single SQL JOIN by `db.headline_odds()`. This drives the inline 1/X/2 pills on home cards without N+1 fetches.

The reference row in the comparison table is starred (`★`) and never has a diff pill (it'd always be 0%).

**Flask SPA fallback** is in `app.py` as the last route (`/<path:path>`): static assets in `web/dist/` resolve directly; everything else returns `index.html` so deep links to `/match/123` work on hard refresh.

## Repo notes

- `data/odds.db` is committed; it's live state, not a fixture. Don't `rm` casually.
- `atletico_arsenal_1x2_NL.xlsx` at the repo root is a one-off export, unused by the app.
- `web/dist/` is the build output — Flask serves it directly. If you change components in `web/src/`, run `npm run build` (or use `npm run dev` for hot-reload at `:5173`).
- No auth, no .env, no secrets — local-only tool.

## Adding things

- **A new league:** append `(display_name, kambi_term)` to `COMPETITIONS` in `scrapers.py`. The term is whatever Kambi expects in `listView/football/{term}.json` — slash-separated for country-scoped leagues (`england/premier_league`). Test by calling `kambi_listview("sevelevnl", "<term>")`; an empty list means the term is wrong.
- **A new operator:** add a `fetch_<op>(operator, kambi_event_id, home, away, kickoff_utc_iso, league_term, **_)` function returning a list of canonical odds rows, register it in `OPERATORS`. If it's a Kambi brand, just add to `KAMBI_BRANDS` and let `fetch_kambi` handle it.
- **A new canonical market:** identify the `criterion.id` from a Kambi event payload, add to `KAMBI_CANONICAL_CRIT`. Then update the TOTO mapping in `_toto_canonical_for_market` if TOTO exposes the same market.
