import type { MatchDetail, Market, Selection } from "../api";
import { fmtOdd, formatMarketLabel } from "../format";

type Props = { detail: MatchDetail };

type BestPick = {
  marketKey: string;
  marketLabel: string;
  selectionLabel: string;
  bestOdd: number;
  bestOperator: string;
};

// For a given market+selection, find the operator with the highest odd (best
// for the bettor). Returns null if no operator has a usable odd.
function bestPick(market: Market, sel: Selection): BestPick | null {
  let best: BestPick | null = null;
  const label = formatMarketLabel({
    market_label: market.market_label,
    market_key: market.market_key,
    line: market.line,
  });
  for (const op of sel.operators) {
    if (op.odd == null) continue;
    if (!best || op.odd > best.bestOdd) {
      best = {
        marketKey: market.market_key,
        marketLabel: label,
        selectionLabel: sel.selection_label,
        bestOdd: op.odd,
        bestOperator: op.operator,
      };
    }
  }
  return best;
}

export function BestPricesPanel({ detail }: Props) {
  // Pick the canonical "headline" markets to summarize.
  const targets = ["MATCH_RESULT_FT", "BTTS_FT", "OVER_UNDER_FT@2.5"];
  const picks: BestPick[] = [];

  for (const tgt of targets) {
    const market = detail.markets.find((m) => m.market_key === tgt);
    if (!market) continue;
    for (const sel of market.selections) {
      const b = bestPick(market, sel);
      if (b) picks.push(b);
    }
  }

  if (picks.length === 0) return null;

  // Group by marketLabel for visual rows.
  const grouped = new Map<string, BestPick[]>();
  for (const p of picks) {
    const arr = grouped.get(p.marketLabel) ?? [];
    arr.push(p);
    grouped.set(p.marketLabel, arr);
  }

  return (
    <section className="best-panel">
      <div className="best-panel-head">
        <span className="best-panel-icon">★</span>
        <h2 className="best-panel-title">En İyi Fiyatlar</h2>
        <span className="muted small">Her seçim için en yüksek oran</span>
      </div>
      <div className="best-panel-body">
        {[...grouped.entries()].map(([marketLabel, items]) => (
          <div key={marketLabel} className="best-row">
            <div className="best-row-label">{marketLabel}</div>
            <div className="best-row-items">
              {items.map((p) => (
                <div key={p.selectionLabel + p.bestOperator} className="best-item">
                  <div className="best-item-sel">{p.selectionLabel}</div>
                  <div className="best-item-odd">{fmtOdd(p.bestOdd)}</div>
                  <div className="best-item-op">{p.bestOperator}</div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
