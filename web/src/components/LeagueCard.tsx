import { useState } from "react";
import type { League } from "../api";
import { leaguePillMeta } from "../format";
import { MatchCard } from "./MatchCard";

type Props = {
  league: League;
  defaultOpen?: boolean;
};

export function LeagueCard({ league, defaultOpen = true }: Props) {
  const [open, setOpen] = useState(defaultOpen);
  const [pillText, pillCls] = leaguePillMeta(league.competition);

  return (
    <details
      className="league"
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary className="league-summary">
        <span className="caret">▸</span>
        <span className={"league-pill" + (pillCls ? " " + pillCls : "")}>{pillText}</span>
        <span className="league-name">{league.competition}</span>
        <span className="match-count muted small">{league.matches.length} maç</span>
      </summary>
      <div className="league-body">
        <div className="match-list">
          {league.matches.map((m) => (
            <MatchCard key={m.id} match={m} />
          ))}
        </div>
      </div>
    </details>
  );
}
