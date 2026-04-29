import type { Market } from "../api";
import { fmtOdd, fmtPct, diffSign } from "../format";

type Props = {
  market: Market;
  referenceOperator: string;
  allOperators: string[];
};

export function ComparisonTable({ market, referenceOperator, allOperators }: Props) {
  // Reference first, others alphabetical
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
          let rowNote: string | null = null;
          return (
            <tr key={op}>
              <td className={"op-name" + (isRef ? " ref" : "")}>{op}</td>
              {market.selections.map((sel) => {
                const opData = sel.operators.find((o) => o.operator === op);
                if (!opData || opData.odd == null) {
                  if (opData?.note) rowNote = opData.note;
                  return (
                    <td key={sel.selection_key} className="cmp-cell na">—</td>
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
              {/* Surface a per-row note if every cell was empty */}
              {rowNote && false /* placeholder for tooltip integration */ && null}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
