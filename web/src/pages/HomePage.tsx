import { useEffect, useMemo, useState } from "react";
import { fetchMatches, type MatchesResponse, type League, type MatchSummary } from "../api";
import { norm } from "../format";
import { Topbar } from "../components/Topbar";
import { LeagueCard } from "../components/LeagueCard";
import { FeaturedStrip } from "../components/FeaturedStrip";
import { SearchBar } from "../components/SearchBar";

function useDebounced<T>(value: T, delay = 150): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return v;
}

function filterLeagues(leagues: League[], query: string): League[] {
  if (!query.trim()) return leagues;
  const q = norm(query);
  const out: League[] = [];
  for (const lg of leagues) {
    const matches = lg.matches.filter(
      (m) => norm(m.home).includes(q) || norm(m.away).includes(q)
    );
    if (matches.length) out.push({ ...lg, matches });
  }
  return out;
}

function flattenMatches(leagues: League[]): MatchSummary[] {
  return leagues.flatMap((l) => l.matches);
}

export default function HomePage() {
  const [data, setData] = useState<MatchesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const debouncedSearch = useDebounced(search, 150);

  const load = async () => {
    try {
      const d = await fetchMatches(false);
      setData(d);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const filtered = useMemo(
    () => (data ? filterLeagues(data.leagues, debouncedSearch) : []),
    [data, debouncedSearch]
  );
  const totalMatches = data?.leagues.reduce((n, l) => n + l.matches.length, 0) ?? 0;
  const matchedTotal = filtered.reduce((n, l) => n + l.matches.length, 0);

  return (
    <>
      <Topbar onJobComplete={load} />
      <main>
        <div className="home-search">
          <SearchBar value={search} onChange={setSearch} />
        </div>

        {error && (
          <div className="empty" role="alert">
            Yükleme başarısız: {error}
          </div>
        )}

        {!data && !error && <HomeSkeleton />}

        {data && totalMatches === 0 && (
          <div className="empty">
            Henüz yaklaşan maç yok — “↻ Maçları Yenile” butonuna tıklayın.
          </div>
        )}

        {data && totalMatches > 0 && filtered.length === 0 && (
          <div className="empty">
            “{debouncedSearch}” araması için sonuç yok ({totalMatches} maç içinden).
          </div>
        )}

        {data && !debouncedSearch && filtered.length > 0 && (
          <FeaturedStrip matches={flattenMatches(filtered)} />
        )}

        {data && debouncedSearch && filtered.length > 0 && (
          <div className="muted small search-summary">
            “{debouncedSearch}” için <strong>{matchedTotal}</strong> sonuç ({totalMatches} maç içinden)
          </div>
        )}

        <div className="leagues">
          {filtered.map((lg) => (
            <LeagueCard key={lg.competition} league={lg} defaultOpen />
          ))}
        </div>
      </main>
    </>
  );
}

function HomeSkeleton() {
  return (
    <>
      <div className="featured">
        <div className="featured-head">
          <div className="skel skel-title" />
        </div>
        <div className="featured-grid">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="skel skel-card" />
          ))}
        </div>
      </div>
      <div className="leagues">
        {[0, 1].map((i) => (
          <div key={i} className="skel skel-league" />
        ))}
      </div>
    </>
  );
}
