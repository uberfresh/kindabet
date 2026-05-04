"""Kinda Bet — Flask web app.

Surfaces matches across UCL, UEL, Premier League, Süper Lig, and TFF 1. Lig.
711.nl is the reference operator (it carries the most markets); Unibet.nl,
TOTO.nl, TonyBet.nl are shown for comparison.

The frontend lives in `web/` (Vite + React + TS). In production, Flask serves
the built bundle from `web/dist/`. In dev, run `npm run dev` in `web/` (port
5173) — Vite proxies `/api/*` to Flask :5000."""
import os
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request, send_from_directory

import db
import scrapers

_HERE = os.path.dirname(os.path.abspath(__file__))
_DIST = os.path.join(_HERE, "web", "dist")

# static_folder=None disables Flask's auto-static handler. The SPA catch-all
# at the bottom of this file serves files from web/dist/ for known asset
# paths and falls back to index.html for everything else (so deep links like
# /firsatlar and /match/42 work on hard refresh).
app = Flask(__name__, static_folder=None)
db.init()

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="refresh")

# Auto-refresh cadence: kept in sync with deploy/kindabet-refresh.timer
# (OnUnitActiveSec=1h). Frontend uses this + finished_at to compute the
# countdown until the next automated sweep.
AUTO_REFRESH_INTERVAL_SECONDS = 3600


def _refresh_match_in_background(match_id, match_dict):
    rows = scrapers.fetch_all_for_match(match_dict)
    # If TOTO resolved a fresh event id (cache miss or stale-cache fallback),
    # persist it so the next refresh skips the rate-limited /search lookup.
    for r in rows:
        if r.get("operator") == "TOTO.nl" and r.get("toto_event_id"):
            try:
                db.set_toto_event_id(match_id, int(r["toto_event_id"]))
            except (TypeError, ValueError):
                pass
            break
    db.insert_snapshots(match_id, rows)
    return rows


# ---------- bulk refresh job state ----------

_refresh_all_lock = threading.Lock()
_refresh_all_job = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "total": 0,
    "completed": 0,
    "failed": 0,
    "error": None,
    "scope": None,   # "all" | "discovery" | "sport:<term>" | None
}

def _utc_now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _enabled_competitions():
    """Resolve the list of competitions to scan from the settings table.
    Falls back to the hardcoded defaults when no setting is stored.

    Display names AND sport_term are RE-RESOLVED from the current Kambi
    taxonomy so legacy saved settings (which lacked sport_term) get the
    correct sport classification automatically."""
    saved = db.get_setting("enabled_leagues")
    if not saved or not isinstance(saved, list) or not saved:
        return list(scrapers.COMPETITIONS)

    try:
        all_leagues = scrapers.kambi_list_all_sports(
            scrapers.KAMBI_BRANDS[scrapers.REFERENCE_OPERATOR])
        by_term = {lg["league_term"]: lg for lg in all_leagues}
    except Exception:
        by_term = {}

    out = []
    for item in saved:
        if not isinstance(item, dict):
            continue
        term = item.get("league_term")
        if not term:
            continue
        # Default truly-legacy entries (pre-multi-sport — no sport_term ever
        # written) to football. Don't fall through to by_term lookup: when a
        # league_term is shared across sports (champions_league exists in
        # football + handball + volleyball), by_term last-write-wins would
        # silently misroute legacy football entries to volleyball.
        sport = (item.get("sport_term") or "football").lower()
        # Prefer the freshly-walked display name for the (sport, term) pair —
        # but fall back to whatever's stored if the live taxonomy is offline.
        meta = by_term.get(term) or {}
        # Only apply the meta name if it's actually for the same sport
        # (avoids handing back a volleyball league name for football UCL).
        if meta.get("sport_term") == sport:
            name = meta.get("display_name") or item.get("display_name") or term
        else:
            name = item.get("display_name") or term
        out.append({
            "sport_term":   sport,
            "display_name": name,
            "league_term":  term,
        })
    return out


def _enabled_league_terms():
    return [c["league_term"] for c in _enabled_competitions()]


def _enabled_sport_league_pairs():
    """Composite (sport_term, league_term) tuples for the currently-enabled
    leagues — needed because the same league_term may exist under multiple
    sports (Champions League exists for both football and handball)."""
    return [(c["sport_term"], c["league_term"]) for c in _enabled_competitions()]


def _scrape_discovery():
    """Run match discovery against currently-enabled competitions and persist
    new fixtures. Returns {"discovered": int, "error": str|None}. Fast (~1-2s
    with cache); safe to call synchronously from a request handler."""
    try:
        matches = scrapers.discover_matches(_enabled_competitions())
    except Exception as e:
        return {"discovered": 0, "error": f"discover failed: {e}"}
    n = 0
    for m in matches:
        try:
            db.upsert_match(m)
            n += 1
        except Exception:
            pass
    return {"discovered": n, "error": None}


def _refresh_sweep_worker(sport=None, discover=True, scope_label=None):
    """Coordinator thread: optionally re-discover, then refresh odds for every
    upcoming match (optionally restricted to one sport). Updates the shared
    job state so /api/refresh_all/status can serve any scope.

    Args:
        sport: restrict to one sport_term (e.g. 'football'), or None for all.
        discover: run discovery first (skip when caller already did it).
        scope_label: human-readable scope tag (e.g. "sport:basketball")."""
    if discover:
        res = _scrape_discovery()
        if res.get("error"):
            with _refresh_all_lock:
                _refresh_all_job["error"] = res["error"]

    # only_pre_kickoff: skip matches that have already kicked off. Their
    # markets are suspended at every operator, TOTO removes the event from
    # /search post-kickoff, and the chrome dump fails or returns a stale
    # page. Just leave the last pre-kickoff snapshot in place.
    matches = db.list_matches(
        only_pre_kickoff=True,
        sport_league_pairs=_enabled_sport_league_pairs(),
        sport=sport,
    )
    with _refresh_all_lock:
        _refresh_all_job["total"] = len(matches)
        _refresh_all_job["completed"] = 0
        _refresh_all_job["failed"] = 0
        _refresh_all_job["scope"] = scope_label or ("all" if sport is None else f"sport:{sport}")

    futures = {}
    for m in matches:
        match_dict = {
            "sport":           m.get("sport") or "football",
            "competition":     m["competition"],
            "league_term":     m["league_term"],
            "home":            m["home"],
            "away":            m["away"],
            "kickoff_utc_iso": m["kickoff_utc"],
            "kambi_event_id":  m["kambi_event_id"],
        }
        fut = _pool.submit(_refresh_match_in_background, m["id"], match_dict)
        futures[fut] = m["id"]

    for fut in as_completed(futures):
        try:
            fut.result(timeout=180)
        except Exception:
            with _refresh_all_lock:
                _refresh_all_job["failed"] += 1
        finally:
            with _refresh_all_lock:
                _refresh_all_job["completed"] += 1

    with _refresh_all_lock:
        _refresh_all_job["running"] = False
        _refresh_all_job["finished_at"] = _utc_now_str()

    # Fresh data → recompute the diffs cache so /firsatlar reflects the sweep.
    try:
        _refresh_biggest_diffs_cache()
    except Exception as e:
        # Don't let a cache rebuild failure crash the worker; the next refresh
        # (or a cold-start GET) will recover.
        print(f"[refresh-sweep] diff cache rebuild failed: {e}", flush=True)


def _bulk_refresh_worker():
    """Backwards-compat alias for the systemd timer — full sweep, all sports."""
    _refresh_sweep_worker(sport=None, discover=True, scope_label="all")


# ---------- pages ----------

@app.route("/")
def index():
    """Serve the built React app. If the bundle isn't there, point the user
    at the dev server."""
    if not os.path.isfile(os.path.join(_DIST, "index.html")):
        return (
            "<h1>Kinda Bet</h1>"
            "<p>Frontend not built yet. Run <code>cd web && npm install && "
            "npm run build</code>, or in dev: <code>cd web && npm run dev</code> "
            "and visit http://127.0.0.1:5173.</p>",
            503,
        )
    return send_from_directory(_DIST, "index.html")


# ---------- API ----------

# Per-sport primary "winner" market — the head-to-head row we put inline on
# the home card. Football/handball are 3-way (1/X/2); UFC, tennis, basketball,
# volleyball are 2-way (1/2). Sports without a clear primary fall back to None
# and the home card just shows the market-count chip.
_PRIMARY_MARKET_BY_SPORT = {
    "football":         "MATCH_RESULT_FT",
    "handball":         "HANDBALL_RESULT_FT",
    "basketball":       "BASKETBALL_MONEYLINE",
    "ufc_mma":          "MMA_BOUT_RESULT",
    "tennis":           "MATCH_WINNER",
    "volleyball":       "MATCH_WINNER",
    "boxing":           "MATCH_WINNER",
    "ice_hockey":       "HOCKEY_HANDICAP_3WAY",
}


@app.route("/api/matches")
def api_matches():
    """Matches grouped by competition. ?sync=1 re-discovers from Kambi."""
    if request.args.get("sync") == "1":
        for m in scrapers.discover_matches(_enabled_competitions()):
            db.upsert_match(m)
    matches    = db.list_matches(only_upcoming=True, sport_league_pairs=_enabled_sport_league_pairs())
    # Pull headline odds for every distinct primary market in one pass per
    # market — covers all enabled sports without N+1 queries.
    primary_by_sport = {(m.get("sport") or "football"): _PRIMARY_MARKET_BY_SPORT.get(m.get("sport") or "football")
                        for m in matches}
    hl_by_market = {}
    for mk in {v for v in primary_by_sport.values() if v}:
        hl_by_market[mk] = db.headline_odds(scrapers.REFERENCE_OPERATOR, mk)
    hl_ou25    = db.headline_odds(scrapers.REFERENCE_OPERATOR, "OVER_UNDER_FT@2.5")
    mkt_counts = db.market_counts(scrapers.REFERENCE_OPERATOR)
    grouped = defaultdict(list)
    for m in matches:
        sport = (m.get("sport") or "football").lower()
        primary_market = _PRIMARY_MARKET_BY_SPORT.get(sport)
        m["last_refresh"]    = db.last_refresh(m["id"])
        m["headline_odds"]   = (hl_by_market.get(primary_market, {}).get(m["id"]) or None) if primary_market else None
        m["headline_market"] = primary_market
        m["over_under_2_5"]  = hl_ou25.get(m["id"]) or None
        m["market_count"]    = mkt_counts.get(m["id"], 0)
        m["logo_url"]        = scrapers.league_logo_url(m.get("league_term"))
        m["sport_name_tr"]   = scrapers._kambi_sport_tr(m.get("sport") or "football")
        grouped[m["competition"]].append(m)
    # Preserve our preferred competition order from scrapers.COMPETITIONS
    order = [c["display_name"] for c in scrapers.COMPETITIONS]
    leagues = []
    for comp in order:
        if comp in grouped:
            leagues.append({"competition": comp, "matches": grouped.pop(comp)})
    # Any unexpected competition (e.g. from old data) goes at the end
    for comp, ms in grouped.items():
        leagues.append({"competition": comp, "matches": ms})
    return jsonify({
        "leagues": leagues,
        "operators": [name for name, _l, _f, _r in scrapers.OPERATORS],
        "reference_operator": scrapers.REFERENCE_OPERATOR,
    })


# Display order for canonical market families. Anything not listed sorts
# after this in alphabetical order on market_key. Multi-sport: each sport's
# "primary" (head-to-head) market goes first, then totals, then handicaps.
_MARKET_ORDER = [
    # Football
    "MATCH_RESULT_FT",
    "DOUBLE_CHANCE_FT",
    "BTTS_FT",
    "BTTS_1H",
    "OVER_UNDER_FT",
    "HTFT_FT",
    "HANDICAP_FT",
    "HANDICAP_3WAY_FT",
    # Basketball
    "BASKETBALL_MONEYLINE",
    "BASKETBALL_TOTAL",
    "BASKETBALL_SPREAD",
    # Ice Hockey
    "HOCKEY_HANDICAP_3WAY",
    "HOCKEY_TOTAL",
    "HOCKEY_PUCK_LINE",
    "HOCKEY_HANDICAP_3WAY_RT",
    "HOCKEY_TOTAL_RT",
    "HOCKEY_PUCK_LINE_RT",
    # Tennis / Volleyball / MMA share the no-draw winner
    "MATCH_WINNER",
    "TENNIS_SET_HANDICAP",
    "TENNIS_TOTAL_GAMES",
    "TENNIS_GAME_HANDICAP",
    "VOLLEYBALL_TOTAL_SETS",
    "VOLLEYBALL_SET_HANDICAP",
    "VOLLEYBALL_TOTAL_POINTS",
    # Handball
    "HANDBALL_RESULT_FT",
    "HANDBALL_DOUBLE_CHANCE",
    "HANDBALL_DNB",
    "HANDBALL_HANDICAP",
    "HANDBALL_HANDICAP_3WAY",
    "HANDBALL_TOTAL",
    # Baseball
    "BASEBALL_TOTAL_RUNS",
    "BASEBALL_RUN_LINE",
    # MMA
    "MMA_BOUT_RESULT",
    "MMA_TOTAL_ROUNDS",
    "MMA_METHOD",
    "MMA_DISTANCE",
]

def _market_sort_key(market_key):
    head = market_key.split("@", 1)[0]
    try:
        bucket = _MARKET_ORDER.index(head)
    except ValueError:
        bucket = len(_MARKET_ORDER)
    line_str = market_key.split("@", 1)[1] if "@" in market_key else ""
    try:
        line_val = float(line_str)
    except ValueError:
        line_val = 0.0
    return (bucket, head, line_val, market_key)


# Selection display order within a market family.
_SELECTION_ORDER = {
    # Football
    "MATCH_RESULT_FT":         ["1", "X", "2"],
    "MATCH_RESULT_HT":         ["1", "X", "2"],
    "MATCH_RESULT_2H":         ["1", "X", "2"],
    "DOUBLE_CHANCE_FT":        ["1X", "12", "X2"],
    "BTTS_FT":                 ["YES", "NO"],
    "BTTS_1H":                 ["YES", "NO"],
    "HTFT_FT":                 ["1/1", "X/1", "X/X", "2/2", "1/X", "X/2", "2/X", "2/1", "1/2"],
    "OVER_UNDER_FT":           ["OVER", "UNDER"],
    "OVER_UNDER_1H":           ["OVER", "UNDER"],
    "OVER_UNDER_2H":           ["OVER", "UNDER"],
    # Generic OU-shaped markets — same pattern across sports.
    "BASKETBALL_TOTAL":        ["OVER", "UNDER"],
    "HOCKEY_TOTAL":            ["OVER", "UNDER"],
    "HOCKEY_TOTAL_RT":         ["OVER", "UNDER"],
    "TENNIS_TOTAL_GAMES":      ["OVER", "UNDER"],
    "VOLLEYBALL_TOTAL_POINTS": ["OVER", "UNDER"],
    "VOLLEYBALL_TOTAL_SETS":   ["OVER", "UNDER"],
    "HANDBALL_TOTAL":          ["OVER", "UNDER"],
    "BASEBALL_TOTAL_RUNS":     ["OVER", "UNDER"],
    "MMA_TOTAL_ROUNDS":        ["OVER", "UNDER"],
    "MMA_DISTANCE":            ["YES", "NO"],
    # 3-way handicap (1/X/2) — non-football too
    "HOCKEY_HANDICAP_3WAY":    ["1", "X", "2"],
    "HOCKEY_HANDICAP_3WAY_RT": ["1", "X", "2"],
    "HANDBALL_RESULT_FT":      ["1", "X", "2"],
    "HANDBALL_HANDICAP_3WAY":  ["1", "X", "2"],
    "HANDBALL_DOUBLE_CHANCE":  ["1X", "12", "X2"],
}

def _selection_sort_key(market_key, selection_key):
    head = market_key.split("@", 1)[0]
    order = _SELECTION_ORDER.get(head, [])
    try:
        return (order.index(selection_key), selection_key)
    except ValueError:
        return (len(order), selection_key)


# Markets we never surface, even if they're still active in the DB from
# pre-blocklist refreshes. Substring match against the canonical or fallback
# market_key (case-insensitive) so it covers ASIAN_HANDICAP_FT@-1.5,
# TOTO_ASIAN_HANDICAP@…, KAMBI_<id>@… for blocked criteria, etc.
_BLOCKED_MARKET_FRAGMENTS = ("ASIAN_HANDICAP", "ASIAN_TOTAL",
                             "ASIAN_OVER_UNDER", "CORRECT_SCORE")

# Football Alt/Üst: surface only the canonical 2.5 line. All other lines
# (1.5/3.5/…) and all half/team-specific OU variants are filtered out so the
# UI shows a single, comparable goal-totals market. Non-football totals
# (BASKETBALL_TOTAL, HOCKEY_TOTAL, MMA_TOTAL_ROUNDS, …) are unaffected —
# they're labelled "Toplam …" in TR, not "Alt / Üst".
_OU_ALLOWED_KEY = "OVER_UNDER_FT@2.5"

def _market_is_blocked(market_key):
    if not market_key:
        return False
    up = market_key.upper()
    if any(frag in up for frag in _BLOCKED_MARKET_FRAGMENTS):
        return True
    if up.startswith("OVER_UNDER_") and up != _OU_ALLOWED_KEY:
        return True
    return False


def _localize_market_label(market_key, stored_label):
    """Re-derive the Turkish label from the canonical market_key. Falls back
    to the stored label for non-canonical KAMBI_<id> markets. This keeps the
    UI in Turkish even when old DB rows still carry Dutch/English labels
    that were captured before the localization was added."""
    head = market_key.split("@", 1)[0]
    return scrapers.MARKET_LABELS_TR.get(head) or stored_label or head

def _localize_selection_label(selection_key, stored_label):
    return scrapers.SELECTION_LABELS_TR.get(selection_key) or stored_label or selection_key


def _build_market_view(rows, reference_operator, all_operators=None):
    """Pivot flat odds rows into the nested market → selection → operator shape
    the frontend renders. Operators that DON'T have data for a given
    (market, selection) get an injected placeholder entry with a `status`
    code so the UI can render a small "maç yok / market yok / hata" pill
    instead of a meaningless dash."""
    if all_operators is None:
        all_operators = []

    rows = [r for r in rows if not _market_is_blocked(r.get("market_key"))]

    # Per-operator state, derived in one pass:
    #   op_with_data[op]    — at least one ok=1 odd for this match
    #   op_markets[op]      — set of market_keys the operator had data for
    #   op_error_note[op]   — the most informative note from a failed row
    op_with_data = set()
    op_markets   = defaultdict(set)
    op_error_note = {}
    for r in rows:
        op = r["operator"]
        if r.get("ok") and r.get("odd") is not None:
            op_with_data.add(op)
            op_markets[op].add(r["market_key"])
        elif r.get("note"):
            op_error_note[op] = r["note"]

    # Pivot real rows.
    markets = {}
    for r in rows:
        mk = r["market_key"]
        sk = r["selection_key"]
        m = markets.setdefault(mk, {
            "market_key":   mk,
            "market_label": _localize_market_label(mk, r["market_label"]),
            "line":         r.get("line"),
            "selections":   {},
        })
        s = m["selections"].setdefault(sk, {
            "selection_key":   sk,
            "selection_label": _localize_selection_label(sk, r["selection_label"]),
            "ops":             {},
        })
        # Skip placeholder rows (ok=0, odd=None) — they carry an op-level
        # diagnostic note that we already captured in op_error_note above.
        if not r.get("ok") or r.get("odd") is None:
            continue
        s["ops"][r["operator"]] = {
            "operator":      r["operator"],
            "odd":           r["odd"],
            "ok":            True,
            "note":          r.get("note"),
            "taken_at":      r.get("taken_at"),
            "status":        "ok",
            # Change-detection fields — surface what the prior value was so
            # the UI can flash green/red when this odd just shifted, and
            # gate the sparkline-on-hover behavior to cells that actually
            # have history (change_count > 1).
            "prev_odd":      r.get("prev_odd"),
            "prev_taken_at": r.get("prev_taken_at"),
            "change_count":  r.get("change_count") or 1,
        }

    out_markets = []
    for mk, m in markets.items():
        kept_selections = []
        for sk, s in m["selections"].items():
            ref = s["ops"].get(reference_operator)
            # Drop the entire selection if the reference operator has no odd
            # for it — we anchor everything to 711's catalog.
            if not ref or ref["odd"] is None:
                continue
            ref_odd = ref["odd"]
            ops_list = []
            for op_name, op in s["ops"].items():
                diff_pct = None if op["odd"] is None else (op["odd"] - ref_odd) / ref_odd * 100.0
                ops_list.append({**op, "diff_pct": diff_pct})

            # Inject placeholder entries for operators that have NO data here
            # so the UI can render a status pill explaining why.
            present = {o["operator"] for o in ops_list}
            for op in all_operators:
                if op in present:
                    continue
                if op not in op_with_data:
                    # Couldn't scrape this match at all (event not found / scrape error).
                    status = "na_error" if op_error_note.get(op) else "na_match"
                elif mk not in op_markets[op]:
                    # Match scraped, but this market wasn't in the operator's catalog.
                    status = "na_market"
                else:
                    # Market was in catalog but this specific selection wasn't —
                    # rare but possible (e.g. handicap home/away naming mismatch).
                    status = "na_selection"
                ops_list.append({
                    "operator": op,
                    "odd":      None,
                    "ok":       False,
                    "note":     op_error_note.get(op),
                    "taken_at": None,
                    "diff_pct": None,
                    "status":   status,
                })
            ops_list.sort(key=lambda o: o["operator"])
            kept_selections.append({
                "selection_key":   sk,
                "selection_label": s["selection_label"],
                "ref_odd":         ref_odd,
                "operators":       ops_list,
            })
        if not kept_selections:
            continue
        kept_selections.sort(key=lambda s: _selection_sort_key(mk, s["selection_key"]))
        out_markets.append({
            "market_key":   mk,
            "market_label": m["market_label"],
            "line":         m["line"],
            "selections":   kept_selections,
        })

    out_markets.sort(key=lambda m: _market_sort_key(m["market_key"]))
    return out_markets


@app.route("/api/match/<int:match_id>")
def api_match(match_id):
    m = db.get_match(match_id)
    if not m:
        return jsonify({"error": "not found"}), 404
    rows = db.latest_odds(match_id)
    all_ops = [name for name, _l, _f, _r in scrapers.OPERATORS]
    markets = _build_market_view(rows, scrapers.REFERENCE_OPERATOR, all_ops)
    m["logo_url"] = scrapers.league_logo_url(m.get("league_term"))
    return jsonify({
        "match":              m,
        "reference_operator": scrapers.REFERENCE_OPERATOR,
        "operators":          [name for name, _l, _f, _r in scrapers.OPERATORS],
        "markets":            markets,
        "operator_status":    db.operator_status(match_id),
        "last_refresh":       db.last_refresh(match_id),
    })


@app.route("/api/match/<int:match_id>/history")
def api_match_history(match_id):
    """Return the odds time-series for charting. Optional filters:
      ?operator=711.nl&market_key=MATCH_RESULT_FT&selection_key=1
    Each row is a change-point (insert_snapshots only writes when the
    value differs from the previous snapshot)."""
    operator      = request.args.get("operator") or None
    market_key    = request.args.get("market_key") or None
    selection_key = request.args.get("selection_key") or None
    try:
        limit = max(1, min(10000, int(request.args.get("limit", 2000))))
    except ValueError:
        limit = 2000
    rows = db.odds_history(match_id, operator=operator,
                           market_key=market_key,
                           selection_key=selection_key, limit=limit)
    # Localize labels on the way out so the frontend doesn't have to
    for r in rows:
        r["market_label"]    = _localize_market_label(r["market_key"], r.get("market_label"))
        r["selection_label"] = _localize_selection_label(r["selection_key"], r.get("selection_label"))
    return jsonify({"items": rows, "count": len(rows)})


@app.route("/api/match/<int:match_id>/refresh", methods=["POST"])
def api_refresh(match_id):
    """Trigger a fresh scrape for one match across all 4 operators."""
    m = db.get_match(match_id)
    if not m:
        return jsonify({"error": "not found"}), 404
    # Skip live / past-kickoff matches: every operator has the markets
    # suspended once the whistle blows, and TOTO removes the event page
    # entirely. Hand back the existing snapshot rather than spinning up
    # a chrome process to retrieve nothing.
    try:
        ko = datetime.fromisoformat((m.get("kickoff_utc") or "").replace("Z", "+00:00"))
        if ko <= datetime.now(timezone.utc):
            rows = db.latest_odds(match_id)
            all_ops = [name for name, _l, _f, _r in scrapers.OPERATORS]
            markets = _build_market_view(rows, scrapers.REFERENCE_OPERATOR, all_ops)
            return jsonify({
                "ok":              True,
                "skipped":         "kickoff_passed",
                "rows":            len(rows),
                "by_operator":     {op: {"with_odds": 0, "total": 0} for op in all_ops},
                "markets":         markets,
                "operator_status": db.operator_status(match_id),
                "last_refresh":    db.last_refresh(match_id),
            })
    except (ValueError, TypeError):
        pass  # malformed kickoff — fall through and refresh anyway

    match_dict = {
        "sport":           m.get("sport") or "football",
        "competition":     m["competition"],
        "league_term":     m["league_term"],
        "home":            m["home"],
        "away":            m["away"],
        "kickoff_utc_iso": m["kickoff_utc"],
        "kambi_event_id":  m["kambi_event_id"],
        "toto_event_id":   m.get("toto_event_id"),
    }
    fut = _pool.submit(_refresh_match_in_background, match_id, match_dict)
    rows = fut.result(timeout=180)
    by_op = defaultdict(lambda: {"with_odds": 0, "total": 0})
    for r in rows:
        by_op[r["operator"]]["total"] += 1
        if r.get("ok"):
            by_op[r["operator"]]["with_odds"] += 1
    # This match's odds just changed → refresh the diffs cache so Fırsatlar
    # reflects the new prices instantly.
    try:
        _refresh_biggest_diffs_cache()
    except Exception:
        pass
    return jsonify({
        "ok":              True,
        "rows":            len(rows),
        "by_operator":     by_op,
        "markets":         _build_market_view(
                              db.latest_odds(match_id),
                              scrapers.REFERENCE_OPERATOR,
                              [name for name, _l, _f, _r in scrapers.OPERATORS],
                          ),
        "operator_status": db.operator_status(match_id),
    })


# ---------- league settings ----------

@app.route("/api/leagues/available")
def api_leagues_available():
    """Discover every (sport, league) tuple Kambi exposes for our reference
    brand, enriched with TheSportsDB badge URLs for football leagues (other
    sports get null). Cached in-memory + on-disk, see scrapers.py.

    `?force=1` invalidates both caches before re-walking — surfaced via the
    Ayarlar "Sport Kategorilerini Yenile" button so users can pick up newly
    added Kambi leagues without waiting for the 30min/24h TTLs."""
    brand_ref = scrapers.KAMBI_BRANDS[scrapers.REFERENCE_OPERATOR]
    if request.args.get("force") == "1":
        scrapers.kambi_invalidate_sports_cache()
    leagues = scrapers.kambi_list_all_sports(brand_ref)
    if not leagues:
        leagues = scrapers.kambi_list_all_sports(scrapers.KAMBI_BRANDS[scrapers.FALLBACK_OPERATOR])
    # Logo enrichment is football-only (TheSportsDB is soccer-focused).
    for lg in leagues:
        if lg.get("sport_term") == "football":
            lg["logo_url"] = scrapers.league_logo_url(lg.get("league_term"))
        else:
            lg["logo_url"] = None
    return jsonify({"leagues": leagues})


@app.route("/api/settings/leagues")
def api_settings_leagues_get():
    saved = db.get_setting("enabled_leagues")
    if not saved or not isinstance(saved, list) or not saved:
        saved = [{"sport_term": c["sport_term"], "display_name": c["display_name"],
                  "league_term": c["league_term"]} for c in scrapers.COMPETITIONS]
    # Re-resolve display_names from current taxonomy. Lookup is keyed by
    # (sport_term, league_term) — keying only on league_term collides for
    # leagues that exist under multiple sports (champions_league appears
    # in football, handball, AND volleyball).
    try:
        all_leagues = scrapers.kambi_list_all_sports(
            scrapers.KAMBI_BRANDS[scrapers.REFERENCE_OPERATOR])
        by_pair = {(lg["sport_term"], lg["league_term"]): lg for lg in all_leagues}
    except Exception:
        by_pair = {}
    enriched = []
    for item in saved:
        term = (item or {}).get("league_term")
        if not term:
            continue
        sport = (item.get("sport_term") or "football").lower()
        meta = by_pair.get((sport, term)) or {}
        enriched.append({
            "sport_term":    sport,
            "league_term":   term,
            "display_name":  meta.get("display_name") or item.get("display_name") or term,
        })
    return jsonify({"enabled": enriched})


@app.route("/api/settings/leagues", methods=["POST"])
def api_settings_leagues_set():
    """Persist the user's selected leagues. Body: {"enabled": [{sport_term, display_name, league_term}, ...]}.
    Accepts an empty list (clears all leagues — site shows "no matches"). The
    same league_term may legitimately appear under multiple sports, so dedup
    is on the (sport, term) pair, not term alone."""
    body = request.get_json(silent=True) or {}
    enabled = body.get("enabled")
    if not isinstance(enabled, list):
        return jsonify({"error": "enabled must be a list"}), 400
    try:
        all_leagues = scrapers.kambi_list_all_sports(
            scrapers.KAMBI_BRANDS[scrapers.REFERENCE_OPERATOR])
        by_pair = {(lg["sport_term"], lg["league_term"]): lg for lg in all_leagues}
    except Exception:
        by_pair = {}
    cleaned = []
    seen_pairs = set()
    for item in enabled:
        if not isinstance(item, dict):
            continue
        term = (item.get("league_term") or "").strip()
        if not term:
            continue
        sport = (item.get("sport_term") or "football").lower()
        pair = (sport, term)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        meta = by_pair.get(pair) or {}
        name = meta.get("display_name") or (item.get("display_name") or "").strip() or term
        cleaned.append({"sport_term": sport, "display_name": name, "league_term": term})
    db.set_setting("enabled_leagues", cleaned)
    # Removed leagues should drop out of /firsatlar instantly — recompute the
    # diffs cache against the new enabled-leagues filter (cheap; ~latest_odds
    # join). The next bulk refresh will recompute again, but waiting for it
    # would leave stale entries on the page for up to an hour.
    try:
        _refresh_biggest_diffs_cache()
    except Exception as e:
        print(f"[settings] diff cache rebuild failed: {e}", flush=True)
    return jsonify({"ok": True, "enabled": cleaned})


# ---------- biggest cross-operator price differences ----------

# Cache the full sorted diff list. Recomputed only when fresh odds land
# (after a bulk refresh or a single-match refresh) — every other GET hits
# this in-memory copy and returns instantly.
_biggest_diffs_lock = threading.Lock()
_biggest_diffs_cache = {
    "items": None,           # full sorted list, or None if uncomputed
    "total_evaluated": 0,
    "computed_at": None,     # UTC string
}

def _compute_biggest_diffs():
    """Heavy pass: scan latest_odds, group, compute per-tuple spreads, sort.
    Returns (sorted_list, total_groups_evaluated).

    Filters: only rows belonging to currently-enabled leagues (so removing a
    league in Settings drops its matches from /firsatlar immediately) and
    only canonical markets (KAMBI_/TOTO_-native fallbacks can't align
    cross-operator)."""
    enabled_pairs = set(_enabled_sport_league_pairs())
    rows = db.all_latest_odds()
    groups = defaultdict(list)
    for r in rows:
        if enabled_pairs:
            pair = ((r.get("sport") or "football"), r.get("league_term") or "")
            if pair not in enabled_pairs:
                continue
        mk = r["market_key"]
        if mk.startswith("KAMBI_") or mk.startswith("TOTO_"):
            continue
        if _market_is_blocked(mk):
            continue
        groups[(r["match_id"], mk, r["selection_key"])].append(r)

    out = []
    for (match_id, mk, sk), items in groups.items():
        latest_per_op = {}
        for it in items:
            if it["odd"] is None:
                continue
            cur = latest_per_op.get(it["operator"])
            if not cur or (it.get("taken_at") or "") > (cur.get("taken_at") or ""):
                latest_per_op[it["operator"]] = it
        ops_odds = [(it["operator"], it["odd"]) for it in latest_per_op.values()]
        if len({op for op, _ in ops_odds}) < 2:
            continue
        best_op,  best_odd  = max(ops_odds, key=lambda x: x[1])
        worst_op, worst_odd = min(ops_odds, key=lambda x: x[1])
        if worst_odd <= 0 or best_op == worst_op:
            continue
        diff_pct = (best_odd - worst_odd) / worst_odd * 100.0
        head = next(iter(latest_per_op.values()))
        out.append({
            "match_id":         match_id,
            "home":             head["home"],
            "away":             head["away"],
            "competition":      head["competition"],
            "logo_url":         scrapers.league_logo_url(head.get("league_term")),
            "kickoff_utc":      head["kickoff_utc"],
            "market_key":       mk,
            "market_label":     _localize_market_label(mk, head["market_label"]),
            "line":             head["line"],
            "selection_key":    sk,
            "selection_label":  _localize_selection_label(sk, head["selection_label"]),
            "best_operator":    best_op,
            "best_odd":         best_odd,
            "worst_operator":   worst_op,
            "worst_odd":        worst_odd,
            "diff_pct":         diff_pct,
            "all_operators":    sorted([{"operator": op, "odd": odd} for op, odd in ops_odds],
                                       key=lambda x: -x["odd"]),
        })

    out.sort(key=lambda x: -x["diff_pct"])
    return out, len(groups)


def _refresh_biggest_diffs_cache():
    """Recompute and atomically replace the cache. Safe to call from any
    thread; safe to call repeatedly (it's just expensive)."""
    items, total = _compute_biggest_diffs()
    with _biggest_diffs_lock:
        _biggest_diffs_cache["items"] = items
        _biggest_diffs_cache["total_evaluated"] = total
        _biggest_diffs_cache["computed_at"] = _utc_now_str()


@app.route("/api/biggest_diffs")
def api_biggest_diffs():
    """Rank (match, market, selection) tuples by % spread, served from cache."""
    try:
        limit = max(1, min(100, int(request.args.get("limit", 10))))
    except ValueError:
        limit = 10

    with _biggest_diffs_lock:
        cached_items = _biggest_diffs_cache["items"]
        cached_total = _biggest_diffs_cache["total_evaluated"]
        cached_when  = _biggest_diffs_cache["computed_at"]

    # Cold start (gunicorn just booted) — lazy-compute once. After this first
    # call the cache stays warm until the next refresh hook fires.
    if cached_items is None:
        _refresh_biggest_diffs_cache()
        with _biggest_diffs_lock:
            cached_items = _biggest_diffs_cache["items"]
            cached_total = _biggest_diffs_cache["total_evaluated"]
            cached_when  = _biggest_diffs_cache["computed_at"]

    return jsonify({
        "items":           (cached_items or [])[:limit],
        "total_evaluated": cached_total,
        "computed_at":     cached_when,
    })


# ---------- bulk refresh ----------

def _start_sweep_job(sport, scope_label):
    """Atomically claim the job slot and spawn a coordinator thread. Returns
    (started: bool, snapshot: dict). If a job is already running, returns
    (False, snapshot) so the caller can surface 'already_running'."""
    with _refresh_all_lock:
        if _refresh_all_job["running"]:
            return False, dict(_refresh_all_job)
        _refresh_all_job.update({
            "running":     True,
            "started_at":  _utc_now_str(),
            "finished_at": None,
            "total":       0,
            "completed":   0,
            "failed":      0,
            "error":       None,
            "scope":       scope_label,
        })
        snapshot = dict(_refresh_all_job)
    threading.Thread(
        target=_refresh_sweep_worker,
        kwargs={"sport": sport, "discover": True, "scope_label": scope_label},
        name=f"sweep-{scope_label}",
        daemon=True,
    ).start()
    return True, snapshot


@app.route("/api/refresh_all", methods=["POST"])
def api_refresh_all():
    """Kick off a background job that re-discovers matches and refreshes odds
    for every match across every enabled sport. Returns immediately; clients
    poll /api/refresh_all/status."""
    started, snap = _start_sweep_job(sport=None, scope_label="all")
    return jsonify({"ok": True, "already_running": not started, **snap})


@app.route("/api/refresh_sport/<sport_term>", methods=["POST"])
def api_refresh_sport(sport_term):
    """Refresh odds for every enabled match in one sport. Cheaper than
    /api/refresh_all when only one sport's data is stale (e.g. user just
    enabled basketball and wants to see odds without waiting for football).
    Async; clients poll /api/refresh_all/status."""
    sport = (sport_term or "").lower().strip()
    if not sport:
        return jsonify({"error": "sport_term required"}), 400
    started, snap = _start_sweep_job(sport=sport, scope_label=f"sport:{sport}")
    return jsonify({"ok": True, "already_running": not started, **snap})


@app.route("/api/refresh_discovery", methods=["POST"])
def api_refresh_discovery():
    """Re-discover matches (no odds refresh). Synchronous (~1-2s with the
    sport-tree cache warm); useful when the user just enabled new leagues
    in Ayarlar and wants the new fixtures to appear immediately without
    waiting for a full sweep."""
    res = _scrape_discovery()
    return jsonify({"ok": res.get("error") is None, **res})


@app.route("/api/refresh_all/status")
def api_refresh_all_status():
    with _refresh_all_lock:
        out = dict(_refresh_all_job)
    out["auto_refresh_interval_seconds"] = AUTO_REFRESH_INTERVAL_SECONDS
    # Compute the next scheduled fire time from the most recent successful
    # finish. Frontend renders a relative countdown.
    finished = out.get("finished_at")
    out["next_scheduled_at"] = None
    if finished:
        try:
            dt = datetime.fromisoformat(finished.replace(" ", "T")).replace(tzinfo=timezone.utc)
            nxt = dt + timedelta(seconds=AUTO_REFRESH_INTERVAL_SECONDS)
            out["next_scheduled_at"] = nxt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return jsonify(out)


# ---------- SPA catch-all (must be last) ----------

@app.route("/<path:path>")
def spa_catchall(path):
    """Serve static assets if they exist (e.g. /kinda.png, /assets/index.js).
    Otherwise hand back index.html so the React router can take it from there."""
    full = os.path.join(_DIST, path)
    if os.path.isfile(full):
        return send_from_directory(_DIST, path)
    if not os.path.isfile(os.path.join(_DIST, "index.html")):
        return ("Frontend not built. Run `cd web && npm run build`.", 503)
    return send_from_directory(_DIST, "index.html")


if __name__ == "__main__":
    print("Kinda Bet on http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
