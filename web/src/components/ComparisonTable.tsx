import { useEffect, useRef, useState } from "react";
import {
  fetchMatchHistory,
  type Market,
  type OddStatus,
  type OperatorOdd,
  type OddsHistoryItem,
} from "../api";
import { fmtOdd, fmtPct, diffSign } from "../format";
import { Sparkline } from "./Sparkline";

type Props = {
  matchId: number;
  market: Market;
  referenceOperator: string;
  allOperators: string[];
};

// Short Turkish labels for the absence pill — kept tight on purpose so the
// table doesn't reflow on narrow screens.
const STATUS_LABEL: Record<OddStatus, string> = {
  ok:           "",
  na_match:     "maç yok",
  na_market:    "yok",
  na_selection: "seçim yok",
  na_error:     "hata",
};

const STATUS_TITLE: Record<OddStatus, string> = {
  ok:           "",
  na_match:     "Operatör bu maçı sunmuyor",
  na_market:    "Operatör bu marketi sunmuyor",
  na_selection: "Operatör bu seçimi sunmuyor",
  na_error:     "Tarama sırasında hata oluştu",
};

export function ComparisonTable({ matchId, market, referenceOperator, allOperators }: Props) {
  const opOrder = [
    referenceOperator,
    ...allOperators.filter((o) => o !== referenceOperator).sort(),
  ];

  return (
    <table className="cmp-table">
      <thead>
        <tr>
          <th>Operatör</th>
          {market.selections.map((sel) => (
            <th key={sel.selection_key}>{sel.selection_label || sel.selection_key}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {opOrder.map((op) => {
          const isRef = op === referenceOperator;
          return (
            <tr key={op}>
              <td className={"op-name" + (isRef ? " ref" : "")}>{op}</td>
              {market.selections.map((sel) => {
                const opData = sel.operators.find((o) => o.operator === op);
                if (!opData || opData.odd == null) {
                  // Show a small reason pill — the title attribute exposes
                  // the diagnostic note on hover for debugging.
                  const status: OddStatus = opData?.status ?? "na_match";
                  const baseTitle = STATUS_TITLE[status] || "";
                  const fullTitle = opData?.note ? `${baseTitle} · ${opData.note}` : baseTitle;
                  return (
                    <td key={sel.selection_key} className="cmp-cell na" title={fullTitle}>
                      <span className={`status-pill ${status}`}>
                        {STATUS_LABEL[status] || "—"}
                      </span>
                    </td>
                  );
                }
                return (
                  <OddCell
                    key={sel.selection_key}
                    matchId={matchId}
                    marketKey={market.market_key}
                    selectionKey={sel.selection_key}
                    isRef={isRef}
                    odd={opData}
                  />
                );
              })}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}


/**
 * One operator × selection cell. Beyond rendering the odd + diff%, it:
 *   - shows a small ↑/↓ chip when the odd just changed vs the prior snapshot,
 *   - on hover, fetches the full odds history and renders a Sparkline popover.
 *
 * History is fetched lazily (on first hover) and cached for the lifetime of
 * the cell. Cells with change_count <= 1 skip both the chip and the popover.
 */
function OddCell(props: {
  matchId: number;
  marketKey: string;
  selectionKey: string;
  isRef: boolean;
  odd: OperatorOdd;
}) {
  const { matchId, marketKey, selectionKey, isRef, odd } = props;
  const [hovered, setHovered]   = useState(false);
  const [history, setHistory]   = useState<OddsHistoryItem[] | null>(null);
  const [loading, setLoading]   = useState(false);
  const fetchedRef = useRef(false);

  const hasHistory = (odd.change_count ?? 1) > 1;
  const justChanged =
    odd.prev_odd != null && odd.odd != null && odd.prev_odd !== odd.odd;
  const trend: "up" | "down" | null =
    justChanged && odd.odd != null && odd.prev_odd != null
      ? odd.odd > odd.prev_odd ? "up" : "down"
      : null;

  // Lazy-load history the first time the cell is hovered. The popover
  // doesn't render until we have at least 2 datapoints (single-point
  // sparklines aren't useful and Sparkline returns null in that case).
  useEffect(() => {
    if (!hovered || fetchedRef.current || !hasHistory) return;
    fetchedRef.current = true;
    setLoading(true);
    fetchMatchHistory(matchId, {
      operator: odd.operator,
      market_key: marketKey,
      selection_key: selectionKey,
      limit: 60,
    })
      .then((r) => setHistory(r.items))
      .catch(() => setHistory([]))
      .finally(() => setLoading(false));
  }, [hovered, hasHistory, matchId, odd.operator, marketKey, selectionKey]);

  const diff = isRef ? null : odd.diff_pct;
  const popOpen = hovered && hasHistory;

  return (
    <td
      className={"cmp-cell" + (justChanged ? " just-changed" : "")}
      title={odd.note ?? ""}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      // For touch users — tap toggles the popover.
      onClick={() => setHovered((v) => !v)}
    >
      <span className="odd">{fmtOdd(odd.odd)}</span>
      {!isRef && diff != null && (
        <span className={`diff ${diffSign(diff)}`}>{fmtPct(diff)}</span>
      )}
      {trend && odd.prev_odd != null && (
        <span
          className={`trend-chip ${trend}`}
          title={`Önceki: ${fmtOdd(odd.prev_odd)} → ${fmtOdd(odd.odd)}`}
          aria-label={`Oran ${trend === "up" ? "yükseldi" : "düştü"}`}
        >
          {trend === "up" ? "▲" : "▼"} {fmtOdd(odd.odd)}
          <span className="trend-from">{fmtOdd(odd.prev_odd)}</span>
        </span>
      )}
      {popOpen && (
        <div className="spark-pop">
          <div className="spark-pop-head muted small">
            {odd.operator} · oran geçmişi
          </div>
          {loading && <div className="spark-pop-loading muted small">yükleniyor…</div>}
          {!loading && history && history.length >= 2 && (
            <>
              <Sparkline points={history} />
              <div className="spark-pop-foot muted small">
                {history.length} değişiklik · {fmtOdd(history[0].odd)} → {fmtOdd(history[history.length - 1].odd)}
              </div>
            </>
          )}
          {!loading && history && history.length < 2 && (
            <div className="spark-pop-loading muted small">Geçmiş yok</div>
          )}
        </div>
      )}
    </td>
  );
}
