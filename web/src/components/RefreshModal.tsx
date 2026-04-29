import { useEffect, useRef, useState } from "react";
import { getRefreshAllStatus, type RefreshAllStatus } from "../api";

type Props = {
  open: boolean;
  onClose: () => void;       // called when the job finishes (or user cancels-via-Esc)
  title?: string;            // optional override (default: "Maçlar taranıyor")
};

// Modal that polls /api/refresh_all/status while open and renders a progress
// bar. Closes itself when the job completes. Used by the Ayarlar save flow
// and could be reused anywhere else we kick off a long-running refresh.
export function RefreshModal({ open, onClose, title }: Props) {
  const [status, setStatus] = useState<RefreshAllStatus | null>(null);
  const pollRef = useRef<number | null>(null);
  const closingRef = useRef(false);

  useEffect(() => {
    if (!open) return;
    closingRef.current = false;
    // Kick off polling immediately (no initial delay so the bar shows up
    // before the first 2s window).
    const poll = async () => {
      try {
        const s = await getRefreshAllStatus();
        if (closingRef.current) return;
        setStatus(s);
        if (!s.running) {
          // Either the job hasn't started yet (initial state) or it finished.
          // Treat finished as: started_at set AND not running.
          if (s.started_at && !s.running) {
            stopPolling();
            // Brief delay so the user sees the 100% bar before the modal closes.
            window.setTimeout(onClose, 600);
          }
        }
      } catch {/* swallow */}
    };
    poll();
    pollRef.current = window.setInterval(poll, 1500);

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      closingRef.current = true;
      stopPolling();
      window.removeEventListener("keydown", onKey);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const stopPolling = () => {
    if (pollRef.current != null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  if (!open) return null;

  const total = status?.total ?? 0;
  const done  = status?.completed ?? 0;
  const failed = status?.failed ?? 0;
  const pct   = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const phase =
    !status || (!status.running && !status.started_at)
      ? "Başlatılıyor…"
      : status.running
      ? `Yenileniyor… ${done}/${total}`
      : "Tamamlandı";

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="refresh-modal-title">
      <div className="modal">
        <div className="modal-head">
          <h2 id="refresh-modal-title" className="modal-title">
            {title || "Maçlar taranıyor"}
          </h2>
          <p className="modal-sub muted small">
            Tüm maçlar ve operatörler sırayla taranıyor — bu yaklaşık 2-3 dakika sürer.
          </p>
        </div>

        <div className="modal-progress" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done}>
          <div className="modal-progress-bar" style={{ width: `${pct}%` }} />
        </div>

        <div className="modal-stats">
          <span className="modal-phase"><span className="pulse" />{phase}</span>
          {total > 0 && (
            <span className="muted small">
              {pct}% &middot; {done}/{total}
              {failed > 0 && <> · <span className="bad">{failed} hata</span></>}
            </span>
          )}
        </div>

        {status?.error && (
          <div className="modal-error muted small">⚠ {status.error}</div>
        )}
      </div>
    </div>
  );
}
