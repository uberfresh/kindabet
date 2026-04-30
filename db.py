"""SQLite layer for Kinda Bet.

Schema supports arbitrary markets (1X2, handicaps, totals, BTTS, …). Every
refresh either inserts a new row (when the odd has changed since last seen)
or just bumps `last_seen_at` on the existing row (when unchanged). The
`is_active` flag on each row reflects whether that market_key+selection_key
was emitted in the operator's MOST RECENT refresh — every insert deactivates
all prior rows for that match+operator first, then reactivates or inserts
per emitted row."""
import os
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "odds.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL DEFAULT 'football',
    competition TEXT NOT NULL,
    league_term TEXT NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    kickoff_utc TEXT NOT NULL,
    kambi_event_id INTEGER UNIQUE,
    discovered_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_matches_kickoff ON matches(kickoff_utc);
CREATE INDEX IF NOT EXISTS idx_matches_comp ON matches(competition);
CREATE INDEX IF NOT EXISTS idx_matches_sport ON matches(sport);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    operator TEXT NOT NULL,
    license TEXT,
    market_key TEXT NOT NULL,
    market_label TEXT NOT NULL,
    selection_key TEXT NOT NULL,
    selection_label TEXT NOT NULL,
    line REAL,
    odd REAL,
    ok INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    taken_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_os_match_time      ON odds_snapshots(match_id, taken_at);
CREATE INDEX IF NOT EXISTS idx_os_match_op_time   ON odds_snapshots(match_id, operator, taken_at);
CREATE INDEX IF NOT EXISTS idx_os_match_market    ON odds_snapshots(match_id, market_key);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Indexes that reference columns added by the migration. Created after the
# ALTER TABLE in init() so they don't fail on first-time schema upgrades.
SCHEMA_POST_MIGRATION = """
CREATE INDEX IF NOT EXISTS idx_os_active          ON odds_snapshots(match_id, is_active, ok);
CREATE INDEX IF NOT EXISTS idx_os_history_lookup  ON odds_snapshots(match_id, operator, market_key, selection_key, taken_at);
"""

def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON;")
    return c

def init():
    with _lock, conn() as c:
        c.executescript(SCHEMA)
        # Idempotent column-level migrations for older deployments.
        cols = {r[1] for r in c.execute("PRAGMA table_info(odds_snapshots)").fetchall()}
        if "is_active" not in cols:
            c.execute("ALTER TABLE odds_snapshots ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
            # All pre-existing rows predate change-detection, so we don't
            # know which were "currently emitted" at any past point. Mark
            # inactive; the next refresh activates whatever's still being
            # emitted.
            c.execute("UPDATE odds_snapshots SET is_active = 0")
        if "last_seen_at" not in cols:
            c.execute("ALTER TABLE odds_snapshots ADD COLUMN last_seen_at TEXT")
        # Multi-sport: every legacy match was football, so backfill the column.
        match_cols = {r[1] for r in c.execute("PRAGMA table_info(matches)").fetchall()}
        if "sport" not in match_cols:
            c.execute("ALTER TABLE matches ADD COLUMN sport TEXT NOT NULL DEFAULT 'football'")
            c.execute("CREATE INDEX IF NOT EXISTS idx_matches_sport ON matches(sport)")
        # Indexes that depend on the migrated columns — safe to run last.
        c.executescript(SCHEMA_POST_MIGRATION)
        c.commit()

def upsert_match(m):
    sport = (m.get("sport") or "football").lower()
    with _lock, conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id FROM matches WHERE kambi_event_id = ?",
                    (m["kambi_event_id"],))
        row = cur.fetchone()
        if row:
            mid = row["id"]
            cur.execute("UPDATE matches SET sport=?, competition=?, league_term=?, home=?, away=?, kickoff_utc=? WHERE id=?",
                        (sport, m["competition"], m["league_term"], m["home"], m["away"],
                         m["kickoff_utc_iso"], mid))
        else:
            cur.execute(
                "INSERT INTO matches (sport, competition, league_term, home, away, kickoff_utc, kambi_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sport, m["competition"], m["league_term"], m["home"], m["away"],
                 m["kickoff_utc_iso"], m["kambi_event_id"]))
            mid = cur.lastrowid
        c.commit()
        return mid

def get_setting(key, default=None):
    """Read a setting. Values are stored as JSON; default is returned on miss."""
    import json as _json
    with _lock, conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not r:
            return default
        try:
            return _json.loads(r["value"])
        except _json.JSONDecodeError:
            return default

def set_setting(key, value):
    """Write a setting. Value is JSON-encoded."""
    import json as _json
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _lock, conn() as c:
        c.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, _json.dumps(value, ensure_ascii=False), now))
        c.commit()


def list_matches(only_upcoming=True, league_terms=None, sport=None):
    """Filter by league_terms list when provided so disabled leagues drop
    out of the live view (matches stay in DB; just hidden). Optional sport
    filter restricts to one sport_term (e.g. 'football')."""
    with _lock, conn() as c:
        where = []
        params = []
        if only_upcoming:
            where.append("datetime(kickoff_utc) >= datetime('now', '-3 hours')")
        if league_terms:
            where.append(f"league_term IN ({','.join('?' * len(league_terms))})")
            params.extend(league_terms)
        if sport:
            where.append("sport = ?")
            params.append(sport)
        sql = "SELECT * FROM matches"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY sport, competition, kickoff_utc"
        rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

def get_match(match_id):
    with _lock, conn() as c:
        r = c.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        return dict(r) if r else None

def insert_snapshots(match_id, rows):
    """Persist a refresh. For each (operator, market_key, selection_key):
      * If the most recent stored row has the same `odd` AND `ok` flag,
        we just bump its `last_seen_at` and re-activate it (no new row).
      * Otherwise, we insert a fresh row (this is the change-history).
    Any row that was active for this match+operator before this refresh
    but isn't re-emitted now is left deactivated, so retired markets
    fall out of the live view automatically."""
    if not rows:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Group incoming rows by operator so we can deactivate one operator's
    # batch atomically without disturbing others mid-refresh.
    by_op = defaultdict(list)
    for r in rows:
        by_op[r.get("operator")].append(r)

    with _lock, conn() as c:
        for operator, op_rows in by_op.items():
            # Step 1 — deactivate all current rows for this match+operator.
            c.execute(
                "UPDATE odds_snapshots SET is_active = 0 "
                "WHERE match_id = ? AND operator = ? AND is_active = 1",
                (match_id, operator))

            # Step 2 — pre-fetch the most recent row per (market, selection)
            # so we can decide insert-vs-reactivate without per-row queries.
            latest = {}
            for prev in c.execute("""
                SELECT s.id, s.market_key, s.selection_key, s.odd, s.ok
                FROM odds_snapshots s
                JOIN (
                    SELECT market_key, selection_key, MAX(id) AS mx
                    FROM odds_snapshots
                    WHERE match_id = ? AND operator = ?
                    GROUP BY market_key, selection_key
                ) t ON s.id = t.mx
            """, (match_id, operator)).fetchall():
                latest[(prev["market_key"], prev["selection_key"])] = prev

            # Step 3 — for each emitted row, reactivate or insert.
            insert_payload = []
            for r in op_rows:
                mk = r.get("market_key") or "UNKNOWN"
                ml = r.get("market_label") or mk or "Markt"
                sk = r.get("selection_key") or "?"
                sl = r.get("selection_label") or sk or "?"
                new_odd = r.get("odd")
                new_ok  = 1 if r.get("ok") else 0
                prev = latest.get((mk, sk))
                if prev and prev["odd"] == new_odd and prev["ok"] == new_ok:
                    # Same value as last time — reactivate the existing row,
                    # bump last_seen_at, and labels (they may have been re-localized).
                    c.execute(
                        "UPDATE odds_snapshots SET is_active = 1, last_seen_at = ?, "
                        "market_label = ?, selection_label = ?, line = ?, note = ? "
                        "WHERE id = ?",
                        (now, ml, sl, r.get("line"), r.get("note"), prev["id"]))
                else:
                    insert_payload.append((
                        match_id, operator, r.get("license"),
                        mk, ml, sk, sl,
                        r.get("line"), new_odd, new_ok, r.get("note"),
                        1, now, now,
                    ))

            if insert_payload:
                c.executemany("""
                    INSERT INTO odds_snapshots
                    (match_id, operator, license, market_key, market_label,
                     selection_key, selection_label, line, odd, ok, note,
                     is_active, taken_at, last_seen_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, insert_payload)
        c.commit()
    return now

def headline_odds(operator, market_key="MATCH_RESULT_FT"):
    """Return {match_id: {selection_key: odd}} — current 1X2 (or any market)
    for every match, single query. Drives the inline quick-scan odds on the
    homepage without N round-trips to /api/match/<id>."""
    with _lock, conn() as c:
        rows = c.execute("""
            SELECT match_id, selection_key, odd
            FROM odds_snapshots
            WHERE operator = ? AND market_key = ?
              AND is_active = 1 AND ok = 1
        """, (operator, market_key)).fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["match_id"], {})[r["selection_key"]] = r["odd"]
        return out

def market_counts(operator):
    """Return {match_id: int} — number of distinct active markets per match
    for the given operator. Drives the "+N" chip on home-page rows."""
    with _lock, conn() as c:
        rows = c.execute("""
            SELECT match_id, COUNT(DISTINCT market_key) AS n
            FROM odds_snapshots
            WHERE operator = ? AND is_active = 1 AND ok = 1
            GROUP BY match_id
        """, (operator,)).fetchall()
        return {r["match_id"]: r["n"] for r in rows}


def latest_odds(match_id):
    """All currently-active rows for a match. is_active=1 means the row was
    emitted by its operator's most recent refresh."""
    with _lock, conn() as c:
        rows = c.execute("""
            SELECT * FROM odds_snapshots
            WHERE match_id = ? AND is_active = 1
            ORDER BY market_key, selection_key, operator
        """, (match_id,)).fetchall()
        return [dict(r) for r in rows]


def odds_history(match_id, operator=None, market_key=None, selection_key=None, limit=2000):
    """Time series of odds rows for charting — every row is a change-point
    (because insert_snapshots only writes a new row when the value changes).
    Filters compose with AND. Ordered by taken_at ASC."""
    sql = """
        SELECT taken_at, last_seen_at, operator, market_key, market_label,
               selection_key, selection_label, line, odd, ok, is_active
        FROM odds_snapshots
        WHERE match_id = ? AND ok = 1 AND odd IS NOT NULL
    """
    params = [match_id]
    if operator:
        sql += " AND operator = ?"
        params.append(operator)
    if market_key:
        sql += " AND market_key = ?"
        params.append(market_key)
    if selection_key:
        sql += " AND selection_key = ?"
        params.append(selection_key)
    sql += " ORDER BY taken_at ASC LIMIT ?"
    params.append(limit)
    with _lock, conn() as c:
        rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

def all_latest_odds():
    """Active odds across every upcoming match, joined with match metadata.

    Returns rows where is_active=1, which means they were emitted by their
    operator's most recent refresh. Stale rows from past refreshes are
    automatically excluded."""
    with _lock, conn() as c:
        rows = c.execute("""
            SELECT s.match_id, s.operator, s.market_key, s.market_label,
                   s.selection_key, s.selection_label, s.line, s.odd, s.taken_at,
                   s.last_seen_at,
                   m.home, m.away, m.competition, m.kickoff_utc, m.league_term
            FROM odds_snapshots s
            JOIN matches m ON m.id = s.match_id
            WHERE s.is_active = 1
              AND s.ok = 1
              AND s.odd IS NOT NULL
              AND datetime(m.kickoff_utc) >= datetime('now', '-3 hours')
        """).fetchall()
        return [dict(r) for r in rows]


def operator_status(match_id):
    """Per-operator summary: how many ok rows, how many total, last refresh."""
    with _lock, conn() as c:
        rows = c.execute("""
            SELECT operator,
                   SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS with_odds,
                   COUNT(*) AS total,
                   MAX(taken_at) AS last_refresh
            FROM odds_snapshots
            WHERE match_id = ?
            GROUP BY operator
            ORDER BY operator
        """, (match_id,)).fetchall()
        return [dict(r) for r in rows]

def last_refresh(match_id):
    with _lock, conn() as c:
        r = c.execute("SELECT MAX(taken_at) AS m FROM odds_snapshots WHERE match_id=?",
                      (match_id,)).fetchone()
        return r["m"] if r else None
