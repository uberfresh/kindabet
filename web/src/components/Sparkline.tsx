// Inline SVG sparkline — no chart library, ~80 LOC.
// Renders a small line chart of N price points with an end dot, dashed
// reference line at the latest value, and a green/red trend tint.

type Point = { taken_at: string; odd: number | null };

type Props = {
  points: Point[];
  width?: number;
  height?: number;
};

export function Sparkline({ points, width = 220, height = 60 }: Props) {
  const pts = points.filter((p): p is { taken_at: string; odd: number } => p.odd != null);
  if (pts.length < 2) {
    // Single value or nothing to plot — caller should hide the popover.
    return null;
  }

  const odds = pts.map((p) => p.odd);
  const min = Math.min(...odds);
  const max = Math.max(...odds);
  const range = max - min || 1;
  const padX = 6;
  const padY = 8;
  const innerW = width - padX * 2;
  const innerH = height - padY * 2;

  // Map (index, odd) -> (x, y) — y inverted because higher odds plot up.
  const xy = pts.map((p, i) => ({
    x: padX + (innerW * i) / (pts.length - 1),
    y: padY + innerH - ((p.odd - min) / range) * innerH,
    odd: p.odd,
    taken_at: p.taken_at,
  }));

  const linePath = xy.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
  // Filled area under the line for visual weight.
  const areaPath = `${linePath} L ${xy[xy.length - 1].x.toFixed(1)} ${(padY + innerH).toFixed(1)} L ${xy[0].x.toFixed(1)} ${(padY + innerH).toFixed(1)} Z`;

  const first = pts[0].odd;
  const last  = pts[pts.length - 1].odd;
  // For betting odds, a HIGHER number is better for the bettor — color
  // accordingly so the bookmaker shortening (lower) reads red and lengthening
  // (higher) reads green, matching the diff-pct convention used elsewhere.
  const trend: "up" | "down" | "flat" = last > first ? "up" : last < first ? "down" : "flat";
  const color = trend === "up" ? "#16a34a" : trend === "down" ? "#dc2626" : "#94a3b8";

  return (
    <svg
      className="sparkline"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`Oran geçmişi: ${first.toFixed(2)} → ${last.toFixed(2)} (${pts.length} değişiklik)`}
    >
      <path d={areaPath} fill={color} fillOpacity={0.12} />
      <path d={linePath} fill="none" stroke={color} strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" />
      {/* Endpoint dot */}
      <circle
        cx={xy[xy.length - 1].x}
        cy={xy[xy.length - 1].y}
        r={2.8}
        fill={color}
      />
      {/* First-point dot (lighter) */}
      <circle
        cx={xy[0].x}
        cy={xy[0].y}
        r={2}
        fill={color}
        fillOpacity={0.5}
      />
    </svg>
  );
}
