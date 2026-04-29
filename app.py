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
}

def _utc_now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _bulk_refresh_worker():
    """Coordinator thread: discover matches first, then submit each to the
    pool and update progress as futures complete."""
    try:
        for m in scrapers.discover_matches():
            db.upsert_match(m)
    except Exception as e:
        with _refresh_all_lock:
            _refresh_all_job["error"] = f"discover failed: {e}"

    matches = db.list_matches(only_upcoming=True)
    with _refresh_all_lock:
        _refresh_all_job["total"] = len(matches)
        _refresh_all_job["completed"] = 0
        _refresh_all_job["failed"] = 0

    futures = {}
    for m in matches:
        match_dict = {
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
        print(f"[bulk-refresh] diff cache rebuild failed: {e}", flush=True)


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

@app.route("/api/matches")
def api_matches():
    """Matches grouped by competition. ?sync=1 re-discovers from Kambi."""
    if request.args.get("sync") == "1":
        for m in scrapers.discover_matches():
            db.upsert_match(m)
    matches = db.list_matches(only_upcoming=True)
    headline = db.headline_odds(scrapers.REFERENCE_OPERATOR)  # {match_id: {sel: odd}}
    grouped = defaultdict(list)
    for m in matches:
        m["last_refresh"] = db.last_refresh(m["id"])
        m["headline_odds"] = headline.get(m["id"]) or None
        grouped[m["competition"]].append(m)
    # Preserve our preferred competition order from scrapers.COMPETITIONS
    order = [name for name, _term in scrapers.COMPETITIONS]
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


# Display order for canonical market families. Anything not listed sorts after
# this in alphabetical order on market_key.
_MARKET_ORDER = [
    "MATCH_RESULT_FT",
    "DOUBLE_CHANCE_FT",
    "BTTS_FT",
    "OVER_UNDER_FT",
    "HANDICAP_FT",
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
    "MATCH_RESULT_FT":  ["1", "X", "2"],
    "DOUBLE_CHANCE_FT": ["1X", "12", "X2"],
    "BTTS_FT":          ["YES", "NO"],
    "OVER_UNDER_FT":    ["OVER", "UNDER"],
}

def _selection_sort_key(market_key, selection_key):
    head = market_key.split("@", 1)[0]
    order = _SELECTION_ORDER.get(head, [])
    try:
        return (order.index(selection_key), selection_key)
    except ValueError:
        return (len(order), selection_key)


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
            "operator": r["operator"],
            "odd":      r["odd"],
            "ok":       True,
            "note":     r.get("note"),
            "taken_at": r.get("taken_at"),
            "status":   "ok",
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
    match_dict = {
        "competition":     m["competition"],
        "league_term":     m["league_term"],
        "home":            m["home"],
        "away":            m["away"],
        "kickoff_utc_iso": m["kickoff_utc"],
        "kambi_event_id":  m["kambi_event_id"],
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
    Returns (sorted_list, total_groups_evaluated)."""
    rows = db.all_latest_odds()
    groups = defaultdict(list)
    for r in rows:
        mk = r["market_key"]
        if mk.startswith("KAMBI_") or mk.startswith("TOTO_"):
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

@app.route("/api/refresh_all", methods=["POST"])
def api_refresh_all():
    """Kick off a background job that re-discovers matches and refreshes odds
    for every match. Returns immediately; clients should poll /status."""
    with _refresh_all_lock:
        if _refresh_all_job["running"]:
            return jsonify({"ok": True, "already_running": True, **_refresh_all_job})
        _refresh_all_job.update({
            "running":     True,
            "started_at":  _utc_now_str(),
            "finished_at": None,
            "total":       0,
            "completed":   0,
            "failed":      0,
            "error":       None,
        })
    threading.Thread(target=_bulk_refresh_worker, name="bulk-refresh", daemon=True).start()
    with _refresh_all_lock:
        return jsonify({"ok": True, "already_running": False, **_refresh_all_job})


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
