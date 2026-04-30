import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { fetchMatch, refreshMatch, type MatchDetail } from "../api";
import { fmtKickoffSmart, fmtTs, leaguePillMeta } from "../format";
import { MarketRow } from "../components/MarketRow";
import { BestPricesPanel } from "../components/BestPricesPanel";

export default function MatchPage() {
  const { id } = useParams<{ id: string }>();
  const [detail, setDetail] = useState<MatchDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [status, setStatus] = useState("");

  const matchId = Number(id);

  const load = async () => {
    try {
      const d = await fetchMatch(matchId);
      setDetail(d);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    if (!Number.isFinite(matchId)) return;
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matchId]);

  const onRefresh = async () => {
    setRefreshing(true);
    setStatus("711, Unibet, TOTO ve TonyBet taranıyor… (~30 sn)");
    try {
      await refreshMatch(matchId);
      await load();
      setStatus("Yenileme tamamlandı");
      setTimeout(() => setStatus(""), 3500);
    } catch (e) {
      setStatus("Yenileme başarısız: " + (e as Error).message);
    } finally {
      setRefreshing(false);
    }
  };

  if (!Number.isFinite(matchId)) {
    return (
      <main className="match-page">
        <div className="empty">Geçersiz maç. <Link to="/">Ana sayfaya dön</Link>.</div>
      </main>
    );
  }

  if (error) {
    return (
      <main className="match-page">
        <div className="empty">Yükleme başarısız: {error}. <Link to="/">Geri dön</Link>.</div>
      </main>
    );
  }

  if (!detail) return <MatchPageSkeleton />;

  const m = detail.match;
  const { day, time } = fmtKickoffSmart(m.kickoff_utc);
  const [pillText, pillCls] = leaguePillMeta(m.competition);

  return (
    <main className="match-page">
      <div className="match-back">
        <Link to="/" className="btn ghost">← Tüm Maçlar</Link>
      </div>

      <header className="match-hero">
        <div className="match-hero-meta">
          {pillCls && <span className={`league-pill ${pillCls}`}>{pillText}</span>}
          <span className="match-hero-comp muted">{m.competition}</span>
          <span className="match-hero-when">
            <strong>{day}</strong> · {time}
          </span>
        </div>
        <h1 className="match-hero-teams">
          <span className="match-hero-team home">{m.home}</span>
          <span className="match-hero-vs">–</span>
          <span className="match-hero-team away">{m.away}</span>
        </h1>
        <div className="match-hero-actions">
          <button className="btn" onClick={onRefresh} disabled={refreshing}>
            {refreshing && <span className="pulse" />}
            ↻ Oranları Yenile
          </button>
          <span className="muted small">
            {status ||
              (detail.last_refresh
                ? `Son güncelleme ${fmtTs(detail.last_refresh)}`
                : "Henüz oran yok — yenileyerek başla.")}
          </span>
        </div>
      </header>

      {detail.markets.length > 0 && <BestPricesPanel detail={detail} />}

      <section className="markets-section">
        <div className="section-head">
          <h2 className="section-title">Tüm Marketler</h2>
          <span className="muted small">{detail.markets.length} market</span>
        </div>
        <div className="markets">
          {detail.markets.length === 0 ? (
            <div className="empty">
              Henüz market yok. ↻ Oranları Yenile butonuna tıklayın.
            </div>
          ) : (
            detail.markets.map((mkt) => (
              <MarketRow
                key={mkt.market_key}
                matchId={matchId}
                market={mkt}
                referenceOperator={detail.reference_operator}
                allOperators={detail.operators}
              />
            ))
          )}
        </div>
      </section>
    </main>
  );
}

function MatchPageSkeleton() {
  return (
    <main className="match-page">
      <div className="match-back">
        <div className="skel skel-back" />
      </div>
      <div className="match-hero">
        <div className="skel skel-line short" />
        <div className="skel skel-hero-title" />
        <div className="skel skel-line short" />
      </div>
      <div className="skel skel-best" />
      <div className="markets">
        {[0, 1, 2, 3, 4].map((i) => (
          <div key={i} className="skel skel-market" />
        ))}
      </div>
    </main>
  );
}
