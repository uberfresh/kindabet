import { useEffect, useMemo, useState } from "react";
import {
  fetchAvailableLeagues,
  fetchEnabledLeagues,
  saveEnabledLeagues,
  startRefreshAll,
  type LeagueOption,
} from "../api";
import { sportEmoji } from "../format";
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
  const [available, setAvailable] = useState<LeagueOption[] | null>(null);
  const [selected, setSelected]   = useState<Set<string>>(new Set());  // composite keys
  const [error, setError]         = useState<string | null>(null);
  const [saving, setSaving]       = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);
  const [expanded, setExpanded]   = useState<Set<string>>(new Set());
  const [query, setQuery]         = useState("");

  // Load both lists on mount.
  useEffect(() => {
    (async () => {
      try {
        const [avail, enabled] = await Promise.all([
          fetchAvailableLeagues(),
          fetchEnabledLeagues(),
        ]);
        setAvailable(avail.leagues);
        const enabledKeys = new Set(
          enabled.enabled.map((e) => compositeKey(e.sport_term || "football", e.league_term))
        );
        setSelected(enabledKeys);
        // Auto-expand: football, plus any sport that has at least one selected league.
        const enabledSports = new Set<string>();
        for (const e of enabled.enabled) enabledSports.add(e.sport_term || "football");
        enabledSports.add("football");
        setExpanded(enabledSports);
      } catch (e) {
        setError((e as Error).message);
      }
    })();
  }, []);

  // Group available leagues by sport, then by country within each sport.
  const sportGroups = useMemo<SportGroup[]>(() => {
    if (!available) return [];

    // Two-level Map: sport_term -> country -> leagues[]
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

    // Flatten into the SportGroup[] shape with sorted countries / leagues.
    const out: SportGroup[] = [];
    for (const [sport_term, s] of m) {
      const countries: CountryGroup[] = [];
      for (const [country, leagues] of s.byCountry) {
        leagues.sort((a, b) => (a.display_name || "").localeCompare(b.display_name || "", "tr"));
        countries.push({ country, leagues });
      }
      // International cups first, then alphabetical countries.
      countries.sort((a, b) => {
        if (a.country === "Uluslararası") return -1;
        if (b.country === "Uluslararası") return 1;
        return a.country.localeCompare(b.country, "tr");
      });
      const totalLeagues = countries.reduce((acc, c) => acc + c.leagues.length, 0);
      out.push({ sport_term, sport_name_tr: s.name_tr, countries, totalLeagues });
    }

    // Sort sports by priority, then alphabetically.
    return out.sort((a, b) => {
      const ap = SPORT_PRIORITY.indexOf(a.sport_term);
      const bp = SPORT_PRIORITY.indexOf(b.sport_term);
      if (ap !== -1 && bp !== -1) return ap - bp;
      if (ap !== -1) return -1;
      if (bp !== -1) return 1;
      return a.sport_name_tr.localeCompare(b.sport_name_tr, "tr");
    });
  }, [available]);

  // Filter by search query — match against league display_name, country, sport name.
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

  // When the user types a query, auto-expand every group with matches.
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
      // Kick off a fresh sweep so newly enabled leagues populate immediately.
      await startRefreshAll();
      setModalOpen(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const totalSelected = selected.size;
  const totalAvailable = available?.length ?? 0;
  const allSelected = totalAvailable > 0 && totalSelected === totalAvailable;
  const someSelected = totalSelected > 0 && !allSelected;

  return (
    <>
      <Topbar onJobComplete={() => {}} />
      <main>
        <header className="page-head">
          <h1 className="page-title">Ayarlar</h1>
          <p className="page-sub muted">
            Taranacak sporları ve ligleri seç. Kaydettiğinde tüm maçlar yeniden taranır.
          </p>
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
            <div className="settings-toolbar">
              <label className="check-row master">
                <input
                  type="checkbox"
                  checked={allSelected}
                  ref={(el) => { if (el) el.indeterminate = someSelected; }}
                  onChange={toggleAll}
                />
                <span><strong>Tümünü seç</strong> ({totalSelected} / {totalAvailable})</span>
              </label>
              <input
                className="settings-search"
                type="search"
                placeholder="Spor, ülke veya lig ara…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                aria-label="Lig ara"
              />
              <div className="settings-actions">
                {savedFlash && <span className="muted small">Kaydedildi ✓</span>}
                <button
                  className="btn"
                  onClick={onSave}
                  disabled={saving}
                  title={"Kaydet ve yeni taramayı başlat"}
                >
                  {saving ? "Kaydediliyor…" : "Kaydet ve Tara"}
                </button>
              </div>
            </div>

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

      <RefreshModal open={modalOpen} onClose={() => setModalOpen(false)} />
    </>
  );
}
