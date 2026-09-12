export const SECTORS = ["Health Care","Information Technology","Consumer Discretionary","Financials","Consumer Staples","Industrials","Utilities","Materials","Real Estate","Energy","Communication Services"] as const;

export type Sector = (typeof SECTORS)[number];

export const METRIC_LABELS = ["Climate & GHG","Energy","Water","Waste & Circularity","Biodiversity & Land","Pollution & Toxic Releases","Workers & Labor","Health & Safety","Human Rights & Supply Chain","Product & Customer Responsibility","Data Privacy & Cybersecurity","Community & Social Impact","Transition & Green Investment","Physical Climate Risk","Resource & Supply Resilience"] as const;

export type MetricLabel = (typeof METRIC_LABELS)[number];

export interface ScoreDetail {
  value: number | null;
  status?: string;
  method?: string;
  reporting_year?: number;
  coverage?: { scored_topics: number; total_topics: number };
  components?: { topic: string; label: string; score: number; formula: string; inputs: Record<string, any>[]; annualized_change_percent?: number; baseline_year?: number; reporting_year?: number }[];
  limitations?: string[];
}

export interface Company {
  id: number;
  ticker: string;
  name: string;
  sector: Sector;
  metrics: number[];
  avgMateriality: number;
  highPriorityTopics: string;
  profileMethod: string;
  scores?: { environmental: ScoreDetail; social: ScoreDetail; financial: ScoreDetail };
  environment?: Record<string, unknown>;
}
