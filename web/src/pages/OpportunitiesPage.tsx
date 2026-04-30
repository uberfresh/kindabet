import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchBiggestDiffs, type BiggestDiffsResponse, type BiggestDiff } from "../api";
import { fmtKickoffSmart, fmtOdd, fmtRelative, formatMarketLabel, leaguePillMeta } from "../format";
import { Topbar } from "../components/Topbar";

const LIMIT_KEY = "firsatlar_limit";
const LIMIT_OPTIONS = [10, 20, 50, 100] as const;
type Limit = (typeof LIMIT_OPTIONS)[number];

function readSavedLimit(): Limit {
  try {
    const v = Number(localStorage.getItem(LIMIT_KEY));
    if (LIMIT_OPTIONS.includes(v as Limit)) return v as Limit;
  } catch {/* localStorage may be disabled */}
  return 10;
}

export default function OpportunitiesPage() {
  const [data, setData] = useState<BiggestDiffsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [limit, setLimit] = useState<Limit>(readSavedLimit);

  const load = async (l: Limit) => {
    try {
      const d = await fetchBiggestDiffs(l);
      setData(d);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    load(limit);
    try { localStorage.setItem(LIMIT_KEY, String(limit)); } catch {}
  }, [limit]);

  return (
    <>
      <Topbar onJobComplete={() => load(limit)} />
      <main>
        <header className="page-head page-head-row">
          <div>
            <h1 className="page-title">Fırsatlar</h1>
            <p className="page-sub muted">
              Operatörler arasındaki en büyük oran farkları — en yüksek fiyatı sunan kazanır.
            </p>
          </div>
          <label className="limit-picker">
            <span className="muted small">Göster</span>
            <select
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value) as Limit)}
              aria-label="Sonuç sayısı"
            >
              {LIMIT_OPTIONS.map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </label>
        </header>

        {error && <div className="empty">Yükleme başarısız: {error}</div>}

        {!data && !error && (
          <div className="opportunities">
            {[0, 1, 2, 3, 4].map((i) => (
              <div key={i} className="skel skel-opp" />
            ))}
          </div>
        )}

        {data && data.items.length === 0 && (
          <div className="empty">
            Karşılaştırılacak yeterli oran yok. Önce maçları yenile.
          </div>
        )}

        {data && data.items.length > 0 && (
          <>
            <div className="muted small page-meta">
              {data.total_evaluated.toLocaleString("tr-TR")} pazar değerlendirildi · ilk {limit} fırsat
              {data.computed_at && (
                <> · son hesaplama <strong>{fmtRelative(data.computed_at)}</strong></>
              )}
            </div>
            <div className="opportunities">
              {data.items.map((it, i) => (
                <OpportunityRow key={`${it.match_id}-${it.market_key}-${it.selection_key}`} rank={i + 1} item={it} />
              ))}
            </div>
          </>
        )}
      </main>
    </>
  );
}

function OpportunityRow({ rank, item }: { rank: number; item: BiggestDiff }) {
  const { day, time } = fmtKickoffSmart(item.kickoff_utc);
  const [pillText, pillCls] = leaguePillMeta(item.competition);

  return (
    <Link to={`/match/${item.match_id}`} className="card-link">
      <article className="opp-card">
        <div className="opp-rank">#{rank}</div>

        <div className="opp-main">
          <div className="opp-meta">
            {item.logo_url
              ? <img className="opp-league-logo" src={item.logo_url} alt="" loading="lazy" />
              : pillCls && <span className={`league-pill ${pillCls}`}>{pillText}</span>}
            <span className="opp-when">
              <strong>{day}</strong> · {time}
            </span>
          </div>
          <div className="opp-teams">
            <strong>{item.home}</strong>
            <span className="opp-vs">–</span>
            <strong>{item.away}</strong>
          </div>
          <div className="opp-market">
            <span className="opp-market-label">
              {formatMarketLabel({
                market_label: item.market_label,
                market_key: item.market_key,
                line: item.line,
              })}
            </span>
            <span className="opp-sel-pill">{item.selection_label}</span>
          </div>
        </div>

        <div className="opp-prices">
          <div className="opp-price best">
            <span className="opp-price-tag">★ EN İYİ</span>
            <span className="opp-price-op">{item.best_operator}</span>
            <span className="opp-price-val">{fmtOdd(item.best_odd)}</span>
          </div>
          <div className="opp-price worst">
            <span className="opp-price-tag">EN DÜŞÜK</span>
            <span className="opp-price-op">{item.worst_operator}</span>
            <span className="opp-price-val">{fmtOdd(item.worst_odd)}</span>
          </div>
        </div>

        <div className="opp-diff">
          <div className="opp-diff-pct">+{item.diff_pct.toFixed(1)}%</div>
          <div className="opp-diff-label muted small">fark</div>
        </div>
      </article>
    </Link>
  );
}
