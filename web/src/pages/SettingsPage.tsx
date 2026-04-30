import { useEffect, useMemo, useState } from "react";
import {
  fetchAvailableLeagues,
  fetchEnabledLeagues,
  saveEnabledLeagues,
  startRefreshAll,
  type LeagueOption,
} from "../api";
import { Topbar } from "../components/Topbar";
import { RefreshModal } from "../components/RefreshModal";

// Top-of-list ordering for the sports the site cares about most. Anything
// not in this list sorts after, alphabetical by Turkish name.
const SPORT_PRIORITY = [
  "football", "basketball", "tennis", "ice_hockey", "volleyball",
  "handball", "ufc_mma", "american_football", "baseball", "rugby_union",
  "rugby_league", "snooker", "darts", "cricket", "boxing",
];

type SportGroup = {
  sport_term: string;
  sport_name_tr: string;
  leagues: LeagueOption[];
};

export default function SettingsPage() {
  const [available, setAvailable] = useState<LeagueOption[] | null>(null);
  const [selected, setSelected]   = useState<Set<string>>(new Set());
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
        const enabledTerms = new Set(enabled.enabled.map((e) => e.league_term));
        setSelected(enabledTerms);
        // Auto-expand: football, plus any sport that has at least one selected league.
        const enabledSports = new Set<string>();
        for (const e of enabled.enabled) if (e.sport_term) enabledSports.add(e.sport_term);
        enabledSports.add("football");
        setExpanded(enabledSports);
      } catch (e) {
        setError((e as Error).message);
      }
    })();
  }, []);

  // Group available leagues by sport. Within sport, sort by country.
  const sportGroups = useMemo<SportGroup[]>(() => {
    if (!available) return [];
    const m = new Map<string, SportGroup>();
    for (const lg of available) {
      const key = lg.sport_term || "football";
      if (!m.has(key)) {
        m.set(key, {
          sport_term: key,
          sport_name_tr: lg.sport_name_tr || key,
          leagues: [],
        });
      }
      m.get(key)!.leagues.push(lg);
    }
    // Sort leagues within each sport: international first, then by country/name.
    for (const g of m.values()) {
      g.leagues.sort((a, b) => {
        const aIntl = a.country === "Uluslararası";
        const bIntl = b.country === "Uluslararası";
        if (aIntl !== bIntl) return aIntl ? -1 : 1;
        return (a.display_name || "").localeCompare(b.display_name || "", "tr");
      });
    }
    // Sort sports by priority order, then alphabetically.
    return [...m.values()].sort((a, b) => {
      const ap = SPORT_PRIORITY.indexOf(a.sport_term);
      const bp = SPORT_PRIORITY.indexOf(b.sport_term);
      if (ap !== -1 && bp !== -1) return ap - bp;
      if (ap !== -1) return -1;
      if (bp !== -1) return 1;
      return a.sport_name_tr.localeCompare(b.sport_name_tr, "tr");
    });
  }, [available]);

  // Filter by search query — match against league display_name and sport name.
  const filteredSportGroups = useMemo<SportGroup[]>(() => {
    if (!query.trim()) return sportGroups;
    const q = query.trim().toLocaleLowerCase("tr-TR");
    const out: SportGroup[] = [];
    for (const g of sportGroups) {
      const sportMatches = g.sport_name_tr.toLocaleLowerCase("tr-TR").includes(q);
      const matchedLeagues = sportMatches
        ? g.leagues
        : g.leagues.filter((lg) =>
            lg.display_name.toLocaleLowerCase("tr-TR").includes(q) ||
            (lg.country || "").toLocaleLowerCase("tr-TR").includes(q));
      if (matchedLeagues.length > 0) {
        out.push({ ...g, leagues: matchedLeagues });
      }
    }
    return out;
  }, [sportGroups, query]);

  const toggle = (term: string) => {
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(term)) n.delete(term); else n.add(term);
      return n;
    });
  };

  const toggleAll = () => {
    if (!available) return;
    const allOn = available.length > 0 && available.every((lg) => selected.has(lg.league_term));
    setSelected(allOn ? new Set() : new Set(available.map((lg) => lg.league_term)));
  };

  const toggleSport = (g: SportGroup) => {
    setSelected((prev) => {
      const allOn = g.leagues.every((lg) => prev.has(lg.league_term));
      const n = new Set(prev);
      g.leagues.forEach((lg) => allOn ? n.delete(lg.league_term) : n.add(lg.league_term));
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
        .filter((lg) => selected.has(lg.league_term))
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
                placeholder="Spor veya lig ara…"
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
                const selCount = g.leagues.filter((lg) => selected.has(lg.league_term)).length;
                const allOn = g.leagues.length > 0 && selCount === g.leagues.length;
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
                        <span className="settings-sport-name">{g.sport_name_tr}</span>
                        <span className="settings-sport-meta muted small">
                          {selCount} / {g.leagues.length}
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
                      <div className="settings-sport-leagues" id={`sport-${g.sport_term}`}>
                        {g.leagues.map((lg) => (
                          <label key={lg.league_term} className="check-row">
                            <input
                              type="checkbox"
                              checked={selected.has(lg.league_term)}
                              onChange={() => toggle(lg.league_term)}
                            />
                            {lg.logo_url
                              ? <img className="settings-league-logo" src={lg.logo_url} alt="" loading="lazy" />
                              : <span className="settings-league-logo-blank" aria-hidden="true" />}
                            <span className="settings-league-name">{lg.display_name}</span>
                          </label>
                        ))}
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
