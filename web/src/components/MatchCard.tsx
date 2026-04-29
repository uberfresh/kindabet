import { Link } from "react-router-dom";
import type { MatchSummary } from "../api";
import { fmtKickoffSmart, leagueLogoPath, leaguePillMeta } from "../format";

type Props = {
  match: MatchSummary;
  showLeaguePill?: boolean;
};

function OddPill({ label, value }: { label: string; value: number | undefined }) {
  const isAvailable = value != null;
  return (
    <div className={"oddpill" + (isAvailable ? "" : " na")}>
      <span className="oddpill-label">{label}</span>
      <span className="oddpill-value">{isAvailable ? value!.toFixed(2) : "—"}</span>
    </div>
  );
}

export function MatchCard({ match, showLeaguePill = false }: Props) {
  const { day, time } = fmtKickoffSmart(match.kickoff_utc);
  const [pillText, pillCls] = leaguePillMeta(match.competition);
  const logo = leagueLogoPath(match.competition);
  const odds = match.headline_odds;
  const hasAnyOdd = !!(odds && (odds["1"] || odds.X || odds["2"]));

  return (
    <Link to={`/match/${match.id}`} className="card-link">
      <article className="match-card">
        <div className="match-card-row1">
          <span className="match-card-day">{day}</span>
          <span className="match-card-time">{time}</span>
          {showLeaguePill && (logo
            ? <img className="match-card-logo" src={logo} alt="" loading="lazy" />
            : pillCls && <span className={`league-pill ${pillCls} match-card-pill`}>{pillText}</span>
          )}
          <span className="match-card-arrow">→</span>
        </div>

        <div className="match-card-teams">
          <span className="match-card-team home">{match.home}</span>
          <span className="match-card-vs">–</span>
          <span className="match-card-team away">{match.away}</span>
        </div>

        <div className="match-card-odds">
          <OddPill label="1" value={odds?.["1"]} />
          <OddPill label="X" value={odds?.X} />
          <OddPill label="2" value={odds?.["2"]} />
        </div>

        {!hasAnyOdd && (
          <div className="match-card-cold-strip">henüz oran yok</div>
        )}
      </article>
    </Link>
  );
}
