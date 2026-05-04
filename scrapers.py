"""
Kinda Bet — odds scrapers across 4 NL operators (711.nl, Unibet, TOTO, TonyBet).

Reference operator is 711.nl (Kambi platform — exposes the most markets).
Unibet (also Kambi) is the fallback for match discovery and comparison.

Conventions
-----------
- All `fetch_*` functions return a list of "odds rows":
      {"market_key": str, "market_label": str,
       "selection_key": str, "selection_label": str,
       "line": float|None, "odd": float|None,
       "ok": bool, "note": str}
- They never raise. Network/parse failure → empty list (or a single ok=False
  placeholder row when we want to surface a diagnostic).
- `market_key` is a CANONICAL key meant to align across operators:
    * "MATCH_RESULT_FT"    full-time 1X2
    * "DOUBLE_CHANCE_FT"   1X / 12 / X2
    * "BTTS_FT"            both teams to score
    * "OVER_UNDER_FT@2.5"  totals (line baked in)
    * "HANDICAP_3WAY_FT@-1.5"  3-way handicap with line
    * "KAMBI_<criterionId>[@line]"  fallback for Kambi-native markets that
      didn't map to a canonical type — these still match between two Kambi
      brands (711 ↔ Unibet) but won't match TOTO.
- `selection_key` is also canonicalized where possible: "1" / "X" / "2" for
  match result, "OVER" / "UNDER" for totals, "YES" / "NO" for BTTS, etc.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")

# ---------- helpers ----------

def _http_get(url, timeout=12, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def _http_get_json(url, timeout=12, headers=None):
    return json.loads(_http_get(url, timeout, headers).decode("utf-8"))

# Turkish-specific letters that NFKD doesn't decompose (they're independent
# code points in Unicode, not diacritic combinations). Without this step,
# 'Sarıyer' normalizes to 'sar yer' (dotless ı → stripped by [a-z0-9]) while
# TOTO's 'Sariyer' normalizes to 'sariyer' — and the two no longer match.
_TURKISH_ASCII = str.maketrans({
    "ı": "i", "İ": "i",
    "ş": "s", "Ş": "s",
    "ğ": "g", "Ğ": "g",
    "ü": "u", "Ü": "u",
    "ö": "o", "Ö": "o",
    "ç": "c", "Ç": "c",
})

_TEAM_ALIASES = {
    "psg": "paris saint germain",
    "atl": "atletico",
    "atl madrid": "atletico madrid",
    "atletico": "atletico madrid",
    "real": "real madrid",
    "bayern": "bayern munchen",
    "bayern munich": "bayern munchen",
    "spurs": "tottenham",
    "man utd": "manchester united",
    "man city": "manchester city",
    "inter": "internazionale",
    "fenerbahce": "fenerbahce",
    "galatasaray": "galatasaray",
    "besiktas": "besiktas",
}

def _normalize_team(s):
    if not s:
        return ""
    s = s.translate(_TURKISH_ASCII)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    if s in _TEAM_ALIASES:
        s = _TEAM_ALIASES[s]
    for junk in [" fc", " cf", " ac", " sc", " s.c.", " a.c.", " c.f.",
                 "fc ", "afc ", " club", " bfc", " sk", " as", " fk", "fk ",
                 " bb", " gsk", " bld", " bel", " belediye", " spor kulubu"]:
        s = s.replace(junk, " ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s

def _team_match(a, b):
    na, nb = _normalize_team(a), _normalize_team(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    # Space-collapsed substring — bridges "Vanspor FK" (Kambi) vs "Van Spor FK" (TOTO).
    nas, nbs = na.replace(" ", ""), nb.replace(" ", "")
    if nas and nbs and (nas in nbs or nbs in nas):
        return True
    A = [w for w in na.split() if len(w) >= 3]
    B = [w for w in nb.split() if len(w) >= 3]
    for wa in A:
        for wb in B:
            n = min(len(wa), len(wb))
            if n >= 4 and wa[:n] == wb[:n]:
                return True
            if n >= 4 and wa[:4] == wb[:4]:
                return True
    return False

# Reason the most recent _chrome_dump call failed, so the caller can surface
# it in the persisted note (otherwise we'd just see "chrome dump failed" in
# the DB with no diagnostic). Module-level is fine: chrome calls are
# serialised through the refresh thread pool per worker.
_last_chrome_error = None

def _chrome_dump(url, timeout_sec=35, virtual_time_ms=20000):
    """Run headless Chrome and return rendered DOM bytes. Empty bytes on failure;
    the failure reason is stashed in `_last_chrome_error` for the caller."""
    global _last_chrome_error
    _last_chrome_error = None
    # Each chrome run gets its own profile dir so two parallel refreshes
    # (worker pool, max_workers=2) don't fight over the default chrome
    # profile lock. Also doubles as a writable HOME so chrome's crashpad
    # / cache can write under systemd's ProtectSystem=strict.
    profile_dir = tempfile.mkdtemp(prefix="kb-chrome-")
    try:
        # Chrome (and crashpad in particular) writes a few things to
        # $HOME/.config and $HOME/.cache regardless of --user-data-dir.
        # Under the kindabet systemd unit, $HOME=/opt/kindabet which is
        # read-only (only /opt/kindabet/data is writable). Without an
        # override, crashpad fails to write its socket dir and the main
        # chrome process SIGTRAPs at startup. Pointing HOME at the
        # tempdir routes every $HOME-relative write to a writable place.
        env = {**os.environ, "HOME": profile_dir,
               "XDG_CONFIG_HOME": profile_dir, "XDG_CACHE_HOME": profile_dir,
               "XDG_DATA_HOME": profile_dir}
        return subprocess.check_output([
            "google-chrome", "--headless", "--disable-gpu", "--no-sandbox",
            # systemd's PrivateTmp=true gives the service a small private
            # /dev/shm; parallel chromes saturate it and SIGTRAP. Force
            # IPC shared-memory onto /tmp instead.
            "--disable-dev-shm-usage",
            f"--user-data-dir={profile_dir}",
            f"--user-agent={UA}",
            "--lang=nl-NL", "--window-size=1280,3500",
            f"--virtual-time-budget={virtual_time_ms}",
            "--dump-dom", url,
        ], timeout=timeout_sec, stderr=subprocess.PIPE, env=env)
    except subprocess.TimeoutExpired:
        _last_chrome_error = f"timeout after {timeout_sec}s"
        return b""
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        _last_chrome_error = f"exit {e.returncode}: {tail[-1] if tail else '?'}"[:200]
        return b""
    except FileNotFoundError:
        _last_chrome_error = "google-chrome binary not found on PATH"
        return b""
    except Exception as e:
        _last_chrome_error = f"{type(e).__name__}: {e}"[:200]
        return b""
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)

# ---------- Kambi (711.nl + Unibet.nl) ----------

KAMBI_BRANDS = {
    "711.nl":    "sevelevnl",
    "Unibet.nl": "ubnl",
}

KAMBI_BASE = "https://eu-offering-api.kambicdn.com/offering/v2018"

def kambi_listview(brand, league_term, sport_term="football"):
    """Kambi listView events for a league within a sport. `league_term` is the
    Kambi-native termKey (e.g. 'champions_league' or country-prefixed like
    'england/premier_league'). `sport_term` defaults to 'football' for
    backwards compat with the original football-only API."""
    url = f"{KAMBI_BASE}/{brand}/listView/{sport_term}/{league_term}.json?lang=nl_NL&market=NL"
    try:
        d = _http_get_json(url, timeout=10)
        return d.get("events", []) or []
    except Exception:
        return []

def kambi_event_betoffers(brand, event_id):
    """All bet offers for a single event (every market, not just 1X2)."""
    url = (f"{KAMBI_BASE}/{brand}/betoffer/event/{event_id}.json"
           f"?lang=nl_NL&market=NL&includeParticipants=true")
    try:
        d = _http_get_json(url, timeout=15)
        return d.get("betOffers", []) or []
    except Exception:
        return []

# ---------- League logo lookup (TheSportsDB free CDN, no API key needed) ----------

_logo_cache_lock = threading.Lock()
_logo_cache = {}  # versatile cache: 'term:<x>' / 'country:<x>' / 'id:<x>'

# Kambi country prefix → TheSportsDB country name (their canonical spelling)
_KAMBI_TO_TSDB_COUNTRY = {
    "england": "England",       "spain": "Spain",          "italy": "Italy",
    "germany": "Germany",       "france": "France",        "netherlands": "Netherlands",
    "turkey": "Turkey",         "portugal": "Portugal",    "belgium": "Belgium",
    "scotland": "Scotland",     "denmark": "Denmark",      "norway": "Norway",
    "sweden": "Sweden",         "russia": "Russia",        "argentina": "Argentina",
    "brazil": "Brazil",         "usa": "United States",    "mexico": "Mexico",
    "japan": "Japan",           "south_korea": "South Korea", "australia": "Australia",
    "austria": "Austria",       "switzerland": "Switzerland", "poland": "Poland",
    "greece": "Greece",         "ukraine": "Ukraine",      "czech_republic": "Czech Republic",
    "finland": "Finland",       "ireland": "Ireland",      "wales": "Wales",
    "croatia": "Croatia",       "serbia": "Serbia",        "romania": "Romania",
    "bulgaria": "Bulgaria",     "hungary": "Hungary",      "slovakia": "Slovakia",
    "slovenia": "Slovenia",     "iceland": "Iceland",      "israel": "Israel",
    "saudi_arabia": "Saudi Arabia", "uae": "United Arab Emirates",
    "china": "China",           "india": "India",          "egypt": "Egypt",
    "south_africa": "South Africa", "morocco": "Morocco",  "chile": "Chile",
    "colombia": "Colombia",     "uruguay": "Uruguay",      "paraguay": "Paraguay",
    "peru": "Peru",             "ecuador": "Ecuador",      "venezuela": "Venezuela",
    "canada": "Canada",
}

# Direct league_term → TheSportsDB ID overrides. Used when TheSportsDB's
# country-scoped search doesn't return the league (their catalog is
# inconsistent for a few popular ones — e.g. Eredivisie isn't in their
# Netherlands search results but is queryable by ID).
_INTL_TSDB_IDS = {
    "champions_league":          4480,
    "europa_league":             4481,
    "conference_league":         4838,
    "netherlands/eredivisie":    4337,
    "england/premier_league":    4328,
    "germany/bundesliga":        4331,
    "italy/serie_a":             4332,
    "france/ligue_1":            4334,
    "spain/la_liga":             4335,
    "turkey/super_lig":          4339,
    "turkey/1__lig":             4676,
    "scotland/premiership":      4330,
}

def _tsdb_country_leagues(country):
    """All soccer leagues for a country on TheSportsDB, cached forever (within process)."""
    cache_key = f"country:{country}"
    with _logo_cache_lock:
        if cache_key in _logo_cache:
            return _logo_cache[cache_key]
    out = []
    try:
        url = (f"https://www.thesportsdb.com/api/v1/json/3/search_all_leagues.php"
               f"?c={urllib.parse.quote(country)}&s=Soccer")
        d = _http_get_json(url, timeout=10)
        for L in (d.get("countries") or d.get("leagues") or []):
            out.append({
                "name":  (L.get("strLeague") or "").strip(),
                "alt":   (L.get("strLeagueAlternate") or "").strip(),
                "badge": L.get("strBadge"),
            })
    except Exception:
        pass
    with _logo_cache_lock:
        _logo_cache[cache_key] = out
    return out

def _tsdb_lookup_id(league_id):
    cache_key = f"id:{league_id}"
    with _logo_cache_lock:
        if cache_key in _logo_cache:
            return _logo_cache[cache_key]
    L = {}
    try:
        url = f"https://www.thesportsdb.com/api/v1/json/3/lookupleague.php?id={league_id}"
        d = _http_get_json(url, timeout=8)
        L = (d.get("leagues") or [{}])[0]
    except Exception:
        pass
    with _logo_cache_lock:
        _logo_cache[cache_key] = L
    return L

def league_logo_url(league_term):
    """Resolve Kambi league_term → TheSportsDB badge URL. Cached. None if unknown."""
    if not league_term:
        return None
    key = f"term:{league_term}"
    with _logo_cache_lock:
        if key in _logo_cache:
            return _logo_cache[key]
    badge = None
    if league_term in _INTL_TSDB_IDS:
        L = _tsdb_lookup_id(_INTL_TSDB_IDS[league_term])
        badge = L.get("strBadge")
    elif "/" in league_term:
        country_code, league_short = league_term.split("/", 1)
        country = _KAMBI_TO_TSDB_COUNTRY.get(country_code)
        if country:
            leagues = _tsdb_country_leagues(country)
            normalized = league_short.lower().replace("_", " ").strip()
            tokens = [t for t in normalized.split() if t]
            best = (-1, None)
            for L in leagues:
                name_low = (L.get("name") or "").lower()
                alt_low  = (L.get("alt") or "").lower()
                score = sum(1 for t in tokens if t in name_low or t in alt_low)
                if normalized and (normalized in name_low or normalized in alt_low):
                    score += 10
                if score > best[0]:
                    best = (score, L.get("badge"))
            if best[0] > 0:
                badge = best[1]
    with _logo_cache_lock:
        _logo_cache[key] = badge
    return badge


# Turkish display names for Kambi country term codes. Falls back to the
# Kambi-provided (Dutch) name when a code isn't mapped.
_KAMBI_COUNTRY_TR = {
    "england": "İngiltere",       "spain": "İspanya",         "italy": "İtalya",
    "germany": "Almanya",         "france": "Fransa",         "netherlands": "Hollanda",
    "turkey": "Türkiye",          "portugal": "Portekiz",     "belgium": "Belçika",
    "scotland": "İskoçya",        "denmark": "Danimarka",     "norway": "Norveç",
    "sweden": "İsveç",            "russia": "Rusya",          "argentina": "Arjantin",
    "brazil": "Brezilya",         "usa": "ABD",               "mexico": "Meksika",
    "japan": "Japonya",           "south_korea": "Güney Kore", "australia": "Avustralya",
    "austria": "Avusturya",       "switzerland": "İsviçre",   "poland": "Polonya",
    "greece": "Yunanistan",       "ukraine": "Ukrayna",       "czech_republic": "Çekya",
    "finland": "Finlandiya",      "ireland": "İrlanda",       "wales": "Galler",
    "croatia": "Hırvatistan",     "serbia": "Sırbistan",      "romania": "Romanya",
    "bulgaria": "Bulgaristan",    "hungary": "Macaristan",    "slovakia": "Slovakya",
    "slovenia": "Slovenya",       "iceland": "İzlanda",       "israel": "İsrail",
    "saudi_arabia": "Suudi Arabistan", "uae": "BAE",          "china": "Çin",
    "india": "Hindistan",         "egypt": "Mısır",           "south_africa": "Güney Afrika",
    "morocco": "Fas",             "chile": "Şili",            "colombia": "Kolombiya",
    "uruguay": "Uruguay",         "paraguay": "Paraguay",     "peru": "Peru",
    "ecuador": "Ekvador",         "venezuela": "Venezuela",   "canada": "Kanada",
    "algeria": "Cezayir",         "tunisia": "Tunus",         "libya": "Libya",
    "north_macedonia": "Kuzey Makedonya", "albania": "Arnavutluk",
    "bosnia_and_herzegovina": "Bosna Hersek",
    "kazakhstan": "Kazakistan",   "georgia": "Gürcistan",     "armenia": "Ermenistan",
    "azerbaijan": "Azerbaycan",   "qatar": "Katar",           "iran": "İran",
    "iraq": "Irak",               "lebanon": "Lübnan",        "jordan": "Ürdün",
    "syria": "Suriye",            "kenya": "Kenya",           "nigeria": "Nijerya",
    "ghana": "Gana",              "thailand": "Tayland",      "indonesia": "Endonezya",
    "vietnam": "Vietnam",         "malaysia": "Malezya",      "singapore": "Singapur",
    "philippines": "Filipinler",  "new_zealand": "Yeni Zelanda",
    "estonia": "Estonya",         "latvia": "Letonya",        "lithuania": "Litvanya",
    "moldova": "Moldova",         "belarus": "Belarus",       "cyprus": "Kıbrıs",
    "luxembourg": "Lüksemburg",   "malta": "Malta",           "andorra": "Andorra",
    "san_marino": "San Marino",   "liechtenstein": "Liechtenstein",
    "monaco": "Monako",           "faroe_islands": "Faroe Adaları",
    "gibraltar": "Cebelitarık",
}

# Turkish display names for Kambi sport termKeys. Falls back to englishName
# (Kambi-provided) when a sport isn't mapped here.
_KAMBI_SPORT_TR = {
    "football":          "Futbol",
    "basketball":        "Basketbol",
    "tennis":            "Tenis",
    "ice_hockey":        "Buz Hokeyi",
    "icehockey":         "Buz Hokeyi",
    "volleyball":        "Voleybol",
    "handball":          "Hentbol",
    "snooker":           "Snooker",
    "darts":             "Dart",
    "mma":               "MMA",
    "ufc":               "UFC",
    "boxing":            "Boks",
    "american_football": "Amerikan Futbolu",
    "baseball":          "Beyzbol",
    "cricket":           "Kriket",
    "golf":              "Golf",
    "rugby_union":       "Ragbi",
    "rugby_league":      "Ragbi Ligi",
    "rugby":             "Ragbi",
    "cycling":           "Bisiklet",
    "motor_sports":      "Motor Sporları",
    "esports":           "E-Spor",
    "table_tennis":      "Masa Tenisi",
    "badminton":         "Badminton",
    "speedway":          "Speedway",
    "floorball":         "Floorball",
    "futsal":            "Futsal",
    "beach_soccer":      "Plaj Futbolu",
    "beach_volleyball":  "Plaj Voleybolu",
    "winter_sports":     "Kış Sporları",
    "athletics":         "Atletizm",
    "swimming":          "Yüzme",
    "horse_racing":      "At Yarışı",
    "greyhounds":        "Tazı Yarışı",
    "lacrosse":          "Lakros",
    "trotting":          "Tırıs",
    "water_polo":        "Sutopu",
    "chess":             "Satranç",
    "australian_rules":  "Avustralya Futbolu",
    "motorsports":       "Motor Sporları",
    "formula_1":         "Formula 1",
    "netball":           "Netbol",
    "surfing":           "Sörf",
    "ufc_mma":           "MMA",
    "pesapallo":         "Pesäpallo",
    "politics":          "Politika",
    "entertainment":     "Eğlence",
    "tv_specials":       "TV Özel",
}

def _kambi_sport_tr(sport_term, fallback=None):
    return _KAMBI_SPORT_TR.get((sport_term or "").lower(), fallback or sport_term or "")


# 5-minute TTL cache for league lists (~80KB JSON, called from a few hot paths).
_leagues_cache_lock = threading.Lock()
_leagues_cache = {}  # brand -> (timestamp, list)
_LEAGUES_CACHE_TTL = 300.0

# All-sports cache (heavier walk). Keyed by brand.
_all_sports_cache = {}  # brand -> (timestamp, list)
_ALL_SPORTS_CACHE_TTL = 1800.0   # 30 minutes in-memory
_SPORTS_FILE_CACHE_TTL = 86400.0  # 24 hours on disk
_SPORTS_FILE_PATH = None  # lazily resolved (avoids import-time os work)


def _sports_cache_path():
    global _SPORTS_FILE_PATH
    if _SPORTS_FILE_PATH is None:
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        _SPORTS_FILE_PATH = os.path.join(here, "data", "sports.json")
    return _SPORTS_FILE_PATH


def _read_sports_file_cache(brand):
    """Read brand's cached sport tree from disk if present and fresh.
    Returns the list or None on miss/expired/error."""
    import os
    p = _sports_cache_path()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    entry = (d or {}).get(brand)
    if not entry:
        return None
    age = time.time() - (entry.get("written_at") or 0)
    if age > _SPORTS_FILE_CACHE_TTL:
        return None
    return entry.get("leagues") or None


def _write_sports_file_cache(brand, leagues):
    """Persist brand's sport tree to disk (best-effort; never raises)."""
    import os
    p = _sports_cache_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        existing = {}
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    existing = json.load(f) or {}
            except Exception:
                existing = {}
        existing[brand] = {"written_at": time.time(), "leagues": leagues}
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


def kambi_invalidate_sports_cache(brand=None):
    """Clear in-memory + on-disk sport-tree cache so the next
    kambi_list_all_sports() call re-walks Kambi from scratch. Used by the
    Ayarlar "Sport Kategorilerini Yenile" button when the user wants to
    pick up newly-added leagues without waiting for the 30min/24h TTLs."""
    import os
    with _leagues_cache_lock:
        if brand:
            _all_sports_cache.pop(brand, None)
        else:
            _all_sports_cache.clear()
    try:
        p = _sports_cache_path()
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass


def kambi_list_all_sports(brand):
    """Walk Kambi's full group tree and return every (sport, league) tuple
    with Turkish display labels. Returns a flat list — frontend groups by
    sport_term. In-memory cache 30min; disk cache 24h.

    Each row:
      {
        "sport_term":    "football",
        "sport_name_tr": "Futbol",
        "sport_name_en": "Football",
        "league_term":   "champions_league" | "england/premier_league",
        "display_name":  "Uluslararası - Champions League",
        "country":       "Uluslararası",
        "country_code":  None | "england",
      }"""
    now = time.time()
    with _leagues_cache_lock:
        cached = _all_sports_cache.get(brand)
        if cached and (now - cached[0]) < _ALL_SPORTS_CACHE_TTL:
            return cached[1]
        disk = _read_sports_file_cache(brand)
        if disk:
            _all_sports_cache[brand] = (now, disk)
            return disk

    # Depth=3 reaches sport → country → league for nested structures and
    # sport → league for flat (international) structures.
    url = f"{KAMBI_BASE}/{brand}/group.json?lang=nl_NL&market=NL&depth=3"
    try:
        d = _http_get_json(url, timeout=20)
    except Exception:
        return []

    out = []
    root = d.get("group", {}) or {}
    sports = root.get("groups", []) or []

    for sport in sports:
        sport_term = (sport.get("termKey") or "").lower()
        if not sport_term:
            continue
        sport_name_en = sport.get("englishName") or sport.get("name") or sport_term
        sport_name_tr = _kambi_sport_tr(sport_term, sport_name_en)

        # Sports we explicitly skip — non-sport categories or simulation feeds.
        if sport_term in ("politics", "entertainment", "tv_specials",
                          "novelty", "virtual_sports"):
            continue

        for entry in sport.get("groups", []) or []:
            children = entry.get("groups", []) or []
            league_name = entry.get("name") or entry.get("termKey")
            if not children:
                # Flat international/cup competition — no country wrapper.
                lterm = entry.get("termKey")
                if not lterm:
                    continue
                out.append({
                    "sport_term":    sport_term,
                    "sport_name_tr": sport_name_tr,
                    "sport_name_en": sport_name_en,
                    "league_term":   lterm,
                    "display_name":  f"Uluslararası - {league_name}",
                    "country":       "Uluslararası",
                    "country_code":  None,
                })
            else:
                country_term = entry.get("termKey")
                country_nl   = entry.get("name") or country_term
                country_tr   = _KAMBI_COUNTRY_TR.get(country_term, country_nl)
                for league in children:
                    lterm = league.get("termKey")
                    if not lterm:
                        continue
                    lname = league.get("name") or lterm
                    out.append({
                        "sport_term":    sport_term,
                        "sport_name_tr": sport_name_tr,
                        "sport_name_en": sport_name_en,
                        "league_term":   f"{country_term}/{lterm}",
                        "display_name":  f"{country_tr} - {lname}",
                        "country":       country_tr,
                        "country_code":  country_term,
                    })

    out.sort(key=lambda x: (x["sport_name_tr"] or "", x["country"] or "", x["display_name"] or ""))

    with _leagues_cache_lock:
        _all_sports_cache[brand] = (now, out)
    _write_sports_file_cache(brand, out)
    return out


def kambi_list_football_leagues(brand):
    """Walk Kambi's group tree and return every football league term with a
    fully-qualified Turkish display name like "Türkiye - Süper Lig" or
    "Uluslararası - UEFA Champions League". Cached 5min."""
    now = time.time()
    with _leagues_cache_lock:
        cached = _leagues_cache.get(brand)
        if cached and (now - cached[0]) < _LEAGUES_CACHE_TTL:
            return cached[1]

    url = f"{KAMBI_BASE}/{brand}/group.json?lang=nl_NL&market=NL&depth=3"
    try:
        d = _http_get_json(url, timeout=15)
    except Exception:
        return []

    out = []

    def find_football(node):
        if (node.get("termKey") or "").lower() == "football":
            return node
        for child in node.get("groups", []) or []:
            hit = find_football(child)
            if hit:
                return hit
        return None

    football = find_football(d.get("group", {}))
    if not football:
        return out

    for entry in football.get("groups", []) or []:
        children = entry.get("groups", []) or []
        league_name = entry.get("name") or entry.get("termKey")
        if not children:
            # International/cup competition (no country wrapper) — e.g. UCL, UEL.
            out.append({
                "league_term":  entry.get("termKey"),
                "display_name": f"Uluslararası - {league_name}",
                "country":      "Uluslararası",
                "country_code": None,
            })
        else:
            country_term = entry.get("termKey")
            country_nl   = entry.get("name") or country_term
            country_tr   = _KAMBI_COUNTRY_TR.get(country_term, country_nl)
            for league in children:
                lterm = league.get("termKey")
                if not lterm:
                    continue
                lname = league.get("name") or lterm
                out.append({
                    "league_term":  f"{country_term}/{lterm}",
                    "display_name": f"{country_tr} - {lname}",
                    "country":      country_tr,
                    "country_code": country_term,
                })

    out.sort(key=lambda x: (x["country"] or "", x["display_name"] or ""))
    with _leagues_cache_lock:
        _leagues_cache[brand] = (now, out)
    return out


def league_display_name(league_term, brand=None):
    """Resolve a league_term to its Turkish-prefixed display name using the
    cached league list. Returns the term itself if not found."""
    if not league_term:
        return league_term
    if brand is None:
        brand = KAMBI_BRANDS.get(REFERENCE_OPERATOR)
    leagues = kambi_list_football_leagues(brand) if brand else []
    for lg in leagues:
        if lg.get("league_term") == league_term:
            return lg.get("display_name") or league_term
    return league_term

# Kambi criterion.ids that we map to cross-operator canonical keys. Anything
# not listed here gets a `KAMBI_<crit_id>` key — comparison still works across
# Kambi brands (711 ↔ Unibet) but TOTO won't align with these.
#
# Discovered by inspecting betoffer/event payloads. Add more as we find them.
KAMBI_CANONICAL_CRIT = {
    # ===== Football =====
    1001159858: "MATCH_RESULT_FT",     # FT 1X2 (the canonical "Full Time" Match Result)
    1000316018: "MATCH_RESULT_HT",     # 1X2 at half time
    1001159826: "MATCH_RESULT_2H",     # 1X2 in the 2nd half
    1001642858: "BTTS_FT",             # Both Teams To Score (FT)
    1001642863: "BTTS_1H",             # BTTS — 1st half
    1001642868: "BTTS_2H",             # BTTS — 2nd half
    1001159926: "OVER_UNDER_FT",       # Total Goals (FT) — line baked into key
    1001159532: "OVER_UNDER_1H",       # Total Goals — 1st half
    1001243173: "OVER_UNDER_2H",       # Total Goals — 2nd half
    1001159666: "DNB_FT",              # Draw No Bet (FT)
    1001159884: "DNB_1H",              # Draw No Bet — 1st half
    1001421321: "DNB_2H",              # Draw No Bet — 2nd half
    1001159711: "HANDICAP_FT",         # European 2-way handicap
    1001224081: "HANDICAP_3WAY_FT",    # 3-way handicap (1/X/2)
    1001568620: "HANDICAP_3WAY_1H",
    1001159967: "OVER_UNDER_HOME_FT",  # team-specific totals
    1001159633: "OVER_UNDER_AWAY_FT",
    1003194958: "OVER_UNDER_HOME_1H",
    1003194959: "OVER_UNDER_HOME_2H",
    1003194956: "OVER_UNDER_AWAY_1H",
    1003194957: "OVER_UNDER_AWAY_2H",
    1001159750: "FIRST_GOAL_FT",
    1001159830: "HTFT_FT",             # Half Time / Full Time (3x3 = 9 outcomes)
    1005692199: "TO_QUALIFY",
    # ===== Basketball =====
    # All basketball markets are "Including Overtime" by default in Kambi —
    # that's the actual final-result series most operators settle on.
    1001159732: "BASKETBALL_MONEYLINE",   # Moneyline (head-to-head, no draw)
    1001159509: "BASKETBALL_TOTAL",       # Total Points
    1001159512: "BASKETBALL_SPREAD",      # Point Spread (handicap)
    # ===== Ice Hockey =====
    # Hockey distinguishes "Regular Time" (RT, 60 min) from "Including OT"
    # (incl shootout) — settle differently, so canonicalize separately.
    1001482065: "HOCKEY_HANDICAP_3WAY_RT",
    1006583306: "HOCKEY_HANDICAP_3WAY",
    1001105863: "HOCKEY_TOTAL_RT",
    1001806062: "HOCKEY_TOTAL",
    1001105889: "HOCKEY_PUCK_LINE_RT",
    1006584055: "HOCKEY_PUCK_LINE",
    # ===== Tennis =====
    1001159891: "TENNIS_TOTAL_GAMES",
    1001427539: "TENNIS_GAME_HANDICAP",
    1001419385: "TENNIS_SET_HANDICAP",
    # ===== Volleyball =====
    1001159603: "VOLLEYBALL_TOTAL_POINTS",
    1001639432: "VOLLEYBALL_SET_HANDICAP",
    1001159489: "VOLLEYBALL_TOTAL_SETS",
    # ===== Match Winner (no-draw 2-way) — used by tennis, volleyball, MMA =====
    1001160042: "MATCH_WINNER",
    # ===== Handball (footy-shaped markets but distinct enough to namespace) =====
    1001105805: "HANDBALL_RESULT_FT",
    1001105726: "HANDBALL_HANDICAP",
    1001105791: "HANDBALL_HANDICAP_3WAY",
    1001105804: "HANDBALL_DOUBLE_CHANCE",
    1001105798: "HANDBALL_DNB",
    1001105866: "HANDBALL_TOTAL",
    # ===== Baseball =====
    1001159850: "BASEBALL_TOTAL_RUNS",
    1001159777: "BASEBALL_RUN_LINE",
    # ===== MMA / UFC =====
    1001160027: "MMA_BOUT_RESULT",     # Bout Odds (head-to-head)
    1001159754: "MMA_METHOD",          # Winning Method (KO/TKO, Submission, Decision)
    1001985368: "MMA_TOTAL_ROUNDS",    # Over/under rounds
    1001159960: "MMA_DISTANCE",        # To Go The Distance (yes/no)
}

# Markets we deliberately don't surface (cluttery and rarely worth comparing).
# Identified by criterion.id where we know it, and by an englishLabel substring
# match as a safety net for variants we haven't catalogued.
KAMBI_BLOCKED_CRIT_IDS = {
    1002275572,   # Asian Handicap (FT)
    1002275573,   # Asian Handicap (1H)
    1002244276,   # Asian Total (FT)
    1002558602,   # Asian Total (1H)
}
KAMBI_BLOCKED_LABEL_FRAGMENTS = (
    "asian handicap",
    "asian total",
    "asian over",
    "correct score",
)

def _kambi_offer_blocked(offer):
    crit = offer.get("criterion") or {}
    if crit.get("id") in KAMBI_BLOCKED_CRIT_IDS:
        return True
    label = (crit.get("englishLabel") or crit.get("label") or "").lower()
    return any(frag in label for frag in KAMBI_BLOCKED_LABEL_FRAGMENTS)

def _kambi_offer_line(offer):
    """Kambi puts the handicap/total line on each outcome (in milliunits).
    Both outcomes of a single offer share it — read from the first non-null."""
    for o in offer.get("outcomes", []) or []:
        ln = o.get("line")
        if ln is not None:
            return ln / 1000.0
    return None

# Turkish display labels for canonical market roots. The market_key keeps its
# canonical English form for cross-operator alignment; only the *displayed*
# label is localized.
MARKET_LABELS_TR = {
    # Football
    "MATCH_RESULT_FT":     "Maç Sonucu",
    "MATCH_RESULT_HT":     "İlk Yarı Sonucu",
    "MATCH_RESULT_2H":     "İkinci Yarı Sonucu",
    "BTTS_FT":             "Karşılıklı Gol",
    "BTTS_1H":             "Karşılıklı Gol (İlk Yarı)",
    "BTTS_2H":             "Karşılıklı Gol (İkinci Yarı)",
    "DOUBLE_CHANCE_FT":    "Çifte Şans",
    "DOUBLE_CHANCE_1H":    "Çifte Şans (İlk Yarı)",
    "OVER_UNDER_FT":       "Alt / Üst",
    "OVER_UNDER_1H":       "Alt / Üst (İlk Yarı)",
    "OVER_UNDER_2H":       "Alt / Üst (İkinci Yarı)",
    "OVER_UNDER_HOME_FT":  "Ev Sahibi Alt / Üst",
    "OVER_UNDER_AWAY_FT":  "Deplasman Alt / Üst",
    "OVER_UNDER_HOME_1H":  "Ev Sahibi Alt / Üst (İlk Yarı)",
    "OVER_UNDER_HOME_2H":  "Ev Sahibi Alt / Üst (İkinci Yarı)",
    "OVER_UNDER_AWAY_1H":  "Deplasman Alt / Üst (İlk Yarı)",
    "OVER_UNDER_AWAY_2H":  "Deplasman Alt / Üst (İkinci Yarı)",
    "DNB_FT":              "Beraberlikte İade",
    "DNB_1H":              "Beraberlikte İade (İlk Yarı)",
    "DNB_2H":              "Beraberlikte İade (İkinci Yarı)",
    "HANDICAP_FT":         "Handikap",
    "HANDICAP_3WAY_FT":    "Üçlü Handikap",
    "HANDICAP_3WAY_1H":    "Üçlü Handikap (İlk Yarı)",
    "FIRST_GOAL_FT":       "İlk Gol",
    "HTFT_FT":             "İlk Yarı / Maç Sonucu",
    "TO_QUALIFY":          "Tur Atlama",
    # No-draw match winner — tennis, volleyball, some MMA placements
    "MATCH_WINNER":              "Maç Kazananı",
    # Basketball
    "BASKETBALL_MONEYLINE":      "Maç Sonucu (Uzatma Dahil)",
    "BASKETBALL_TOTAL":          "Toplam Sayı (Uzatma Dahil)",
    "BASKETBALL_SPREAD":         "Sayı Farkı (Uzatma Dahil)",
    # Ice Hockey
    "HOCKEY_HANDICAP_3WAY":      "Üçlü Handikap (Uzatma Dahil)",
    "HOCKEY_HANDICAP_3WAY_RT":   "Üçlü Handikap (Normal Süre)",
    "HOCKEY_TOTAL":              "Toplam Gol (Uzatma Dahil)",
    "HOCKEY_TOTAL_RT":           "Toplam Gol (Normal Süre)",
    "HOCKEY_PUCK_LINE":          "Puck Line (Uzatma Dahil)",
    "HOCKEY_PUCK_LINE_RT":       "Puck Line (Normal Süre)",
    # Tennis
    "TENNIS_TOTAL_GAMES":        "Toplam Oyun",
    "TENNIS_GAME_HANDICAP":      "Oyun Handikabı",
    "TENNIS_SET_HANDICAP":       "Set Handikabı",
    # Volleyball
    "VOLLEYBALL_TOTAL_POINTS":   "Toplam Sayı",
    "VOLLEYBALL_SET_HANDICAP":   "Set Handikabı",
    "VOLLEYBALL_TOTAL_SETS":     "Toplam Set",
    # Handball
    "HANDBALL_RESULT_FT":        "Maç Sonucu",
    "HANDBALL_HANDICAP":         "Handikap",
    "HANDBALL_HANDICAP_3WAY":    "Üçlü Handikap",
    "HANDBALL_DOUBLE_CHANCE":    "Çifte Şans",
    "HANDBALL_DNB":              "Beraberlikte İade",
    "HANDBALL_TOTAL":            "Toplam Gol",
    # Baseball
    "BASEBALL_TOTAL_RUNS":       "Toplam Sayı",
    "BASEBALL_RUN_LINE":         "Run Line",
    # MMA / UFC
    "MMA_BOUT_RESULT":           "Maç Sonucu",
    "MMA_METHOD":                "Kazanma Şekli",
    "MMA_TOTAL_ROUNDS":          "Toplam Raund",
    "MMA_DISTANCE":              "Tüm Raundları Tamamlama",
}

# Turkish labels for normalized selection keys. Team-name selections (used by
# handicap markets) are NOT in this dict — they pass through unchanged.
#
# Note on KG: Turkish bet sites (Bilyoner, İddaa, Nesine, Misli) use
# "KG VAR" / "KG YOK" — never "Evet/Hayır" — for Both Teams To Score, so we
# label these "Var" / "Yok" to match the convention.
SELECTION_LABELS_TR = {
    "1":     "1",
    "X":     "X",
    "2":     "2",
    "1X":    "1-X",
    "12":    "1-2",
    "X2":    "X-2",
    "YES":   "Var",
    "NO":    "Yok",
    "OVER":  "Üst",
    "UNDER": "Alt",
}

def _kambi_canonical_market(offer):
    """Return (market_key, market_label_tr, line_or_None)."""
    crit = offer.get("criterion") or {}
    crit_id = crit.get("id")
    line_f = _kambi_offer_line(offer)
    canonical_root = KAMBI_CANONICAL_CRIT.get(crit_id)
    if canonical_root:
        mk = f"{canonical_root}@{line_f:g}" if line_f is not None else canonical_root
        # Prefer Turkish label for canonical markets; fall back to englishLabel
        label = MARKET_LABELS_TR.get(canonical_root) or crit.get("englishLabel") or "Market"
    else:
        suffix = f"@{line_f:g}" if line_f is not None else ""
        mk = f"KAMBI_{crit_id}{suffix}"
        # Unknown market — use Kambi's English label (universal) instead of Dutch.
        label = crit.get("englishLabel") or crit.get("label") or "Market"
    return mk, label, line_f

def _kambi_canonical_selection(outcome):
    """Map a Kambi outcome to a stable (selection_key, selection_label_tr)."""
    eng = (outcome.get("englishLabel") or "").strip()
    lab = (outcome.get("label") or "").strip()
    el = eng.lower()
    sel_key = None
    if eng in ("1", "X", "2"):  sel_key = eng
    elif el in ("yes", "ja"):    sel_key = "YES"
    elif el in ("no", "nee"):    sel_key = "NO"
    elif el == "over":           sel_key = "OVER"
    elif el == "under":          sel_key = "UNDER"
    if sel_key:
        return sel_key, SELECTION_LABELS_TR.get(sel_key, sel_key)
    # Team names (handicap markets) and other outcomes pass through using
    # englishLabel (consistent across Kambi brands).
    return (eng or lab or str(outcome.get("id"))), (eng or lab)

def _kambi_parse_betoffers(offers):
    rows = []
    for offer in offers:
        if _kambi_offer_blocked(offer):
            continue
        market_key, market_label, line_f = _kambi_canonical_market(offer)
        crit_id = (offer.get("criterion") or {}).get("id")
        for o in offer.get("outcomes", []) or []:
            odd = o.get("odds")
            if odd is not None:
                odd = odd / 1000.0
            sel_key, sel_label = _kambi_canonical_selection(o)
            rows.append({
                "market_key":      market_key,
                "market_label":    market_label,
                "selection_key":   sel_key,
                "selection_label": sel_label,
                "line":            line_f,
                "odd":             odd,
                "ok":              odd is not None,
                "note":            f"kambi:crit={crit_id}",
            })
    return rows

def fetch_kambi(operator, kambi_event_id, **_):
    """Fetch all markets for one event from a Kambi brand."""
    brand = KAMBI_BRANDS.get(operator)
    if not brand or not kambi_event_id:
        return []
    offers = kambi_event_betoffers(brand, kambi_event_id)
    if not offers:
        return [{"market_key": "MATCH_RESULT_FT", "market_label": "Maç Sonucu",
                 "selection_key": "1", "selection_label": "1",
                 "line": None, "odd": None, "ok": False,
                 "note": f"kambi:{brand} no betoffers for ev {kambi_event_id}"}]
    return _kambi_parse_betoffers(offers)

# ---------- TOTO (sport.toto.nl + sport-api.toto.nl) ----------
#
# TOTO's REST API only exposes the "primary" market (1X2). Their full event
# catalog (handicaps, totals, BTTS, etc.) is delivered to their SPA via SignalR
# WebSocket. Rather than reverse-engineering the auth+protocol, we render the
# match page in headless Chrome — the SPA writes every market into the DOM as
# inline JSON, which is straightforward to extract.
#
# Flow:
#   1. JSON search → find event_id (fast, ~0.5s)
#   2. Chrome dump match page (virtual-time-budget=30s) → all markets in DOM
#   3. Walk the DOM, extract every {"id":"...", "eventId":"<id>", ...} object
#   4. Map TOTO groupCode → canonical market_key (aligns with Kambi)

# TOTO search has a fairly tight rate limit. During a bulk refresh we hit
# /search dozens of times per minute (each match → ~6 query attempts). To
# stay below the limit we (a) cache results in-process for 60s so repeated
# queries for the same token (Hatayspor home + Hatayspor away of two matches)
# collapse, (b) soft-throttle distinct lookups to roughly 2 req/s, and
# (c) retry once on HTTP 429.
_toto_search_cache = {}                       # query_text → (timestamp, list[ev])
_toto_search_cache_lock = threading.Lock()
_TOTO_SEARCH_CACHE_TTL = 60.0
_TOTO_SEARCH_THROTTLE_S = 0.4
_toto_throttle_lock = threading.Lock()
_toto_last_call_ts = [0.0]                    # mutable container — accessed under throttle_lock

def _toto_search_query(text):
    """Run a TOTO /search with caching, throttling, and one 429 retry.
    Returns the events list (possibly empty); never raises."""
    now = time.time()
    with _toto_search_cache_lock:
        cached = _toto_search_cache.get(text)
        if cached and (now - cached[0]) < _TOTO_SEARCH_CACHE_TTL:
            return cached[1]
    # Soft throttle — block briefly if we ran another query too recently.
    with _toto_throttle_lock:
        wait = _TOTO_SEARCH_THROTTLE_S - (time.time() - _toto_last_call_ts[0])
        if wait > 0:
            time.sleep(wait)
        _toto_last_call_ts[0] = time.time()

    url = f"https://sport-api.toto.nl/search?searchText={urllib.parse.quote(text)}"
    events = []
    succeeded = False
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read().decode("utf-8"))
            events = d.get("events", []) or []
            succeeded = True
            break
        except urllib.error.HTTPError as e:
            if getattr(e, "code", None) == 429 and attempt == 0:
                time.sleep(2.0)  # one cooldown then retry
                continue
            break
        except Exception:
            break
    # Only cache successful responses; caching a 429-induced empty list would
    # poison the next bulk-refresh sweep with false negatives.
    if succeeded:
        with _toto_search_cache_lock:
            _toto_search_cache[text] = (time.time(), events)
    return events

def _toto_search_event(home, away, kickoff_utc_iso=None):
    """Find a TOTO event matching (home, away). Returns event dict or None."""
    norm_home = _normalize_team(home)
    norm_away = _normalize_team(away)
    queries = []
    for src in (norm_home, norm_away):
        for token in src.split():
            if len(token) >= 4 and token not in queries:
                queries.append(token)
        if src and src not in queries:
            queries.append(src)
    for q in queries[:6]:
        for ev in _toto_search_query(q):
            name = ev.get("name", "")
            if _team_match(home, name) and _team_match(away, name):
                return ev
    return None

def _toto_extract_markets_from_dom(event_id, dom):
    """Walk the rendered TOTO match-page DOM and return every market JSON.
    Markets are embedded as inline JSON objects keyed by `eventId`. We locate
    each `"eventId":"<event_id>"` occurrence, walk back to the opening brace
    and forward to the balanced close, then `json.loads` the slice."""
    markets = []
    seen_ids = set()
    needle = f'"eventId":"{event_id}"'
    i = 0
    while True:
        i = dom.find(needle, i)
        if i < 0:
            break
        # Walk back to the enclosing '{'
        j = i
        depth = 0
        while j > 0:
            ch = dom[j]
            if ch == '}':
                depth += 1
            elif ch == '{':
                if depth == 0:
                    break
                depth -= 1
            j -= 1
        # Walk forward to the matching '}'
        k = j
        d = 0
        while k < len(dom):
            ch = dom[k]
            if ch == '{':
                d += 1
            elif ch == '}':
                d -= 1
                if d == 0:
                    k += 1
                    break
            k += 1
        try:
            m = json.loads(dom[j:k])
        except json.JSONDecodeError:
            i = k
            continue
        if "outcomes" in m and m.get("id") not in seen_ids:
            seen_ids.add(m["id"])
            markets.append(m)
        i = k
    return markets

# TOTO `groupCode` → (canonical_root, has_line). Anything not in this table
# falls through to a TOTO-native key that won't align cross-operator.
TOTO_GROUP_TO_CANONICAL = {
    "MATCH_RESULT":                          ("MATCH_RESULT_FT",      False),
    "MATCH_RESULT_1ST_HALF":                 ("MATCH_RESULT_HT",      False),
    "MATCH_RESULT_2ND_HALF":                 ("MATCH_RESULT_2H",      False),
    "BOTH_TEAMS_TO_SCORE":                   ("BTTS_FT",              False),
    "BOTH_TEAMS_TO_SCORE_1ST_HALF":          ("BTTS_1H",              False),
    "BOTH_TEAMS_TO_SCORE_2ND_HALF":          ("BTTS_2H",              False),
    "DOUBLE_CHANCE":                         ("DOUBLE_CHANCE_FT",     False),
    "DOUBLE_CHANCE_1ST_HALF":                ("DOUBLE_CHANCE_1H",     False),
    "NO_BET_DRAW":                           ("DNB_FT",               False),
    "NO_BET_DRAW_1ST_HALF":                  ("DNB_1H",               False),
    "NO_BET_DRAW_2ND_HALF":                  ("DNB_2H",               False),
    "HANDICAP":                              ("HANDICAP_3WAY_FT",     True),
    "HANDICAP_1ST_HALF":                     ("HANDICAP_3WAY_1H",     True),
    "HANDICAP_2ND_HALF":                     ("HANDICAP_3WAY_2H",     True),
    "TOTAL_GOALS_OVER/UNDER":                ("OVER_UNDER_FT",        True),
    "TOTAL_GOALS_OVER/UNDER_1ST_HALF":       ("OVER_UNDER_1H",        True),
    "TOTAL_GOALS_OVER/UNDER_2ND_HALF":       ("OVER_UNDER_2H",        True),
    "TOTAL_GOALS_OVER/UNDER_HOME":           ("OVER_UNDER_HOME_FT",   True),
    "TOTAL_GOALS_OVER/UNDER_AWAY":           ("OVER_UNDER_AWAY_FT",   True),
    "TOTAL_GOALS_OVER/UNDER_1ST_HALF_HOME":  ("OVER_UNDER_HOME_1H",   True),
    "TOTAL_GOALS_OVER/UNDER_2ND_HALF_HOME":  ("OVER_UNDER_HOME_2H",   True),
    "TOTAL_GOALS_OVER/UNDER_1ST_HALF_AWAY":  ("OVER_UNDER_AWAY_1H",   True),
    "TOTAL_GOALS_OVER/UNDER_2ND_HALF_AWAY":  ("OVER_UNDER_AWAY_2H",   True),
    # MMA — TOTO ships the bout-winner under FIGHT_WINNER. Selections come
    # back as subType=H (competitor1) and subType=A (competitor2) plus the
    # actual fighter name; we map them to 1/2 to align with Kambi's MMA rows.
    "FIGHT_WINNER":                          ("MMA_BOUT_RESULT",      False),
}

# Selection-key remaps per canonical type.
_TOTO_SEL_3WAY    = {"H": "1", "D": "X", "L": "X", "A": "2"}        # 1/X/2 (handicap uses L for draw)
_TOTO_SEL_OU      = {"H": "OVER", "L": "UNDER", "O": "OVER", "U": "UNDER"}
_TOTO_SEL_BTTS    = {"Ja": "YES", "Nee": "NO", "Yes": "YES", "No": "NO"}
_TOTO_SEL_DC      = {"1": "1X", "3": "12", "2": "X2"}               # by subType (HD/HA/DA encoded as 1/3/2)
_TOTO_SEL_DNB     = {"H": "1", "A": "2"}
_TOTO_SEL_HH      = {"H": "1", "A": "2"}                            # head-to-head (MMA, tennis-like)

_TOTO_3WAY_ROOTS  = {"MATCH_RESULT_FT", "MATCH_RESULT_HT", "MATCH_RESULT_2H",
                     "HANDICAP_3WAY_FT", "HANDICAP_3WAY_1H", "HANDICAP_3WAY_2H"}
_TOTO_HH_ROOTS    = {"MMA_BOUT_RESULT"}
_TOTO_OU_ROOTS    = {"OVER_UNDER_FT", "OVER_UNDER_1H", "OVER_UNDER_2H",
                     "OVER_UNDER_HOME_FT", "OVER_UNDER_AWAY_FT",
                     "OVER_UNDER_HOME_1H", "OVER_UNDER_HOME_2H",
                     "OVER_UNDER_AWAY_1H", "OVER_UNDER_AWAY_2H"}
_TOTO_BTTS_ROOTS  = {"BTTS_FT", "BTTS_1H", "BTTS_2H"}
_TOTO_DC_ROOTS    = {"DOUBLE_CHANCE_FT", "DOUBLE_CHANCE_1H"}
_TOTO_DNB_ROOTS   = {"DNB_FT", "DNB_1H", "DNB_2H"}

# Substring matches against `groupCode`. Anything we don't want to surface
# (Asian variants, correct score grids, weird specials) is skipped before
# canonicalization so it never reaches the DB.
TOTO_BLOCKED_GROUP_FRAGMENTS = (
    "ASIAN_HANDICAP",
    "ASIAN_TOTAL",
    "ASIAN_OVER_UNDER",
    "CORRECT_SCORE",
)

def _toto_canonical_market(market):
    """Return (market_key, market_label, selection_remap or None, line_or_None)."""
    gc = market.get("groupCode") or ""
    name = market.get("name") or gc
    line_raw = market.get("handicapValue")
    try:
        line_f = float(line_raw) if line_raw is not None else None
    except (TypeError, ValueError):
        line_f = None
    # TOTO stores Asian Handicap lines as integer counts of 0.25 steps
    # (handicapValue=2 means a +0.5 line, =-6 means -1.5, etc.).
    # Other markets (regular handicap, totals) already use plain decimals.
    if line_f is not None and gc.startswith("ASIAN_HANDICAP"):
        line_f = line_f / 4.0
    canonical = TOTO_GROUP_TO_CANONICAL.get(gc)
    if canonical:
        root, has_line = canonical
        mk = f"{root}@{line_f:g}" if (has_line and line_f is not None) else root
        if root in _TOTO_3WAY_ROOTS:    sel_remap = _TOTO_SEL_3WAY
        elif root in _TOTO_OU_ROOTS:    sel_remap = _TOTO_SEL_OU
        elif root in _TOTO_BTTS_ROOTS:  sel_remap = _TOTO_SEL_BTTS
        elif root in _TOTO_DC_ROOTS:    sel_remap = _TOTO_SEL_DC
        elif root in _TOTO_DNB_ROOTS:   sel_remap = _TOTO_SEL_DNB
        elif root in _TOTO_HH_ROOTS:    sel_remap = _TOTO_SEL_HH
        else:                           sel_remap = None  # Asian handicap etc — keep team names
        return mk, name, sel_remap, line_f
    # Fallback — TOTO-native key
    line_suffix = f"@{line_f:g}" if line_f is not None else ""
    return f"TOTO_{gc}{line_suffix}", name, None, line_f

def _toto_dump_markets(event_id):
    """Render the TOTO match page and parse every embedded market JSON.
    Returns the list of market dicts (possibly empty). DOM-empty / fetch-fail
    distinction: returns ([], "<note>") so the caller can decide whether to
    re-resolve the event id."""
    url = f"https://sport.toto.nl/wedden/wedstrijd/{event_id}"
    dom = _chrome_dump(url, timeout_sec=60, virtual_time_ms=30000).decode("utf-8", "replace")
    if not dom:
        reason = _last_chrome_error or "unknown"
        return [], f"chrome dump failed for ev {event_id} ({reason})"
    markets = _toto_extract_markets_from_dom(event_id, dom)
    if not markets:
        return [], f"0 markets in DOM for ev {event_id}"
    return markets, None

def fetch_toto(home, away, kickoff_utc_iso=None, toto_event_id=None, **_):
    """Render TOTO's match page via headless Chrome and harvest every market.
    If `toto_event_id` is supplied (cached by db.set_toto_event_id), skip the
    /search resolution entirely. On stale-cache fallback (cached id renders
    empty), re-resolve via /search and retry once."""
    try:
        event_id = str(toto_event_id) if toto_event_id else None
        markets = []
        empty_note = None

        if event_id:
            markets, empty_note = _toto_dump_markets(event_id)
            if not markets:
                # Cached id no longer valid (event finished, reassigned, …).
                # Drop it and fall through to a fresh /search lookup.
                event_id = None

        if not event_id:
            ev = _toto_search_event(home, away, kickoff_utc_iso)
            if not ev:
                return [{"market_key": "MATCH_RESULT_FT", "market_label": "Maç Sonucu",
                         "selection_key": "1", "selection_label": "1",
                         "line": None, "odd": None, "ok": False,
                         "note": "toto: no event match",
                         "toto_event_id": None}]
            event_id = str(ev["id"])
            markets, empty_note = _toto_dump_markets(event_id)

        if not markets:
            return [{"market_key": "MATCH_RESULT_FT", "market_label": "Maç Sonucu",
                     "selection_key": "1", "selection_label": "1",
                     "line": None, "odd": None, "ok": False,
                     "note": f"toto: {empty_note}",
                     "toto_event_id": event_id}]
        # MATCH_RESULT_2 is the "early payout" version — different odds, would
        # double up on MATCH_RESULT_FT. Drop it and prefer plain MATCH_RESULT
        # for cross-operator comparison.
        if any(m.get("groupCode") == "MATCH_RESULT" for m in markets):
            markets = [m for m in markets if m.get("groupCode") != "MATCH_RESULT_2"]
        rows = []
        for m in markets:
            if m.get("status") and m["status"] != "ACTIVE":
                continue
            gc_up = (m.get("groupCode") or "").upper()
            if any(frag in gc_up for frag in TOTO_BLOCKED_GROUP_FRAGMENTS):
                continue
            mk, mlabel, sel_remap, line_f = _toto_canonical_market(m)
            for o in m.get("outcomes", []) or []:
                if not o.get("active", True) or not o.get("displayed", True):
                    continue
                prices = o.get("prices") or []
                if not prices:
                    continue
                odd = prices[0].get("decimal")
                osub = o.get("subType") or ""
                olab = o.get("name") or osub
                if sel_remap and osub in sel_remap:
                    sk = sel_remap[osub]
                elif sel_remap and olab in sel_remap:
                    sk = sel_remap[olab]
                else:
                    # TOTO uses subType="-" for many markets (BTTS variants,
                    # team-specific bets); falling back to subType collides
                    # every outcome onto one selection_key. Prefer the
                    # outcome name when the subType is unhelpful.
                    sk = olab if osub in ("", "-") else osub
                rows.append({
                    "market_key":      mk,
                    "market_label":    mlabel,
                    "selection_key":   sk,
                    "selection_label": olab,
                    "line":            line_f,
                    "odd":             odd,
                    "ok":              odd is not None,
                    "note":            f"toto: ev {event_id} mkt {m.get('id')} {m.get('groupCode')}",
                })
        if not rows:
            rows.append({"market_key": "MATCH_RESULT_FT", "market_label": "Maç Sonucu",
                         "selection_key": "1", "selection_label": "1",
                         "line": None, "odd": None, "ok": False,
                         "note": f"toto: parsed 0 outcomes for ev {event_id}"})
        # Carry the resolved id back to the caller on the first row so it can
        # update the per-match cache. Other rows don't need it; the caller
        # only reads it once.
        rows[0]["toto_event_id"] = event_id
        return rows
    except Exception as e:
        return [{"market_key": "MATCH_RESULT_FT", "market_label": "Maç Sonucu",
                 "selection_key": "1", "selection_label": "1",
                 "line": None, "odd": None, "ok": False,
                 "note": f"toto err: {e}",
                 "toto_event_id": None}]

# ---------- TonyBet.nl (platform.tonybet.nl) ----------
#
# TonyBet runs on a Sportradar-derived backend with a public-ish JSON API.
# The catalog is filtered by `sportCategoryId` (per-league) for football and
# `sportId` (per-sport) for MMA, since UFC is one big category that's easier
# to grab in a single sweep. Per-sport fetch specs:
#   football: sportCategoryId in (101 UEFA, 41 England, 111 Turkey)
#   ufc_mma:  sportId=1122 (covers all UFC categories)
#
# Markets are identified by integer `id` (TonyBet-internal). We map only the
# canonical types we want to compare cross-operator. Lines (when present) live
# in the `specifiers` string — e.g. `"hcp=-1"` for Asian Handicap. TonyBet
# typically exposes only the main line per market type (not all variants),
# unlike Kambi which lists every line as a separate offer.

# (sport_term → list of (filter_param, value) tuples). Each tuple becomes one
# request; results are unioned. Football needs three category requests; MMA
# uses a single sportId-scoped request.
TONYBET_FETCH_SPECS = {
    "football": [("sportCategoryId_eq", 101),
                 ("sportCategoryId_eq", 41),
                 ("sportCategoryId_eq", 111)],
    "ufc_mma":  [("sportId_eq", 1122)],
}

# market.id → (canonical_root, line_mode, outcome_id → selection_key), keyed
# by sport. `line_mode` is True if the market_key gets the line appended.
#
# Football "Notably absent":
#   - 868 (Total Goals OU, "main"): single line per event with specifiers=null.
#     Replaced by 289 which ships every line explicitly in `specifiers`.
#   - 557 (Asian Handicap): removed system-wide.
#   - 189: spurious BTTS-1H mapping reverted in 4931dae after Everton-City
#     showed only one outcome with implied probability 7.7% vs Kambi's 25%.
TONYBET_MARKETS = {
    "football": {
        621: ("MATCH_RESULT_FT",   False, {1: "1", 2: "X", 3: "2"}),
        589: ("BTTS_FT",           False, {74: "YES", 76: "NO"}),
        721: ("DOUBLE_CHANCE_FT",  False, {436: "1X", 438: "12", 440: "X2"}),
        # Multi-line Total Goals OU. `specifiers="total=2.5"` parses to line=2.5;
        # the global OVER_UNDER_FT@2.5 filter in app.py keeps only 2.5 visible.
        289: ("OVER_UNDER_FT",     True,  {12: "OVER", 13: "UNDER"}),
        # Half-Time / Full-Time (3x3 matrix). Confirmed against Kambi
        # crit 1001159830 across 3 events.
        467: ("HTFT_FT",           False, {
            418: "1/1", 420: "1/X", 422: "1/2",
            424: "X/1", 426: "X/X", 428: "X/2",
            430: "2/1", 432: "2/X", 434: "2/2",
        }),
    },
    "ufc_mma": {
        # Bout winner. Outcomes 4/5 follow the Sportradar UOF convention for
        # head-to-head markets (4=competitor1, 5=competitor2). Mapped to 1/2
        # so they line up with Kambi's MMA rows.
        910: ("MMA_BOUT_RESULT",   False, {4: "1", 5: "2"}),
    },
}

# Cached per-(sport, query) fetch — bulk-refresh hits the same API many
# times; collapses redundant calls within a 60s window.
_tonybet_cache = {}                       # (sport, param, value) → (timestamp, data)
_tonybet_cache_lock = threading.Lock()
_TONYBET_CACHE_TTL = 60.0

def _tonybet_fetch_spec(sport, param, value):
    """Fetch one TonyBet event-list slice scoped by either sportCategoryId or
    sportId. Cached in-process for 60s."""
    cache_key = (sport, param, value)
    now = time.time()
    with _tonybet_cache_lock:
        cached = _tonybet_cache.get(cache_key)
        if cached and (now - cached[0]) < _TONYBET_CACHE_TTL:
            return cached[1]
    qs = (
        "lang=nl&relations=odds&relations=competitors&relations=league"
        "&oddsExists_eq=1&main=1&period=0&limit=150&status_in=0&isLive=false"
        f"&{param}={value}"
    )
    try:
        d = _http_get_json(
            f"https://platform.tonybet.nl/api/event/list?{qs}", timeout=15)
        data = d.get("data", {})
    except Exception:
        return None
    with _tonybet_cache_lock:
        _tonybet_cache[cache_key] = (now, data)
    return data

def _tonybet_parse_line(spec):
    """`specifiers` is a string like 'hcp=-1', 'total=2.5'. Returns float or None."""
    if not spec:
        return None
    parts = spec.split("=", 1)
    if len(parts) != 2:
        return None
    try:
        return float(parts[1])
    except ValueError:
        return None

def fetch_tonybet(home, away, kickoff_utc_iso=None, sport="football", **_):
    """Find the TonyBet event that matches our (home, away, kickoff) and pull
    every canonical-mappable market off it. Sport-aware: football uses three
    category-scoped fetches, ufc_mma uses one sportId-scoped fetch."""
    sport = (sport or "football").lower()
    fetch_specs = TONYBET_FETCH_SPECS.get(sport)
    if not fetch_specs:
        # No support for this sport yet (basketball, hockey, …). Emit one
        # placeholder row so the operator surface area stays honest.
        return [{"market_key": "MATCH_RESULT_FT", "market_label": "Maç Sonucu",
                 "selection_key": "1", "selection_label": "1",
                 "line": None, "odd": None, "ok": False,
                 "note": f"tonybet: sport '{sport}' not wired"}]
    market_map = TONYBET_MARKETS.get(sport, {})

    target_dt = None
    if kickoff_utc_iso:
        try:
            target_dt = datetime.fromisoformat(kickoff_utc_iso.replace("Z", "+00:00"))
        except Exception:
            target_dt = None

    found_event = None
    found_odds = []
    found_c1 = found_c2 = ""

    # MMA fight names sometimes shift by ±10 minutes from Kambi's listed
    # kickoff (especially card co-mains). Loosen the time gate to ±2h for MMA.
    time_window = 7200 if sport == "ufc_mma" else 1800

    for param, value in fetch_specs:
        data = _tonybet_fetch_spec(sport, param, value)
        if not data:
            continue
        comp_by_id = {c["id"]: c for c in data.get("relations", {}).get("competitors", [])}
        odds_by_ev = data.get("relations", {}).get("odds", {}) or {}
        for ev in data.get("items", []):
            c1 = comp_by_id.get(ev.get("competitor1Id")) or {}
            c2 = comp_by_id.get(ev.get("competitor2Id")) or {}
            n1, n2 = c1.get("name", ""), c2.get("name", "")
            # Time gating first (fast filter)
            if target_dt:
                ev_time = ev.get("time")
                try:
                    ev_dt = datetime.fromisoformat(ev_time).replace(tzinfo=timezone.utc)
                    if abs((ev_dt - target_dt).total_seconds()) > time_window:
                        continue
                except Exception:
                    continue
            if _team_match(home, n1) and _team_match(away, n2):
                found_event = ev
                found_odds = odds_by_ev.get(str(ev["id"]), [])
                found_c1, found_c2 = n1, n2
                break
        if found_event:
            break

    if not found_event:
        return [{"market_key": "MATCH_RESULT_FT", "market_label": "Maç Sonucu",
                 "selection_key": "1", "selection_label": "1",
                 "line": None, "odd": None, "ok": False,
                 "note": "tonybet: no event match"}]

    rows = []
    for m in found_odds:
        mapping = market_map.get(m.get("id"))
        if not mapping:
            continue
        canonical_root, line_mode, sel_remap = mapping
        line_f = None
        if line_mode:
            line_f = _tonybet_parse_line(m.get("specifiers"))
            market_key = (f"{canonical_root}@{line_f:g}"
                          if line_f is not None else canonical_root)
        else:
            market_key = canonical_root
        market_label = MARKET_LABELS_TR.get(canonical_root, canonical_root)

        for o in m.get("outcomes", []) or []:
            if o.get("active", 1) != 1:
                continue
            odd = o.get("odds")
            if odd is None:
                continue
            sk = sel_remap.get(o.get("id")) if sel_remap else None
            if not sk:
                continue
            sl = SELECTION_LABELS_TR.get(sk, sk)
            rows.append({
                "market_key":      market_key,
                "market_label":    market_label,
                "selection_key":   sk,
                "selection_label": sl,
                "line":            line_f,
                "odd":             odd,
                "ok":              True,
                "note":            f"tonybet: ev {found_event['id']} mkt {m.get('id')}",
            })

    if not rows:
        rows.append({"market_key": "MATCH_RESULT_FT", "market_label": "Maç Sonucu",
                     "selection_key": "1", "selection_label": "1",
                     "line": None, "odd": None, "ok": False,
                     "note": f"tonybet: ev {found_event['id']} no canonical markets"})
    return rows

# ---------- Operator registry ----------

OPERATORS = [
    # name, license, fetch_fn, is_reference
    ("711.nl",     "KSA", fetch_kambi,   True),    # reference (Kambi platform — deepest catalog)
    ("Unibet.nl",  "KSA", fetch_kambi,   False),   # Kambi platform
    ("TOTO.nl",    "KSA", fetch_toto,    False),   # Headless Chrome (SignalR-only API)
    ("TonyBet.nl", "KSA", fetch_tonybet, False),   # Sportradar-derived JSON API
]

REFERENCE_OPERATOR = "711.nl"
FALLBACK_OPERATOR = "Unibet.nl"

# ---------- Match discovery ----------

# Kambi league entries — each item is a dict (sport_term, display_name,
# league_term) so we can handle multiple sports without ambiguity. If a term
# turns out to be wrong for a brand, the league just shows empty; tweak here.
COMPETITIONS = [
    {"sport_term": "football", "display_name": "Uluslararası - Champions League",  "league_term": "champions_league"},
    {"sport_term": "football", "display_name": "Uluslararası - Europa League",     "league_term": "europa_league"},
    {"sport_term": "football", "display_name": "İngiltere - Premier League",       "league_term": "england/premier_league"},
    {"sport_term": "football", "display_name": "Türkiye - Süper Lig",              "league_term": "turkey/super_lig"},
    {"sport_term": "football", "display_name": "Türkiye - 1. Lig",                 "league_term": "turkey/1__lig"},
]


def _normalize_competition(c):
    """Accept either the new dict shape or the legacy (name, term) tuple shape
    (from older saved settings). Always returns a dict — sport_term defaults
    to 'football' for legacy entries."""
    if isinstance(c, dict):
        return {
            "sport_term":   (c.get("sport_term") or "football").lower(),
            "display_name": c.get("display_name") or c.get("league_term") or "",
            "league_term":  c.get("league_term") or "",
        }
    if isinstance(c, (list, tuple)) and len(c) >= 2:
        return {
            "sport_term":   "football",
            "display_name": c[0],
            "league_term":  c[1],
        }
    return None


def discover_matches(competitions=None):
    """List upcoming fixtures across the given competitions (defaults to
    COMPETITIONS). Reference brand (711) first; if a league returns nothing,
    fall back to Unibet.

    `competitions` is a list of dicts {sport_term, display_name, league_term}.
    Legacy 2-tuples (name, term) are accepted and treated as football."""
    if competitions is None:
        competitions = COMPETITIONS
    out = []
    seen_ids = set()
    for raw in competitions:
        c = _normalize_competition(raw)
        if not c or not c["league_term"]:
            continue
        sport_term  = c["sport_term"]
        comp_name   = c["display_name"]
        term        = c["league_term"]
        events = kambi_listview(KAMBI_BRANDS[REFERENCE_OPERATOR], term, sport_term)
        if not events:
            events = kambi_listview(KAMBI_BRANDS[FALLBACK_OPERATOR], term, sport_term)
        for ev in events:
            event = ev.get("event", {})
            state = event.get("state")
            if state and state != "NOT_STARTED":
                continue
            kid = event.get("id")
            if kid in seen_ids:
                continue
            seen_ids.add(kid)
            out.append({
                "sport":           sport_term,
                "competition":     comp_name,
                "league_term":     term,
                "home":            event.get("homeName") or "",
                "away":            event.get("awayName") or "",
                "kickoff_utc_iso": event.get("start") or "",
                "kambi_event_id":  kid,
            })
    out.sort(key=lambda m: (m["competition"], m["kickoff_utc_iso"] or ""))
    return out

# Per-operator sport support. Kambi covers every sport via the same
# betoffer/event API; TOTO and TonyBet have parsers wired explicitly per
# sport. Operators not listed for a given sport are skipped during refresh.
OPERATOR_SUPPORTED_SPORTS = {
    "TOTO.nl":    {"football", "ufc_mma"},
    "TonyBet.nl": {"football", "ufc_mma"},
}

def fetch_all_for_match(match):
    """Run every operator's fetcher for one match. Returns list of odds rows
    annotated with operator + license. Operators are skipped silently if
    they don't have a parser for the match's sport (Kambi brands always run)."""
    sport = (match.get("sport") or "football").lower()

    rows = []
    for name, lic, fn, _ref in OPERATORS:
        # Kambi brands always run — their parser is sport-agnostic. Other
        # operators run only for sports their parser explicitly handles.
        if name not in KAMBI_BRANDS:
            supported = OPERATOR_SUPPORTED_SPORTS.get(name, set())
            if sport not in supported:
                continue
        try:
            opr = fn(operator=name,
                     kambi_event_id=match.get("kambi_event_id"),
                     toto_event_id=match.get("toto_event_id"),
                     home=match.get("home"),
                     away=match.get("away"),
                     kickoff_utc_iso=match.get("kickoff_utc_iso"),
                     league_term=match.get("league_term"),
                     sport=sport)
        except Exception as e:
            opr = [{"market_key": "MATCH_RESULT_FT", "market_label": "Maç Sonucu",
                    "selection_key": "1", "selection_label": "1",
                    "line": None, "odd": None, "ok": False,
                    "note": f"{name} err: {e}"}]
        for r in opr:
            r["operator"] = name
            r["license"] = lic
            rows.append(r)

    return rows
