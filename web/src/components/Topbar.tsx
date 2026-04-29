import { useEffect, useRef, useState } from "react";
import { Link, NavLink } from "react-router-dom";
import {
  startRefreshAll,
  getRefreshAllStatus,
  type RefreshAllStatus,
} from "../api";
import { fmtRelative } from "../format";

type Props = {
  onJobComplete: () => void;
};

export function Topbar({ onJobComplete }: Props) {
  const [status, setStatus] = useState<RefreshAllStatus | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [, setTick] = useState(0);  // 1Hz tick to keep relative-time live
  const pollRef = useRef<number | null>(null);

  // Re-render once per second so "5 dk önce" → "6 dk önce" stays accurate.
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    let cancelled = false;
    getRefreshAllStatus()
      .then((s) => {
        if (cancelled) return;
        setStatus(s);
        if (s.running) startPolling();
      })
      .catch(() => {});
    return () => {
      cancelled = true;
      stopPolling();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startPolling = () => {
    stopPolling();
    pollRef.current = window.setInterval(async () => {
      try {
        const s = await getRefreshAllStatus();
        setStatus(s);
        if (!s.running) {
          stopPolling();
          onJobComplete();
        }
      } catch {
        /* swallow */
      }
    }, 2000);
  };

  const stopPolling = () => {
    if (pollRef.current != null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const onClick = async () => {
    if (status?.running) return;
    if (!confirming) {
      setConfirming(true);
      window.setTimeout(() => setConfirming(false), 4000);
      return;
    }
    setConfirming(false);
    try {
      const s = await startRefreshAll();
      setStatus(s);
      startPolling();
    } catch (e) {
      setStatus({
        running: false,
        started_at: null,
        finished_at: null,
        total: 0,
        completed: 0,
        failed: 0,
        error: (e as Error).message,
      });
    }
  };

  const running = !!status?.running;
  const total = status?.total ?? 0;
  const done = status?.completed ?? 0;
  const failed = status?.failed ?? 0;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  let label: React.ReactNode;
  let extraClass = "danger";
  if (running) {
    label = (
      <>
        <span className="pulse" />
        {total > 0 ? `Yenileniyor… ${done}/${total}` : "Başlatılıyor…"}
      </>
    );
    extraClass = "danger running";
  } else if (confirming) {
    label = (
      <>
        <span className="warn-icon">⚠</span>
        Eminim, başlat
      </>
    );
    extraClass = "danger confirming";
  } else {
    label = (
      <>
        <span className="warn-icon">⚠</span>
        Hepsini Yenile
      </>
    );
  }

  // "Son tarama" / "Sonraki tarama" — derived from job state. Only show when
  // we have a finished_at, otherwise the data hasn't been seeded yet.
  const lastScan = status?.finished_at ? fmtRelative(status.finished_at) : "";
  const nextScan = status?.next_scheduled_at ? fmtRelative(status.next_scheduled_at) : "";

  return (
    <header className="topbar">
      <Link to="/" className="brand">
        <img className="logo" src="/kinda.png" alt="Kinda Bet" />
        <span className="brand-name">Kinda Bet</span>
      </Link>

      <nav className="topnav">
        <NavLink to="/" end className={({ isActive }) => "topnav-link" + (isActive ? " active" : "")}>
          Maçlar
        </NavLink>
        <NavLink to="/firsatlar" className={({ isActive }) => "topnav-link" + (isActive ? " active" : "")}>
          Fırsatlar
        </NavLink>
        <NavLink to="/ayarlar" className={({ isActive }) => "topnav-link" + (isActive ? " active" : "")}>
          Ayarlar
        </NavLink>
      </nav>

      {(lastScan || nextScan) && !running && (
        <div className="scan-info muted small">
          {lastScan && <span>Son tarama: <strong>{lastScan}</strong></span>}
          {lastScan && nextScan && <span className="scan-info-sep">·</span>}
          {nextScan && <span>Sonraki: <strong>{nextScan}</strong></span>}
        </div>
      )}

      <div className="actions">
        <button
          className={`btn ${extraClass}`}
          onClick={onClick}
          disabled={running}
          title={
            running
              ? `${done}/${total} maç güncellendi`
              : confirming
              ? "Tüm maçların oranlarını yenilemek için tekrar tıkla (~2-3 dk sürebilir)"
              : "Tüm maçları ve oranları yenile (~2-3 dk)"
          }
        >
          {label}
        </button>
        {running && total > 0 && (
          <div
            className="topbar-progress"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={total}
            aria-valuenow={done}
          >
            <div className="topbar-progress-bar" style={{ width: `${pct}%` }} />
          </div>
        )}
        {!running && status?.finished_at && failed > 0 && (
          <span className="muted small">{failed} maç güncellenemedi</span>
        )}
      </div>
    </header>
  );
}
