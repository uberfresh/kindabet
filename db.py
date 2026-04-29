"""SQLite layer for Kinda Bet.

Schema supports arbitrary markets (1X2, handicaps, totals, BTTS, …) — every
odds row is keyed by (match, operator, market_key, selection_key) and we
keep an append-only history."""
import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "odds.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    taken_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_os_match_time ON odds_snapshots(match_id, taken_at);
CREATE INDEX IF NOT EXISTS idx_os_match_op_time ON odds_snapshots(match_id, operator, taken_at);
CREATE INDEX IF NOT EXISTS idx_os_match_market ON odds_snapshots(match_id, market_key);
"""

def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON;")
    return c

def init():
    with _lock, conn() as c:
        c.executescript(SCHEMA)

def upsert_match(m):
    with _lock, conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id FROM matches WHERE kambi_event_id = ?",
                    (m["kambi_event_id"],))
        row = cur.fetchone()
        if row:
            mid = row["id"]
            cur.execute("UPDATE matches SET competition=?, league_term=?, home=?, away=?, kickoff_utc=? WHERE id=?",
                        (m["competition"], m["league_term"], m["home"], m["away"],
                         m["kickoff_utc_iso"], mid))
        else:
            cur.execute(
                "INSERT INTO matches (competition, league_term, home, away, kickoff_utc, kambi_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (m["competition"], m["league_term"], m["home"], m["away"],
                 m["kickoff_utc_iso"], m["kambi_event_id"]))
            mid = cur.lastrowid
        c.commit()
        return mid

def list_matches(only_upcoming=True):
    with _lock, conn() as c:
        sql = "SELECT * FROM matches"
        if only_upcoming:
            sql += " WHERE datetime(kickoff_utc) >= datetime('now', '-3 hours')"
        sql += " ORDER BY competition, kickoff_utc"
        rows = c.execute(sql).fetchall()
        return [dict(r) for r in rows]

def get_match(match_id):
    with _lock, conn() as c:
        r = c.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        return dict(r) if r else None

def insert_snapshots(match_id, rows):
    """Append one row per (operator, market_key, selection_key)."""
    if not rows:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    payload = [
        (match_id,
         r.get("operator"),
         r.get("license"),
         r.get("market_key") or "UNKNOWN",
         r.get("market_label") or r.get("market_key") or "Markt",
         r.get("selection_key") or "?",
         r.get("selection_label") or r.get("selection_key") or "?",
         r.get("line"),
         r.get("odd"),
         1 if r.get("ok") else 0,
         r.get("note"),
         now)
        for r in rows
    ]
    with _lock, conn() as c:
        c.executemany(
            "INSERT INTO odds_snapshots (match_id, operator, license, market_key, market_label, "
            "selection_key, selection_label, line, odd, ok, note, taken_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            payload)
        c.commit()
    return now

def headline_odds(operator, market_key="MATCH_RESULT_FT"):
    """Return {match_id: {selection_key: odd}} — latest 1X2 (or any market) for
    every match, single query. Used to render quick-scan odds on the homepage
    without paying N round-trips to /api/match/<id>."""
    with _lock, conn() as c:
        rows = c.execute("""
            SELECT s.match_id, s.selection_key, s.odd
            FROM odds_snapshots s
            JOIN (
                SELECT match_id, selection_key, MAX(taken_at) AS mx
                FROM odds_snapshots
                WHERE operator = ? AND market_key = ? AND ok = 1
                GROUP BY match_id, selection_key
            ) t
              ON s.match_id      = t.match_id
             AND s.selection_key = t.selection_key
             AND s.taken_at      = t.mx
            WHERE s.operator = ? AND s.market_key = ? AND s.ok = 1
        """, (operator, market_key, operator, market_key)).fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["match_id"], {})[r["selection_key"]] = r["odd"]
        return out

def latest_odds(match_id):
    """All rows from each operator's MOST RECENT refresh of this match.

    Keyed on (operator) — see all_latest_odds() for rationale. If an
    operator's last refresh didn't include a given market_key (e.g. that
    market is no longer mapped), it doesn't appear here."""
    with _lock, conn() as c:
        rows = c.execute("""
            SELECT s.* FROM odds_snapshots s
            JOIN (
                SELECT operator, MAX(taken_at) AS mx
                FROM odds_snapshots
                WHERE match_id = ?
                GROUP BY operator
            ) t
              ON s.operator = t.operator
             AND s.taken_at = t.mx
            WHERE s.match_id = ?
            ORDER BY s.market_key, s.selection_key, s.operator
        """, (match_id, match_id)).fetchall()
        return [dict(r) for r in rows]

def all_latest_odds():
    """Latest odds across every upcoming match, joined with match metadata.

    Keyed on (match, operator) — only the rows from each operator's MOST
    RECENT refresh are returned. This is intentional: if a scraper stops
    emitting a particular market_key (e.g. we removed a wrong canonical
    mapping), the stale rows under that key get filtered out automatically
    on the next refresh, instead of poisoning latest_odds forever."""
    with _lock, conn() as c:
        rows = c.execute("""
            SELECT s.match_id, s.operator, s.market_key, s.market_label,
                   s.selection_key, s.selection_label, s.line, s.odd, s.taken_at,
                   m.home, m.away, m.competition, m.kickoff_utc
            FROM odds_snapshots s
            JOIN matches m ON m.id = s.match_id
            JOIN (
                SELECT match_id, operator, MAX(taken_at) AS mx
                FROM odds_snapshots
                WHERE ok = 1
                GROUP BY match_id, operator
            ) t
              ON s.match_id = t.match_id
             AND s.operator = t.operator
             AND s.taken_at = t.mx
            WHERE s.ok = 1
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
