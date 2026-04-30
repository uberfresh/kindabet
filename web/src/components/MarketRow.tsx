import { useState } from "react";
import type { Market } from "../api";
import { fmtOdd, formatMarketLabel } from "../format";
import { ComparisonTable } from "./ComparisonTable";

type Props = {
  matchId: number;
  market: Market;
  referenceOperator: string;
  allOperators: string[];
};

export function MarketRow({ matchId, market, referenceOperator, allOperators }: Props) {
  const [open, setOpen] = useState(false);
  const fullLabel = formatMarketLabel({
    market_label: market.market_label || market.market_key,
    market_key: market.market_key,
    line: market.line,
  });
  return (
    <section className={"market" + (open ? " open" : "")} data-key={market.market_key}>
      <button className="market-toggle" onClick={() => setOpen((o) => !o)}>
        <span className="caret">▸</span>
        <span className="market-label">{fullLabel}</span>
        <span className="ref-odds">
          {market.selections.map((sel) => (
            <span key={sel.selection_key} className="ref-odd">
              <span className="sel-key">{sel.selection_label || sel.selection_key}</span>
              <span className="sel-odd">{fmtOdd(sel.ref_odd)}</span>
            </span>
          ))}
        </span>
      </button>
      {open && (
        <div className="market-body">
          <ComparisonTable
            matchId={matchId}
            market={market}
            referenceOperator={referenceOperator}
            allOperators={allOperators}
          />
        </div>
      )}
    </section>
  );
}
