// Display helpers for odds, percentages, and TR-locale timestamps.

const TR_DATE_FMT = new Intl.DateTimeFormat("tr-TR", {
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

export function fmtOdd(n: number | null | undefined): string {
  return n == null ? "—" : Number(n).toFixed(2);
}

export function fmtPct(p: number | null | undefined): string {
  if (p == null) return "";
  const sign = p > 0 ? "+" : "";
  return `${sign}${p.toFixed(2)}%`;
}

export function fmtKickoff(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(+d) ? iso : TR_DATE_FMT.format(d);
}

const TR_DAY_FMT = new Intl.DateTimeFormat("tr-TR", { weekday: "short" });
const TR_TIME_FMT = new Intl.DateTimeFormat("tr-TR", { hour: "2-digit", minute: "2-digit" });
const TR_DAYMONTH_FMT = new Intl.DateTimeFormat("tr-TR", { day: "2-digit", month: "short" });

// Smart kickoff: "Bugün 22:00" / "Yarın 18:00" / "Çar 20:00" (this week) / "29 Nis 22:00".
export function fmtKickoffSmart(iso: string | null | undefined): { day: string; time: string } {
  if (!iso) return { day: "", time: "" };
  const d = new Date(iso);
  if (isNaN(+d)) return { day: iso, time: "" };
  const now = new Date();
  const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const dayDiff = Math.round((+startOfDay(d) - +startOfDay(now)) / 86_400_000);
  const time = TR_TIME_FMT.format(d);
  let day: string;
  if (dayDiff === 0) day = "Bugün";
  else if (dayDiff === 1) day = "Yarın";
  else if (dayDiff > 1 && dayDiff < 7) day = TR_DAY_FMT.format(d);
  else day = TR_DAYMONTH_FMT.format(d);
  return { day, time };
}

// Server timestamps look like "2026-04-29 12:34:56" (UTC, no tz suffix).
export function fmtTs(s: string | null | undefined): string {
  if (!s) return "—";
  const d = new Date(s.replace(" ", "T") + "Z");
  return isNaN(+d) ? s : TR_DATE_FMT.format(d);
}

// Diff% threshold for green/red coloring (anything within ±0.5% is muted).
export type DiffSign = "good" | "bad" | "zero";
export function diffSign(p: number | null | undefined): DiffSign {
  if (p == null) return "zero";
  if (p > 0.5) return "good";
  if (p < -0.5) return "bad";
  return "zero";
}

// Lowercase + diacritic strip for search matching.
export function norm(s: string): string {
  return s
    .toLocaleLowerCase("tr-TR")
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "");
}

// Build the full market display label, baking the line into the title where
// applicable. Examples:
//   { label: "Alt / Üst",       line: 2.5  } → "Alt / Üst 2.50"
//   { label: "Handikap",        line: -1.5 } → "Handikap -1.50"
//   { label: "Asya Handikap",   line:  0.5 } → "Asya Handikap +0.50"
//   { label: "Üçlü Handikap",   line:  1   } → "Üçlü Handikap +1.00"
//   { label: "Maç Sonucu",      line: null } → "Maç Sonucu"
// Handicap-style markets get an explicit sign; totals never need one.
export function formatMarketLabel(args: {
  market_label: string;
  market_key: string;
  line: number | null;
}): string {
  if (args.line == null) return args.market_label;
  const isHandicap = /HANDICAP/i.test(args.market_key);
  if (isHandicap) {
    const sign = args.line > 0 ? "+" : "";
    return `${args.market_label} ${sign}${args.line.toFixed(2)}`;
  }
  return `${args.market_label} ${args.line.toFixed(2)}`;
}

// Map competition Turkish (or fallback English) name to a short pill label and color class.
export function leaguePillMeta(name: string): [string, string] {
  if (name.includes("Şampiyonlar") || name.includes("Champions")) return ["ŞL", "ucl"];
  if (name.includes("Avrupa") || name.includes("Europa")) return ["AL", "uel"];
  if (name.includes("Premier")) return ["PL", "epl"];
  if (name.includes("Süper")) return ["SL", "sl"];
  if (name.includes("1. Lig")) return ["1L", "tff"];
  return ["–", ""];
}
