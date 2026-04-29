import type { MatchSummary } from "../api";
import { MatchCard } from "./MatchCard";

type Props = { matches: MatchSummary[] };

// Show the next few matches across all leagues, prioritizing the marquee
// competitions (UCL > UEL > EPL > Süper > 1. Lig) and earlier kickoffs.
const COMP_PRIORITY: Record<string, number> = {
  UEFA: 0,                  // matches anything starting with UEFA (Şampiyonlar/Avrupa)
  Premier: 1,
  Süper: 2,
  "1. Lig": 3,
};

function priority(comp: string): number {
  for (const [k, v] of Object.entries(COMP_PRIORITY)) {
    if (comp.includes(k)) return v;
  }
  return 99;
}

export function FeaturedStrip({ matches }: Props) {
  const sorted = [...matches].sort((a, b) => {
    const pa = priority(a.competition);
    const pb = priority(b.competition);
    if (pa !== pb) return pa - pb;
    return (a.kickoff_utc || "").localeCompare(b.kickoff_utc || "");
  });
  const top = sorted.slice(0, 4);
  if (top.length === 0) return null;

  return (
    <section className="featured">
      <div className="featured-head">
        <h2 className="featured-title">Öne Çıkan Maçlar</h2>
        <span className="featured-sub muted small">En yakın yüksek profilli karşılaşmalar</span>
      </div>
      <div className="featured-grid">
        {top.map((m) => (
          <MatchCard key={m.id} match={m} showLeaguePill />
        ))}
      </div>
    </section>
  );
}
