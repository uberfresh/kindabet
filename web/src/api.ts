// API contract — mirrors what app.py returns. Keep in sync with the Flask
// routes; if the backend shape drifts, TypeScript will scream.

// Why this operator's cell is empty (shown to the user as a small pill).
export type OddStatus = "ok" | "na_match" | "na_market" | "na_selection" | "na_error";

export type OperatorOdd = {
  operator: string;
  odd: number | null;
  ok: boolean;
  note: string | null;
  taken_at: string | null;
  diff_pct: number | null;
  status: OddStatus;
};

export type Selection = {
  selection_key: string;
  selection_label: string;
  ref_odd: number;
  operators: OperatorOdd[];
};

export type Market = {
  market_key: string;
  market_label: string;
  line: number | null;
  selections: Selection[];
};

export type HeadlineOdds = {
  "1"?: number;
  X?: number;
  "2"?: number;
};

export type OverUnderOdds = {
  OVER?: number;
  UNDER?: number;
};

export type MatchSummary = {
  id: number;
  sport?: string;              // "football" | "basketball" | … (optional for legacy responses)
  sport_name_tr?: string;      // Turkish sport name — backend resolves via the EN→TR map
  competition: string;
  league_term: string;
  home: string;
  away: string;
  kickoff_utc: string;
  kambi_event_id: number | null;
  discovered_at: string;
  last_refresh: string | null;
  headline_odds: HeadlineOdds | null;
  over_under_2_5?: OverUnderOdds | null;
  market_count?: number;
  logo_url?: string | null;
};

export type League = {
  competition: string;
  matches: MatchSummary[];
};

export type MatchesResponse = {
  leagues: League[];
  operators: string[];
  reference_operator: string;
};

export type OperatorStatus = {
  operator: string;
  with_odds: number;
  total: number;
  last_refresh: string | null;
};

export type MatchDetail = {
  match: MatchSummary;
  reference_operator: string;
  operators: string[];
  markets: Market[];
  operator_status: OperatorStatus[];
  last_refresh: string | null;
};

export type RefreshResponse = {
  ok: boolean;
  rows: number;
  by_operator: Record<string, { with_odds: number; total: number }>;
  markets: Market[];
  operator_status: OperatorStatus[];
};

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

export function fetchMatches(sync = false): Promise<MatchesResponse> {
  return api<MatchesResponse>(`/api/matches${sync ? "?sync=1" : ""}`);
}

export function fetchMatch(id: number): Promise<MatchDetail> {
  return api<MatchDetail>(`/api/match/${id}`);
}

export function refreshMatch(id: number): Promise<RefreshResponse> {
  return api<RefreshResponse>(`/api/match/${id}/refresh`, { method: "POST" });
}

export type RefreshAllStatus = {
  running: boolean;
  started_at: string | null;
  finished_at: string | null;
  total: number;
  completed: number;
  failed: number;
  error: string | null;
  scope?: string | null;             // "all" | "sport:<term>" | "discovery" | null
  auto_refresh_interval_seconds?: number;
  next_scheduled_at?: string | null;
};

export function startRefreshAll(): Promise<RefreshAllStatus & { ok: boolean; already_running: boolean }> {
  return api(`/api/refresh_all`, { method: "POST" });
}

export function startRefreshSport(sportTerm: string): Promise<RefreshAllStatus & { ok: boolean; already_running: boolean }> {
  return api(`/api/refresh_sport/${encodeURIComponent(sportTerm)}`, { method: "POST" });
}

export type DiscoveryRefreshResponse = { ok: boolean; discovered: number; error: string | null };

export function startRefreshDiscovery(): Promise<DiscoveryRefreshResponse> {
  return api(`/api/refresh_discovery`, { method: "POST" });
}

export function getRefreshAllStatus(): Promise<RefreshAllStatus> {
  return api<RefreshAllStatus>(`/api/refresh_all/status`);
}

export type BiggestDiff = {
  match_id: number;
  home: string;
  away: string;
  competition: string;
  logo_url?: string | null;
  kickoff_utc: string;
  market_key: string;
  market_label: string;
  line: number | null;
  selection_key: string;
  selection_label: string;
  best_operator: string;
  best_odd: number;
  worst_operator: string;
  worst_odd: number;
  diff_pct: number;
  all_operators: { operator: string; odd: number }[];
};

export type BiggestDiffsResponse = {
  items: BiggestDiff[];
  total_evaluated: number;
  computed_at: string | null;
};

export function fetchBiggestDiffs(limit = 10): Promise<BiggestDiffsResponse> {
  return api<BiggestDiffsResponse>(`/api/biggest_diffs?limit=${limit}`);
}

// ---------- Settings: leagues ----------

export type LeagueOption = {
  // Multi-sport metadata. Football leagues all share sport_term="football";
  // basketball, tennis, etc. expose their own. Legacy clients can ignore these
  // fields and treat any league as football.
  sport_term: string;
  sport_name_tr: string;
  sport_name_en?: string;
  league_term: string;
  display_name: string;
  country: string | null;
  country_code?: string | null;
  logo_url?: string | null;
};

export type AvailableLeaguesResponse = { leagues: LeagueOption[] };

export type EnabledLeague = {
  sport_term?: string;          // Optional during transition; backend defaults to "football"
  display_name: string;
  league_term: string;
};
export type EnabledLeaguesResponse = { enabled: EnabledLeague[] };

export function fetchAvailableLeagues(): Promise<AvailableLeaguesResponse> {
  return api<AvailableLeaguesResponse>(`/api/leagues/available`);
}

export function fetchEnabledLeagues(): Promise<EnabledLeaguesResponse> {
  return api<EnabledLeaguesResponse>(`/api/settings/leagues`);
}

export function saveEnabledLeagues(enabled: EnabledLeague[]): Promise<{ ok: boolean; enabled: EnabledLeague[] }> {
  return api(`/api/settings/leagues`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}
