import { Link } from "react-router-dom";
import type { MatchSummary } from "../api";
import { fmtKickoffSmart, leagueLogoPath, leaguePillMeta } from "../format";

type Props = {
  match: MatchSummary;
  showLeaguePill?: boolean;  // featured strip shows it; league lists hide it
};

function OddBtn({ label, value }: { label: string; value: number | undefined }) {
  const has = value != null;
  return (
    <div className={"odd-btn" + (has ? "" : " na")}>
      <span className="odd-btn-label">{label}</span>
      <span className="odd-btn-value">{has ? value!.toFixed(2) : "—"}</span>
    </div>
  );
}

export function MatchCard({ match, showLeaguePill = false }: Props) {
  const { day, time } = fmtKickoffSmart(match.kickoff_utc);
  const [pillText, pillCls] = leaguePillMeta(match.competition);
  const logo = match.logo_url || leagueLogoPath(match.competition);
  const odds = match.headline_odds;
  const ou = match.over_under_2_5;
  const count = match.market_count ?? 0;
  // Strict football detection: only treat as football when sport is explicitly
  // "football". Anything else (UFC, basketball, …) — and any match where the
  // sport tag is missing — drops the football-shaped 1X2/OU layout.
  const isFootball = match.sport === "football";
  // Three-way (1/X/2) header has the X selection; two-way (UFC, tennis, …) doesn't.
  const isThreeWay = !!(odds && odds.X != null);
  const hasAnyOdd  = !!(odds && (odds["1"] != null || odds.X != null || odds["2"] != null));

  return (
    <Link to={`/match/${match.id}`} className="card-link">
      <article className="match-row">
        <div className="match-row-when">
          <span className="match-row-day">{day}</span>
          <span className="match-row-time">{time}</span>
          {showLeaguePill && (logo
            ? <img className="match-row-logo" src={logo} alt="" loading="lazy" />
            : pillCls && <span className={`league-pill ${pillCls}`}>{pillText}</span>
          )}
          {!isFootball && match.sport_name_tr && (
            <span className="sport-pill" title={match.sport}>{match.sport_name_tr}</span>
          )}
        </div>

        <div className="match-row-teams">
          <span className="match-row-team home">{match.home}</span>
          <span className="match-row-team away">{match.away}</span>
        </div>

        <div className="match-row-markets">
          {hasAnyOdd ? (
            <div className="market-block">
              <span className="market-block-head muted small">
                {isThreeWay ? "Maç Sonucu" : "Kazanan"}
              </span>
              <div className={"market-block-buttons " + (isThreeWay ? "cols-3" : "cols-2")}>
                <OddBtn label="1" value={odds?.["1"]} />
                {isThreeWay && <OddBtn label="X" value={odds?.X} />}
                <OddBtn label="2" value={odds?.["2"]} />
              </div>
            </div>
          ) : (
            <span className="match-row-detail-hint muted small">
              {count > 0 ? "Detay için tıkla" : "Henüz oran yok"}
            </span>
          )}

          {/* Football-only second block: Alt/Üst 2.5. Other sports' totals
              are sport-specific (rounds, sets, points) — handled on the
              detail page rather than the home card. */}
          {isFootball && hasAnyOdd && (
            <div className="market-block">
              <span className="market-block-head muted small">Alt / Üst 2.5</span>
              <div className="market-block-buttons cols-2">
                <OddBtn label="Üst" value={ou?.OVER} />
                <OddBtn label="Alt" value={ou?.UNDER} />
              </div>
            </div>
          )}
        </div>

        <span
          className={"match-row-count" + (count > 0 ? "" : " empty")}
          title={count > 0 ? `${count} market mevcut` : "Henüz market yok"}
        >
          {count > 0 ? `+${count}` : "—"}
        </span>
      </article>
    </Link>
  );
}
