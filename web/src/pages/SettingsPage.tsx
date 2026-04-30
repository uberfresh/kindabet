import { useEffect, useMemo, useRef, useState } from "react";
import {
  fetchAvailableLeagues,
  fetchEnabledLeagues,
  getRefreshAllStatus,
  saveEnabledLeagues,
  startRefreshAll,
  type LeagueOption,
  type RefreshAllStatus,
} from "../api";
import { fmtRelative, sportEmoji } from "../format";
import { Topbar } from "../components/Topbar";
import { RefreshModal } from "../components/RefreshModal";

// Top-of-list ordering for the sports the site cares about most. Anything
// not in this list sorts after, alphabetical by Turkish name.
const SPORT_PRIORITY = [
  "football", "basketball", "tennis", "ice_hockey", "volleyball",
  "handball", "ufc_mma", "american_football", "baseball", "rugby_union",
  "rugby_league", "snooker", "darts", "cricket", "boxing",
];

// Compound key disambiguates leagues that share a league_term across sports
// (e.g. "champions_league" is both a football AND a handball league).
const compositeKey = (sport_term: string, league_term: string) =>
  `${sport_term}:${league_term}`;

// localStorage cache for the heavy /api/leagues/available payload (~50KB).
// Refresh in the background on every mount so we still pick up new leagues
// added upstream, but the user gets an instant render.
const CACHE_KEY = "kinda_bet_available_leagues_v1";
const CACHE_TTL_MS = 24 * 60 * 60 * 1000;

function readCachedAvailable(): LeagueOption[] | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const { cached_at, leagues } = JSON.parse(raw);
    if (!Array.isArray(leagues)) return null;
    if (Date.now() - new Date(cached_at).getTime() > CACHE_TTL_MS) return null;
    return leagues;
  } catch {
    return null;
  }
}

function writeCachedAvailable(leagues: LeagueOption[]) {
  try {
    localStorage.setItem(
      CACHE_KEY,
      JSON.stringify({ cached_at: new Date().toISOString(), leagues })
    );
  } catch { /* quota / private mode — silently ignore */ }
}

type CountryGroup = {
  country: string;
  leagues: LeagueOption[];
};

type SportGroup = {
  sport_term: string;
  sport_name_tr: string;
  countries: CountryGroup[];
  totalLeagues: number;
};

export default function SettingsPage() {
  const [available, setAvailable] = useState<LeagueOption[] | null>(() => readCachedAvailable());
  const [selected, setSelected]   = useState<Set<string>>(new Set());  // composite keys
  const [error, setError]         = useState<string | null>(null);
  const [saving, setSaving]       = useState(false);
  const [scanning, setScanning]   = useState(false);
  const [refreshingCatalog, setRefreshingCatalog] = useState(false);
  const [catalogFlash, setCatalogFlash] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);
  const [expanded, setExpanded]   = useState<Set<string>>(new Set());
  const [query, setQuery]         = useState("");
  const [scanStatus, setScanStatus] = useState<RefreshAllStatus | null>(null);
  const [, setTick] = useState(0);
  const pollRef = useRef<number | null>(null);

  // Keep relative-time strings ("5 dk önce") fresh.
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, []);

  // Initial load: enabled leagues from server (always fresh — they're tiny);
  // available leagues from cache if we have it (instant render), then
  // refetch in the background so newly-discovered leagues get picked up.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const enabled = await fetchEnabledLeagues();
        if (cancelled) return;
        const enabledKeys = new Set(
          enabled.enabled.map((e) => compositeKey(e.sport_term || "football", e.league_term))
        );
        setSelected(enabledKeys);
        const enabledSports = new Set<string>();
        for (const e of enabled.enabled) enabledSports.add(e.sport_term || "football");
        enabledSports.add("football");
        setExpanded(enabledSports);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }

      // Available leagues: refetch in background regardless of cache state.
      // If cache miss, this is the first fetch and the user sees the skeleton
      // until it lands. If cache hit, the UI already rendered — we're just
      // refreshing data in place.
      try {
        const avail = await fetchAvailableLeagues();
        if (cancelled) return;
        setAvailable(avail.leagues);
        writeCachedAvailable(avail.leagues);
      } catch (e) {
        if (!cancelled && !available) setError((e as Error).message);
      }
    })();

    // Initial scan-status fetch.
    getRefreshAllStatus().then((s) => { if (!cancelled) setScanStatus(s); }).catch(() => {});

    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Poll status when a scan is running so the bottom bar info updates.
  useEffect(() => {
    if (!scanStatus?.running) {
      if (pollRef.current != null) { clearInterval(pollRef.current); pollRef.current = null; }
      return;
    }
    if (pollRef.current != null) return;
    pollRef.current = window.setInterval(async () => {
      try {
        const s = await getRefreshAllStatus();
        setScanStatus(s);
      } catch {/* swallow */}
    }, 2000);
    return () => {
      if (pollRef.current != null) { clearInterval(pollRef.current); pollRef.current = null; }
    };
  }, [scanStatus?.running]);

  // Group available leagues by sport, then by country within each sport.
  const sportGroups = useMemo<SportGroup[]>(() => {
    if (!available) return [];

    const m = new Map<string, { name_tr: string; byCountry: Map<string, LeagueOption[]> }>();
    for (const lg of available) {
      const sportKey = lg.sport_term || "football";
      let s = m.get(sportKey);
      if (!s) {
        s = { name_tr: lg.sport_name_tr || sportKey, byCountry: new Map() };
        m.set(sportKey, s);
      }
      const country = lg.country || "Uluslararası";
      if (!s.byCountry.has(country)) s.byCountry.set(country, []);
      s.byCountry.get(country)!.push(lg);
    }

    const out: SportGroup[] = [];
    for (const [sport_term, s] of m) {
      const countries: CountryGroup[] = [];
      for (const [country, leagues] of s.byCountry) {
        leagues.sort((a, b) => (a.display_name || "").localeCompare(b.display_name || "", "tr"));
        countries.push({ country, leagues });
      }
      countries.sort((a, b) => {
        if (a.country === "Uluslararası") return -1;
        if (b.country === "Uluslararası") return 1;
        return a.country.localeCompare(b.country, "tr");
      });
      const totalLeagues = countries.reduce((acc, c) => acc + c.leagues.length, 0);
      out.push({ sport_term, sport_name_tr: s.name_tr, countries, totalLeagues });
    }

    return out.sort((a, b) => {
      const ap = SPORT_PRIORITY.indexOf(a.sport_term);
      const bp = SPORT_PRIORITY.indexOf(b.sport_term);
      if (ap !== -1 && bp !== -1) return ap - bp;
      if (ap !== -1) return -1;
      if (bp !== -1) return 1;
      return a.sport_name_tr.localeCompare(b.sport_name_tr, "tr");
    });
  }, [available]);

  const filteredSportGroups = useMemo<SportGroup[]>(() => {
    if (!query.trim()) return sportGroups;
    const q = query.trim().toLocaleLowerCase("tr-TR");
    const out: SportGroup[] = [];
    for (const g of sportGroups) {
      const sportMatches = g.sport_name_tr.toLocaleLowerCase("tr-TR").includes(q);
      const countries: CountryGroup[] = [];
      for (const c of g.countries) {
        const countryMatches = c.country.toLocaleLowerCase("tr-TR").includes(q);
        const matchedLeagues = (sportMatches || countryMatches)
          ? c.leagues
          : c.leagues.filter((lg) =>
              lg.display_name.toLocaleLowerCase("tr-TR").includes(q));
        if (matchedLeagues.length > 0) {
          countries.push({ country: c.country, leagues: matchedLeagues });
        }
      }
      if (countries.length > 0) {
        out.push({ ...g, countries, totalLeagues: countries.reduce((a, c) => a + c.leagues.length, 0) });
      }
    }
    return out;
  }, [sportGroups, query]);

  const toggleLeague = (sport_term: string, league_term: string) => {
    const k = compositeKey(sport_term, league_term);
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(k)) n.delete(k); else n.add(k);
      return n;
    });
  };

  const toggleAll = () => {
    if (!available) return;
    const allKeys = available.map((lg) => compositeKey(lg.sport_term, lg.league_term));
    const allOn = allKeys.length > 0 && allKeys.every((k) => selected.has(k));
    setSelected(allOn ? new Set() : new Set(allKeys));
  };

  const toggleSport = (g: SportGroup) => {
    const keys = g.countries.flatMap((c) =>
      c.leagues.map((lg) => compositeKey(lg.sport_term, lg.league_term)));
    setSelected((prev) => {
      const allOn = keys.every((k) => prev.has(k));
      const n = new Set(prev);
      keys.forEach((k) => allOn ? n.delete(k) : n.add(k));
      return n;
    });
  };

  const toggleCountry = (sport_term: string, c: CountryGroup) => {
    const keys = c.leagues.map((lg) => compositeKey(sport_term, lg.league_term));
    setSelected((prev) => {
      const allOn = keys.every((k) => prev.has(k));
      const n = new Set(prev);
      keys.forEach((k) => allOn ? n.delete(k) : n.add(k));
      return n;
    });
  };

  const toggleExpand = (sport_term: string) => {
    setExpanded((prev) => {
      const n = new Set(prev);
      if (n.has(sport_term)) n.delete(sport_term); else n.add(sport_term);
      return n;
    });
  };

  useEffect(() => {
    if (!query.trim()) return;
    setExpanded(new Set(filteredSportGroups.map((g) => g.sport_term)));
  }, [query, filteredSportGroups]);

  const onSave = async () => {
    if (!available) return;
    setSaving(true);
    setError(null);
    try {
      const toSave = available
        .filter((lg) => selected.has(compositeKey(lg.sport_term, lg.league_term)))
        .map((lg) => ({
          sport_term:   lg.sport_term,
          league_term:  lg.league_term,
          display_name: lg.display_name,
        }));
      await saveEnabledLeagues(toSave);
      setSavedFlash(true);
      window.setTimeout(() => setSavedFlash(false), 2500);
      await startRefreshAll();
      setModalOpen(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const onScanAll = async () => {
    setScanning(true);
    try {
      const s = await startRefreshAll();
      setScanStatus(s);
      setModalOpen(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setScanning(false);
    }
  };

  const onRefreshCatalog = async () => {
    // Force-refetch /api/leagues/available — busts both the localStorage
    // cache here and the in-memory + on-disk caches in scrapers.py. Useful
    // when Kambi adds a new league upstream and the user wants to see it
    // without waiting for the TTL to expire.
    setRefreshingCatalog(true);
    setCatalogFlash(null);
    try {
      try { localStorage.removeItem(CACHE_KEY); } catch {}
      const before = available?.length ?? 0;
      const avail = await fetchAvailableLeagues(true);
      setAvailable(avail.leagues);
      writeCachedAvailable(avail.leagues);
      const after = avail.leagues.length;
      const delta = after - before;
      setCatalogFlash(
        delta > 0
          ? `${after} lig (+${delta} yeni)`
          : `${after} lig — değişiklik yok`
      );
      window.setTimeout(() => setCatalogFlash(null), 4000);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRefreshingCatalog(false);
    }
  };

  const totalSelected = selected.size;
  const totalAvailable = available?.length ?? 0;
  const allSelected = totalAvailable > 0 && totalSelected === totalAvailable;
  const someSelected = totalSelected > 0 && !allSelected;

  const lastScan = scanStatus?.finished_at ? fmtRelative(scanStatus.finished_at) : "";
  const nextScan = scanStatus?.next_scheduled_at ? fmtRelative(scanStatus.next_scheduled_at) : "";
  const intervalH = Math.round((scanStatus?.auto_refresh_interval_seconds ?? 3600) / 3600);

  return (
    <>
      <Topbar onJobComplete={() => {
        getRefreshAllStatus().then((s) => setScanStatus(s)).catch(() => {});
      }} />
      <main className="settings-page">
        <header className="settings-header">
          <div className="settings-title-row">
            <h1 className="page-title">Ayarlar</h1>
            <label className="settings-master">
              <input
                type="checkbox"
                checked={allSelected}
                ref={(el) => { if (el) el.indeterminate = someSelected; }}
                onChange={toggleAll}
                disabled={!available}
              />
              <span>Tümünü seç <span className="muted">({totalSelected} / {totalAvailable})</span></span>
            </label>
          </div>

          <div className="scan-info-card">
            <div className="scan-info-row">
              <span className="scan-info-icon" aria-hidden="true">⏱</span>
              <div className="scan-info-text">
                <div className="scan-info-line">
                  {scanStatus?.running ? (
                    <>
                      <span className="pulse" />
                      <strong>Tarama sürüyor</strong> — {scanStatus.completed}/{scanStatus.total}
                      {scanStatus.scope?.startsWith("sport:") && (
                        <span className="muted small"> ({scanStatus.scope.slice(6)})</span>
                      )}
                    </>
                  ) : lastScan ? (
                    <>
                      Son tarama: <strong>{lastScan}</strong>
                      {nextScan && <> · Sonraki: <strong>{nextScan}</strong></>}
                    </>
                  ) : (
                    <span className="muted">Henüz tarama yapılmadı.</span>
                  )}
                </div>
                <div className="scan-info-sub muted small">
                  Otomatik tarama her {intervalH} saatte bir çalışır. İstediğin zaman aşağıdan elle de tarayabilirsin.
                </div>
              </div>
            </div>
          </div>
        </header>

        {error && (
          <div className="empty" role="alert">Yükleme başarısız: {error}</div>
        )}

        {!available && !error && (
          <div className="settings-skeleton">
            {[0,1,2,3].map(i => <div key={i} className="skel skel-league" />)}
          </div>
        )}

        {available && (
          <>
            {filteredSportGroups.length === 0 && query.trim() && (
              <div className="empty">"{query}" için lig bulunamadı.</div>
            )}

            <div className="settings-sports">
              {filteredSportGroups.map((g) => {
                const allKeysInSport = g.countries.flatMap((c) =>
                  c.leagues.map((lg) => compositeKey(g.sport_term, lg.league_term)));
                const selCount = allKeysInSport.filter((k) => selected.has(k)).length;
                const allOn = allKeysInSport.length > 0 && selCount === allKeysInSport.length;
                const someOn = selCount > 0 && !allOn;
                const isExpanded = expanded.has(g.sport_term);
                return (
                  <section
                    className={`settings-sport ${isExpanded ? "open" : "closed"} ${selCount > 0 ? "has-selected" : ""}`}
                    key={g.sport_term}
                  >
                    <div className="settings-sport-head">
                      <button
                        className="settings-sport-toggle"
                        type="button"
                        aria-expanded={isExpanded}
                        aria-controls={`sport-${g.sport_term}`}
                        onClick={() => toggleExpand(g.sport_term)}
                      >
                        <span className="settings-sport-chevron" aria-hidden="true">
                          {isExpanded ? "▾" : "▸"}
                        </span>
                        <span className="settings-sport-icon" aria-hidden="true">
                          {sportEmoji(g.sport_term)}
                        </span>
                        <span className="settings-sport-name">{g.sport_name_tr}</span>
                        <span className="settings-sport-meta muted small">
                          {selCount} / {g.totalLeagues}
                        </span>
                      </button>
                      <label className="settings-sport-master" onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={allOn}
                          ref={(el) => { if (el) el.indeterminate = someOn; }}
                          onChange={() => toggleSport(g)}
                          aria-label={`${g.sport_name_tr} — tümünü seç`}
                        />
                      </label>
                    </div>
                    {isExpanded && (
                      <div className="settings-sport-body" id={`sport-${g.sport_term}`}>
                        {g.countries.map((c) => {
                          const keys = c.leagues.map((lg) => compositeKey(g.sport_term, lg.league_term));
                          const cSel = keys.filter((k) => selected.has(k)).length;
                          const cAll = cSel === keys.length;
                          const cSome = cSel > 0 && !cAll;
                          return (
                            <div className="settings-country" key={c.country}>
                              <div className="settings-country-head">
                                <label className="check-row tight" onClick={(e) => e.stopPropagation()}>
                                  <input
                                    type="checkbox"
                                    checked={cAll}
                                    ref={(el) => { if (el) el.indeterminate = cSome; }}
                                    onChange={() => toggleCountry(g.sport_term, c)}
                                  />
                                  <span className="settings-country-name">{c.country}</span>
                                  <span className="muted small">
                                    {cSel} / {keys.length}
                                  </span>
                                </label>
                              </div>
                              <div className="settings-country-leagues">
                                {c.leagues.map((lg) => (
                                  <label
                                    key={`${g.sport_term}:${lg.league_term}`}
                                    className="check-row"
                                  >
                                    <input
                                      type="checkbox"
                                      checked={selected.has(compositeKey(g.sport_term, lg.league_term))}
                                      onChange={() => toggleLeague(g.sport_term, lg.league_term)}
                                    />
                                    {lg.logo_url
                                      ? <img className="settings-league-logo" src={lg.logo_url} alt="" loading="lazy" />
                                      : <span className="settings-league-logo-blank" aria-hidden="true" />}
                                    <span className="settings-league-name">{lg.display_name}</span>
                                  </label>
                                ))}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </section>
                );
              })}
            </div>
          </>
        )}
      </main>

      {/* Sticky bottom action bar — three distinct actions, ordered by
          frequency (rarest left, primary right). Mobile-first surface so
          the user can always trigger any action without scrolling. */}
      <div className="settings-actionbar">
        <input
          className="settings-search"
          type="search"
          placeholder="Spor, ülke veya lig ara…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Lig ara"
        />
        <div className="settings-actionbar-buttons">
          {catalogFlash && <span className="muted small saved-flash">{catalogFlash}</span>}
          {savedFlash && <span className="muted small saved-flash">Kaydedildi ✓</span>}
          <button
            className="btn ghost"
            onClick={onRefreshCatalog}
            disabled={refreshingCatalog}
            title="Kambi'den lig kataloğunu yeniden çek (yeni eklenen ligleri göstermek için)"
          >
            {refreshingCatalog ? "Yenileniyor…" : "🗂 Sport Kategorilerini Yenile"}
          </button>
          <button
            className="btn ghost"
            onClick={onScanAll}
            disabled={scanning || scanStatus?.running || !available}
            title="Halihazırda seçili ligler için tüm operatörlerden oranları yeniden çek"
          >
            {scanning || scanStatus?.running ? "Taranıyor…" : "↻ Maç Oranlarını Tara"}
          </button>
          <button
            className="btn"
            onClick={onSave}
            disabled={saving || !available}
            title="Yaptığın değişiklikleri kaydet ve yeni seçimle birlikte taramayı başlat"
          >
            {saving ? "Kaydediliyor…" : "💾 Kaydet ve Tara"}
          </button>
        </div>
      </div>

      <RefreshModal open={modalOpen} onClose={() => {
        setModalOpen(false);
        // Refresh status display once the modal closes so "son tarama" updates.
        getRefreshAllStatus().then((s) => setScanStatus(s)).catch(() => {});
      }} />
    </>
  );
}
