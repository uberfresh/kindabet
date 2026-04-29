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

export default function SettingsPage() {
  const [available, setAvailable] = useState<LeagueOption[] | null>(null);
  const [selected, setSelected]   = useState<Set<string>>(new Set());
  const [error, setError]         = useState<string | null>(null);
  const [saving, setSaving]       = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);

  // Load both lists on mount.
  useEffect(() => {
    (async () => {
      try {
        const [avail, enabled] = await Promise.all([
          fetchAvailableLeagues(),
          fetchEnabledLeagues(),
        ]);
        setAvailable(avail.leagues);
        setSelected(new Set(enabled.enabled.map((e) => e.league_term)));
      } catch (e) {
        setError((e as Error).message);
      }
    })();
  }, []);

  // Group available leagues by country for cleaner rendering.
  const grouped = useMemo(() => {
    if (!available) return [];
    const m = new Map<string, LeagueOption[]>();
    for (const lg of available) {
      const k = lg.country || "Uluslararası";
      if (!m.has(k)) m.set(k, []);
      m.get(k)!.push(lg);
    }
    return [...m.entries()].sort((a, b) => {
      // International cups first, then alphabetical countries.
      if (a[0] === "Uluslararası") return -1;
      if (b[0] === "Uluslararası") return 1;
      return a[0].localeCompare(b[0], "tr");
    });
  }, [available]);

  const allSelected = !!available && available.length > 0 && available.every((lg) => selected.has(lg.league_term));
  const someSelected = !!available && available.some((lg) => selected.has(lg.league_term));

  const toggle = (term: string) => {
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(term)) n.delete(term); else n.add(term);
      return n;
    });
  };

  const toggleAll = () => {
    setSelected(allSelected ? new Set() : new Set(available!.map((lg) => lg.league_term)));
  };

  const toggleCountry = (group: LeagueOption[]) => {
    setSelected((prev) => {
      const allInGroup = group.every((g) => prev.has(g.league_term));
      const n = new Set(prev);
      group.forEach((g) => allInGroup ? n.delete(g.league_term) : n.add(g.league_term));
      return n;
    });
  };

  const onSave = async () => {
    if (!available) return;
    setSaving(true);
    setError(null);
    try {
      const toSave = available
        .filter((lg) => selected.has(lg.league_term))
        .map((lg) => ({ league_term: lg.league_term, display_name: lg.display_name }));
      await saveEnabledLeagues(toSave);
      setSavedFlash(true);
      window.setTimeout(() => setSavedFlash(false), 2500);
      // Kick off a fresh scan with the new league set; show the modal.
      await startRefreshAll();
      setModalOpen(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Topbar onJobComplete={() => {}} />
      <main>
        <header className="page-head">
          <h1 className="page-title">Ayarlar</h1>
          <p className="page-sub muted">
            Taranacak ligleri seç. Kaydettiğinde tüm maçlar yeniden taranır.
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
                  ref={(el) => { if (el) el.indeterminate = !allSelected && someSelected; }}
                  onChange={toggleAll}
                />
                <span><strong>Tümünü seç</strong> ({selected.size} / {available.length})</span>
              </label>
              <div className="settings-actions">
                {savedFlash && <span className="muted small">Kaydedildi ✓</span>}
                <button
                  className="btn"
                  onClick={onSave}
                  disabled={saving || selected.size === 0}
                  title={selected.size === 0 ? "En az bir lig seçmelisin" : "Kaydet ve yeni taramayı başlat"}
                >
                  {saving ? "Kaydediliyor…" : "Kaydet ve Tara"}
                </button>
              </div>
            </div>

            <div className="settings-groups">
              {grouped.map(([country, leagues]) => {
                const allInGroup = leagues.every((lg) => selected.has(lg.league_term));
                const someInGroup = leagues.some((lg) => selected.has(lg.league_term));
                return (
                  <section className="settings-group" key={country}>
                    <header className="settings-group-head">
                      <label className="check-row">
                        <input
                          type="checkbox"
                          checked={allInGroup}
                          ref={(el) => { if (el) el.indeterminate = !allInGroup && someInGroup; }}
                          onChange={() => toggleCountry(leagues)}
                        />
                        <span className="settings-group-name">
                          {country}
                          <span className="muted small"> · {leagues.length}</span>
                        </span>
                      </label>
                    </header>
                    <div className="settings-leagues">
                      {leagues.map((lg) => (
                        <label key={lg.league_term} className="check-row">
                          <input
                            type="checkbox"
                            checked={selected.has(lg.league_term)}
                            onChange={() => toggle(lg.league_term)}
                          />
                          <span className="settings-league-name">{lg.display_name}</span>
                          <span className="muted small mono">{lg.league_term}</span>
                        </label>
                      ))}
                    </div>
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
