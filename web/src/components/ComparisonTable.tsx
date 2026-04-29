import type { Market, OddStatus } from "../api";
import { fmtOdd, fmtPct, diffSign } from "../format";

type Props = {
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

export function ComparisonTable({ market, referenceOperator, allOperators }: Props) {
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
                const diff = isRef ? null : opData.diff_pct;
                return (
                  <td key={sel.selection_key} className="cmp-cell" title={opData.note ?? ""}>
                    <span className="odd">{fmtOdd(opData.odd)}</span>
                    {!isRef && (
                      <span className={`diff ${diffSign(diff)}`}>{fmtPct(diff)}</span>
                    )}
                  </td>
                );
              })}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
